//! Windows Media Foundation H.264 Decoder MFT.
//!
//! Decodes incoming H.264 NALU bitstream from WebRTC video track into uncompressed 32-bit RGB/BGRA
//! pixel buffers for direct presentation on client viewer without JPEG transcoding overhead.
//! Handles dynamic stream format changes, SPS/PPS parameter negotiation, need-more-input states,
//! and drain/flush commands.

use std::time::Instant;
use windows::core::{Interface, GUID, HRESULT};
use windows::Win32::Media::MediaFoundation::*;
use windows::Win32::System::Com::*;

pub const MF_E_TRANSFORM_STREAM_CHANGE: HRESULT = HRESULT(0x80040207u32 as i32);
pub const MF_E_TRANSFORM_NEED_MORE_INPUT: HRESULT = HRESULT(0xC00D6D61u32 as i32);

pub struct MfH264Decoder {
    transform: IMFTransform,
    width: u32,
    height: u32,
    is_initialized: bool,
}

// Microsoft H.264 Video Decoder MFT CLSID: 62CE7E72-4C71-4d20-B15D-253E3D357E0F
const CLSID_CMSH264DecoderMFT: GUID = GUID::from_u128(0x62ce7e72_4c71_4d20_b15d_253e3d357e0f);

impl MfH264Decoder {
    pub fn new() -> Result<Self, String> {
        unsafe {
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            MFStartup(MF_SDK_VERSION, MFSTARTUP_NOSOCKET)
                .map_err(|e| format!("MFStartup failed in decoder: {}", e))?;

            let transform: IMFTransform = CoCreateInstance(&CLSID_CMSH264DecoderMFT, None, CLSCTX_INPROC_SERVER)
                .map_err(|e| format!("Failed to create H.264 Decoder MFT: {}", e))?;

            // 1. Configure Input Type: H.264 Video
            let in_type = MFCreateMediaType().map_err(|e| e.to_string())?;

            in_type.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video).map_err(|e| e.to_string())?;
            in_type.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_H264).map_err(|e| e.to_string())?;
            transform.SetInputType(0, &in_type, 0)
                .map_err(|e| format!("Decoder SetInputType failed: {}", e))?;

            // 2. Discover and configure initial Output Type: RGB32 / ARGB32 for UI rendering
            let mut selected_output = false;
            let mut type_idx = 0u32;
            while let Ok(t) = transform.GetOutputAvailableType(0, type_idx) {
                let subtype = t.GetGUID(&MF_MT_SUBTYPE).ok();
                if subtype == Some(MFVideoFormat_RGB32) || subtype == Some(MFVideoFormat_ARGB32) {
                    if transform.SetOutputType(0, &t, 0).is_ok() {
                        selected_output = true;
                        break;
                    }
                }
                type_idx += 1;
            }

