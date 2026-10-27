//! Windows Media Foundation low-latency H.264 MFT encoder.
//!
//! Enumerates and selects hardware MFT encoders (NVENC, Intel QuickSync, AMD AMF) with
//! automatic fallback to Microsoft software H.264 MFT.
//! Supports direct D3D11/DXGI surface input for hardware encoders to eliminate CPU readback,
//! and memory buffer input for software MFT fallback. Enforces zero B-frames and low-latency mode via ICodecAPI.

use std::time::Instant;
use windows::Win32::Graphics::Direct3D11::ID3D11Texture2D;
use windows::Win32::Media::MediaFoundation::*;
use windows::Win32::System::Com::*;
use windows::core::{Interface, VARIANT};

pub struct MfH264Encoder {
    transform: IMFTransform,
    codec_api: Option<ICodecAPI>,
    pub width: u32,
    pub height: u32,
    bitrate_bps: u32,
    fps: u32,
    is_hardware: bool,
    is_surface_aware: bool,
    frame_index: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct EncoderConfig {
    pub width: u32,
    pub height: u32,
    pub target_bitrate_kbps: u32,
    pub fps: u32,
}

impl Default for EncoderConfig {
    fn default() -> Self {
        Self {
            width: 1920,
            height: 1080,
            target_bitrate_kbps: 4500,
            fps: 60,
        }
    }
}

impl MfH264Encoder {
    pub fn new(config: EncoderConfig) -> Result<Self, String> {
        unsafe {
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            MFStartup(MF_SDK_VERSION, MFSTARTUP_NOSOCKET)
                .map_err(|e| format!("MFStartup failed: {}", e))?;

            let (transform, is_hw) = Self::create_encoder_transform(config.width, config.height)?;
            let codec_api: Option<ICodecAPI> = transform.cast().ok();

            let mut encoder = Self {
                transform,
                codec_api,
                width: config.width,
                height: config.height,
                bitrate_bps: config.target_bitrate_kbps.clamp(500, 30000) * 1000,
                fps: config.fps.clamp(15, 120),
                is_hardware: is_hw,
                is_surface_aware: false,
                frame_index: 0,
            };

            encoder.configure_codecapi()?;
            encoder.configure_media_types()?;

            let _ = encoder.transform.ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);

            Ok(encoder)
        }
    }

    /// Enumerate MFT encoders: checks for hardware first, falls back to software.
    unsafe fn create_encoder_transform(width: u32, height: u32) -> Result<(IMFTransform, bool), String> {
        let input_type = MFT_REGISTER_TYPE_INFO {
            guidMajorType: MFMediaType_Video,
            guidSubtype: MFVideoFormat_NV12,
        };
        let output_type = MFT_REGISTER_TYPE_INFO {
            guidMajorType: MFMediaType_Video,
            guidSubtype: MFVideoFormat_H264,
        };

        // 1. Try Hardware MFT (NVENC, QuickSync, AMF)
        let hw_flags = MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SORTANDFILTER;
        let mut activates_ptr = std::ptr::null_mut();
        let mut count = 0u32;

        let hr_hw = MFTEnumEx(
            MFT_CATEGORY_VIDEO_ENCODER,
            hw_flags,
            Some(&input_type),
            Some(&output_type),
            &mut activates_ptr,
            &mut count,
        );

        if hr_hw.is_ok() && count > 0 && !activates_ptr.is_null() {
            let activates = std::slice::from_raw_parts(activates_ptr, count as usize);
            if let Some(first_activate) = activates.first().and_then(|a| a.as_ref()) {
                if let Ok(transform) = first_activate.ActivateObject::<IMFTransform>() {
                    CoTaskMemFree(Some(activates_ptr as *const _));
                    return Ok((transform, true));
                }
            }
            CoTaskMemFree(Some(activates_ptr as *const _));
        }

        // 2. Fallback: Software MFT (Microsoft H.264 Encoder MFT)
        let sw_flags = MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER;
        let mut sw_activates_ptr = std::ptr::null_mut();
        let mut sw_count = 0u32;

        let hr_sw = MFTEnumEx(
            MFT_CATEGORY_VIDEO_ENCODER,
            sw_flags,
            Some(&input_type),
            Some(&output_type),
            &mut sw_activates_ptr,
            &mut sw_count,
        );

        if hr_sw.is_ok() && sw_count > 0 && !sw_activates_ptr.is_null() {
            let sw_activates = std::slice::from_raw_parts(sw_activates_ptr, sw_count as usize);
            if let Some(first_sw) = sw_activates.first().and_then(|a| a.as_ref()) {
                if let Ok(transform) = first_sw.ActivateObject::<IMFTransform>() {
                    CoTaskMemFree(Some(sw_activates_ptr as *const _));
                    return Ok((transform, false));
                }
            }
            CoTaskMemFree(Some(sw_activates_ptr as *const _));
        }

        Err("No suitable H.264 MFT encoder found on the system (hardware or software)".to_string())
    }