            // Fallback: create explicit RGB32 media type if enumerator had no preferred RGB32
            if !selected_output {
                let explicit_out = MFCreateMediaType().map_err(|e| e.to_string())?;
                explicit_out.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video).map_err(|e| e.to_string())?;
                explicit_out.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_RGB32).map_err(|e| e.to_string())?;
                transform.SetOutputType(0, &explicit_out, 0).map_err(|e| e.to_string())?;
            }

            let _ = transform.ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);

            Ok(Self {
                transform,
                width: 0,
                height: 0,
                is_initialized: true,
            })
        }
    }

    pub fn get_dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    pub fn flush(&mut self) {
        unsafe {
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
        }
    }

    /// Decode an incoming H.264 Access Unit (one or more NALUs).
    /// Returns (bytes_written, width, height, decode_duration_ns).
    /// If more input is needed before a frame can be completed, returns (0, width, height, dur_ns).
    pub fn decode_frame(
        &mut self,
        h264_nalu: &[u8],
        out_rgba: &mut [u8],
    ) -> Result<(usize, u32, u32, u64), String> {
        let t_start = Instant::now();
        if h264_nalu.is_empty() {
            return Ok((0, self.width, self.height, 0));
        }

        unsafe {
            // 1. Create MediaBuffer & Sample for input packet
            let in_buf = MFCreateMemoryBuffer(h264_nalu.len() as u32)
                .map_err(|e| format!("Decoder MFCreateMemoryBuffer failed: {}", e))?;

            let mut p_dst = std::ptr::null_mut();
            in_buf.Lock(&mut p_dst, None, None).map_err(|e| format!("Lock failed: {}", e))?;
            std::ptr::copy_nonoverlapping(h264_nalu.as_ptr(), p_dst, h264_nalu.len());
            let _ = in_buf.Unlock();
            let _ = in_buf.SetCurrentLength(h264_nalu.len() as u32);

            let in_sample = MFCreateSample().map_err(|e| format!("MFCreateSample failed: {}", e))?;
            in_sample.AddBuffer(&in_buf).map_err(|e| e.to_string())?;

            // 2. Deliver sample to decoder transform
            match self.transform.ProcessInput(0, &in_sample, 0) {
                Ok(_) => {}
                Err(e) => {
                    return Err(format!("Decoder ProcessInput failed: {}", e));
                }
            }

            // 3. ProcessOutput to extract decoded uncompressed frame
            let mut bytes_written = 0usize;
            for _ in 0..2 {
                let out_buf = MFCreateMemoryBuffer(out_rgba.len() as u32)
                    .map_err(|e| format!("Decoder out_buf failed: {}", e))?;

                let out_sample = MFCreateSample().map_err(|e| format!("MFCreateSample failed: {}", e))?;
                out_sample.AddBuffer(&out_buf).map_err(|e| e.to_string())?;

                let mut output_data = MFT_OUTPUT_DATA_BUFFER {
                    dwStreamID: 0,
                    pSample: std::mem::ManuallyDrop::new(Some(out_sample)),
                    dwStatus: 0,
                    pEvents: std::mem::ManuallyDrop::new(None),
                };

                let mut status = 0u32;
                let hr = self.transform.ProcessOutput(0, std::slice::from_mut(&mut output_data), &mut status);

                if hr.is_ok() {
                    if let Some(ref sample) = *output_data.pSample {
                        if let Ok(buf) = sample.GetBufferByIndex(0) {
                                let mut p_src = std::ptr::null_mut();
                                let mut cur_len = 0u32;
                                if buf.Lock(&mut p_src, None, Some(&mut cur_len)).is_ok() {
                                    if cur_len > 0 {
                                        let to_copy = (cur_len as usize).min(out_rgba.len());
                                        std::ptr::copy_nonoverlapping(p_src, out_rgba.as_mut_ptr(), to_copy);
                                        bytes_written = to_copy;
                                    }
                                    let _ = buf.Unlock();
                                }
                        }
                    }
                    std::mem::ManuallyDrop::drop(&mut output_data.pSample);
                    std::mem::ManuallyDrop::drop(&mut output_data.pEvents);
                    break;
                } else if hr.as_ref().err().map(|e| e.code()) == Some(MF_E_TRANSFORM_STREAM_CHANGE) {
                    // Dynamic format change (resolution, aspect ratio, SPS/PPS parsed)
                    std::mem::ManuallyDrop::drop(&mut output_data.pSample);
                    std::mem::ManuallyDrop::drop(&mut output_data.pEvents);

                    let mut type_idx = 0u32;
                    while let Ok(t) = self.transform.GetOutputAvailableType(0, type_idx) {
                        let subtype = t.GetGUID(&MF_MT_SUBTYPE).ok();
                        if subtype == Some(MFVideoFormat_RGB32) || subtype == Some(MFVideoFormat_ARGB32) {
                            let frame_size = t.GetUINT64(&MF_MT_FRAME_SIZE).unwrap_or(0);
                            let w = (frame_size >> 32) as u32;
                            let h = frame_size as u32;
                            if w > 0 && h > 0 {
                                self.width = w;
                                self.height = h;
                            }
                            let _ = self.transform.SetOutputType(0, &t, 0);
                            break;
                        }
                        type_idx += 1;
                    }
                    // Loop around and retry ProcessOutput with updated format
                    continue;
                } else if hr.as_ref().err().map(|e| e.code()) == Some(MF_E_TRANSFORM_NEED_MORE_INPUT) {
                    // Normal state: decoder needs more slices/NALUs before producing an output frame
                    std::mem::ManuallyDrop::drop(&mut output_data.pSample);
                    std::mem::ManuallyDrop::drop(&mut output_data.pEvents);
                    break;
                } else {
                    std::mem::ManuallyDrop::drop(&mut output_data.pSample);
                    std::mem::ManuallyDrop::drop(&mut output_data.pEvents);
                    return Err(format!("Decoder ProcessOutput failed: {:?}", hr));
                }
            }

            let dur_ns = t_start.elapsed().as_nanos() as u64;
            Ok((bytes_written, self.width, self.height, dur_ns))
        }
    }
}

impl Drop for MfH264Decoder {
    fn drop(&mut self) {
        unsafe {
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
            let _ = MFShutdown();
        }
    }
}