    /// Enforce zero B-frames and low latency via ICodecAPI properties.
    unsafe fn configure_codecapi(&mut self) -> Result<(), String> {
        let api = match self.codec_api.as_ref() {
            Some(a) => a,
            None => return Ok(()),
        };

        // 1. CODECAPI_AVLowLatencyMode = VARIANT_TRUE
        let var_true = VARIANT::from(true);
        let _ = api.SetValue(&CODECAPI_AVLowLatencyMode, &var_true);

        // 2. Zero B-frames: CODECAPI_AVEncMPVDefaultBPictureCount = 0
        let var_b_frames = VARIANT::from(0u32);
        let _ = api.SetValue(&CODECAPI_AVEncMPVDefaultBPictureCount, &var_b_frames);

        // 3. Bitrate: CODECAPI_AVEncCommonMeanBitRate
        let var_bitrate = VARIANT::from(self.bitrate_bps);
        let _ = api.SetValue(&CODECAPI_AVEncCommonMeanBitRate, &var_bitrate);

        Ok(())
    }

    /// Setup input (NV12/ARGB) and output (H.264) media types.
    unsafe fn configure_media_types(&mut self) -> Result<(), String> {
        // Output type: H.264
        let out_type = MFCreateMediaType().map_err(|e| e.to_string())?;

        out_type.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video).map_err(|e| e.to_string())?;
        out_type.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_H264).map_err(|e| e.to_string())?;
        out_type.SetUINT32(&MF_MT_AVG_BITRATE, self.bitrate_bps).map_err(|e| e.to_string())?;
        out_type.SetUINT64(&MF_MT_FRAME_SIZE, ((self.width as u64) << 32) | self.height as u64).map_err(|e| e.to_string())?;
        out_type.SetUINT64(&MF_MT_FRAME_RATE, ((self.fps as u64) << 32) | 1).map_err(|e| e.to_string())?;
        out_type.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32).map_err(|e| e.to_string())?;

        self.transform.SetOutputType(0, &out_type, 0)
            .map_err(|e| format!("SetOutputType failed: {}", e))?;

        // Input type: NV12
        let in_type = MFCreateMediaType().map_err(|e| e.to_string())?;

        in_type.SetGUID(&MF_MT_MAJOR_TYPE, &MFMediaType_Video).map_err(|e| e.to_string())?;
        in_type.SetGUID(&MF_MT_SUBTYPE, &MFVideoFormat_NV12).map_err(|e| e.to_string())?;
        in_type.SetUINT64(&MF_MT_FRAME_SIZE, ((self.width as u64) << 32) | self.height as u64).map_err(|e| e.to_string())?;
        in_type.SetUINT64(&MF_MT_FRAME_RATE, ((self.fps as u64) << 32) | 1).map_err(|e| e.to_string())?;
        in_type.SetUINT32(&MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive.0 as u32).map_err(|e| e.to_string())?;

        self.transform.SetInputType(0, &in_type, 0)
            .map_err(|e| format!("SetInputType failed: {}", e))?;

        Ok(())
    }

    /// Encode frame directly from D3D11 texture surface on GPU (zero-copy hardware path).
    /// If hardware surface encoding is not accepted, returns Err so caller can use memory fallback.
    pub fn encode_d3d11_texture(
        &mut self,
        texture: &ID3D11Texture2D,
        force_keyframe: bool,
        out_h264: &mut [u8],
    ) -> Result<(usize, bool, u64), String> {
        if !self.is_hardware {
            return Err("Software encoder requires memory buffer".to_string());
        }

        let t_start = Instant::now();
        unsafe {
            let surface_buf = MFCreateDXGISurfaceBuffer(
                &windows::Win32::Graphics::Direct3D11::ID3D11Texture2D::IID,
                texture,
                0,
                false,
            ).map_err(|e| format!("MFCreateDXGISurfaceBuffer failed: {}", e))?;

            let in_sample = MFCreateSample().map_err(|e| e.to_string())?;
            in_sample.AddBuffer(&surface_buf).map_err(|e| e.to_string())?;

            let sample_duration_hns = (10_000_000u64 / (self.fps as u64)) as i64;
            let sample_time_hns = (self.frame_index as i64) * sample_duration_hns;
            self.frame_index += 1;

            let _ = in_sample.SetSampleTime(sample_time_hns);
            let _ = in_sample.SetSampleDuration(sample_duration_hns);

            if force_keyframe {
                let _ = in_sample.SetUINT32(&MFSampleExtension_CleanPoint, 1);
            }

            self.transform.ProcessInput(0, &in_sample, 0)
                .map_err(|e| format!("ProcessInput (GPU surface) failed: {}", e))?;

            self.is_surface_aware = true;
            self.process_output_sample(force_keyframe, out_h264, t_start)
        }
    }

    /// Encode frame from memory buffer (CPU staging readback software fallback).
    pub fn encode_memory_buffer(
        &mut self,
        nv12_input: &[u8],
        force_keyframe: bool,
        out_h264: &mut [u8],
    ) -> Result<(usize, bool, u64), String> {
        let t_start = Instant::now();
        unsafe {
            let in_buffer = MFCreateMemoryBuffer(nv12_input.len() as u32)
                .map_err(|e| format!("MFCreateMemoryBuffer failed: {}", e))?;

            let mut p_dst = std::ptr::null_mut();
            in_buffer.Lock(&mut p_dst, None, None).map_err(|e| format!("Buffer Lock failed: {}", e))?;
            std::ptr::copy_nonoverlapping(nv12_input.as_ptr(), p_dst, nv12_input.len());
            let _ = in_buffer.Unlock();
            let _ = in_buffer.SetCurrentLength(nv12_input.len() as u32);

            let in_sample = MFCreateSample().map_err(|e| format!("MFCreateSample failed: {}", e))?;
            in_sample.AddBuffer(&in_buffer).map_err(|e| e.to_string())?;

            let sample_duration_hns = (10_000_000u64 / (self.fps as u64)) as i64;
            let sample_time_hns = (self.frame_index as i64) * sample_duration_hns;
            self.frame_index += 1;

            let _ = in_sample.SetSampleTime(sample_time_hns);
            let _ = in_sample.SetSampleDuration(sample_duration_hns);

            if force_keyframe {
                let _ = in_sample.SetUINT32(&MFSampleExtension_CleanPoint, 1);
            }

            self.transform.ProcessInput(0, &in_sample, 0)
                .map_err(|e| format!("ProcessInput failed: {}", e))?;

            self.process_output_sample(force_keyframe, out_h264, t_start)
        }
    }

    unsafe fn process_output_sample(
        &mut self,
        force_keyframe: bool,
        out_h264: &mut [u8],
        t_start: Instant,
    ) -> Result<(usize, bool, u64), String> {
        let out_buffer = MFCreateMemoryBuffer(out_h264.len() as u32)
            .map_err(|e| format!("MFCreateMemoryBuffer (out) failed: {}", e))?;

        let out_sample = MFCreateSample().map_err(|e| format!("MFCreateSample (out) failed: {}", e))?;
        out_sample.AddBuffer(&out_buffer).map_err(|e| e.to_string())?;

        let mut output_data = MFT_OUTPUT_DATA_BUFFER {
            dwStreamID: 0,
            pSample: std::mem::ManuallyDrop::new(Some(out_sample)),
            dwStatus: 0,
            pEvents: std::mem::ManuallyDrop::new(None),
        };

        let mut status = 0u32;
        let hr = self.transform.ProcessOutput(0, std::slice::from_mut(&mut output_data), &mut status);

        let mut bytes_written = 0usize;
        let mut is_key = force_keyframe;

        if hr.is_ok() {
            if let Some(ref sample) = *output_data.pSample {
                if let Ok(clean_point) = sample.GetUINT32(&MFSampleExtension_CleanPoint) {
                    is_key = clean_point != 0;
                }
                if let Ok(buf) = sample.GetBufferByIndex(0) {
                        let mut p_src = std::ptr::null_mut();
                        let mut cur_len = 0u32;
                        if buf.Lock(&mut p_src, None, Some(&mut cur_len)).is_ok() {
                            let to_copy = (cur_len as usize).min(out_h264.len());
                            std::ptr::copy_nonoverlapping(p_src, out_h264.as_mut_ptr(), to_copy);
                            bytes_written = to_copy;
                            let _ = buf.Unlock();
                        }
                }
            }
        }

        std::mem::ManuallyDrop::drop(&mut output_data.pSample);
        std::mem::ManuallyDrop::drop(&mut output_data.pEvents);

        let dur_ns = t_start.elapsed().as_nanos() as u64;
        Ok((bytes_written, is_key, dur_ns))
    }

    /// Dynamically adjust target bitrate in response to network congestion feedback.
    pub fn adjust_bitrate(&mut self, new_bitrate_kbps: u32) {
        self.bitrate_bps = new_bitrate_kbps.clamp(500, 30000) * 1000;
        if let Some(api) = self.codec_api.as_ref() {
            unsafe {
                let var_bitrate = VARIANT::from(self.bitrate_bps);
                let _ = api.SetValue(&CODECAPI_AVEncCommonMeanBitRate, &var_bitrate);
            }
        }
    }

    pub fn is_hardware_accelerated(&self) -> bool {
        self.is_hardware
    }

    pub fn is_zero_copy_surface(&self) -> bool {
        self.is_surface_aware
    }
}

impl Drop for MfH264Encoder {
    fn drop(&mut self) {
        unsafe {
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
            let _ = self.transform.ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
            let _ = MFShutdown();
        }
    }
}
