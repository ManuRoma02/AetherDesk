//! Windows DXGI Desktop Duplication capture with GPU texture retention & dirty rects metadata.
//!
//! Provides zero-copy GPU texture retention for hardware encoders and staging CPU readback
//! fallback for software encoders. Implements robust handling for DXGI_ERROR_ACCESS_LOST,
//! resolution changes, and display topology updates.

use std::time::Instant;
use windows::Win32::Graphics::Dxgi::*;
use windows::Win32::Graphics::Dxgi::Common::*;
use windows::Win32::Graphics::Direct3D::*;
use windows::Win32::Graphics::Direct3D11::*;
use windows::core::Interface;

#[repr(C)]
#[derive(Default, Clone, Copy, Debug)]
pub struct FrameMetadata {
    pub width: u32,
    pub height: u32,
    pub cursor_x: i32,
    pub cursor_y: i32,
    pub cursor_visible: bool,
    pub dirty_rects_count: u32,
    pub accumulated_frames: u32,
    pub duration_ns: u64,
}

pub struct DxgiDuplicator {
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    duplication: Option<IDXGIOutputDuplication>,
    working_texture: Option<ID3D11Texture2D>,
    staging_texture: Option<ID3D11Texture2D>,
    output_index: u32,
    pub width: u32,
    pub height: u32,
}

impl DxgiDuplicator {
    pub fn new(output_index: u32) -> Result<Self, String> {
        unsafe {
            let mut device = None;
            let mut context = None;
            let feature_levels = [
                D3D_FEATURE_LEVEL_11_1,
                D3D_FEATURE_LEVEL_11_0,
                D3D_FEATURE_LEVEL_10_1,
            ];
            let flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;

            D3D11CreateDevice(
                None,
                D3D_DRIVER_TYPE_HARDWARE,
                None,
                flags,
                Some(&feature_levels),
                D3D11_SDK_VERSION,
                Some(&mut device),
                None,
                Some(&mut context),
            ).map_err(|e| format!("D3D11CreateDevice failed: {}", e))?;

            let device = device.ok_or_else(|| "D3D11Device pointer is null".to_string())?;
            let context = context.ok_or_else(|| "D3D11DeviceContext pointer is null".to_string())?;

            let mut duplicator = Self {
                device,
                context,
                duplication: None,
                working_texture: None,
                staging_texture: None,
                output_index,
                width: 0,
                height: 0,
            };

            duplicator.init_duplication()?;
            Ok(duplicator)
        }
    }

    /// Re-initialize DXGI output duplication (called on startup or DXGI_ERROR_ACCESS_LOST).
    pub fn init_duplication(&mut self) -> Result<(), String> {
        unsafe {
            self.duplication = None;
            self.working_texture = None;
            self.staging_texture = None;

            let dxgi_device: IDXGIDevice = self.device.cast().map_err(|e| e.to_string())?;
            let adapter = dxgi_device.GetAdapter().map_err(|e| e.to_string())?;
            let output = adapter.EnumOutputs(self.output_index).map_err(|e| e.to_string())?;
            let output1: IDXGIOutput1 = output.cast().map_err(|e| e.to_string())?;

            let desc = output.GetDesc().map_err(|e| e.to_string())?;
            self.width = (desc.DesktopCoordinates.right - desc.DesktopCoordinates.left).unsigned_abs();
            self.height = (desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top).unsigned_abs();

            if self.width == 0 || self.height == 0 {
                return Err("Invalid desktop dimensions (0x0)".to_string());
            }

            let duplication = output1.DuplicateOutput(&self.device).map_err(|e| {
                format!("IDXGIOutput1::DuplicateOutput failed (HRESULT: {}): {}", e.code(), e)
            })?;

            // 1. Working GPU texture (for retaining frame while encoder is consuming it)
            let tex_desc = D3D11_TEXTURE2D_DESC {
                Width: self.width,
                Height: self.height,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: (D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE).0 as u32,
                CPUAccessFlags: 0,
                MiscFlags: 0,
            };
            let mut working_tex = None;
            self.device.CreateTexture2D(&tex_desc, None, Some(&mut working_tex))
                .map_err(|e| format!("CreateTexture2D (working) failed: {}", e))?;

            // 2. CPU Staging texture (for software MFT readback fallback)
            let staging_desc = D3D11_TEXTURE2D_DESC {
                Width: self.width,
                Height: self.height,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_STAGING,
                BindFlags: 0,
                CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
                MiscFlags: 0,
            };
            let mut staging_tex = None;
            self.device.CreateTexture2D(&staging_desc, None, Some(&mut staging_tex))
                .map_err(|e| format!("CreateTexture2D (staging) failed: {}", e))?;

            self.duplication = Some(duplication);
            self.working_texture = working_tex;
            self.staging_texture = staging_tex;

            Ok(())
        }
    }

    /// Acquires next desktop frame, copies to internal working GPU texture, and releases DXGI frame immediately.
    /// This guarantees that the GPU texture remains completely owned and valid during MFT encoding.
    pub fn acquire_frame(&mut self, timeout_ms: u32) -> Result<(bool, FrameMetadata), String> {
        let t_start = Instant::now();
        if self.duplication.is_none() {
            self.init_duplication()?;
        }

        let dup = self.duplication.as_ref().ok_or_else(|| "Duplication not initialized".to_string())?;
        unsafe {
            let mut frame_info = DXGI_OUTDUPL_FRAME_INFO::default();
            let mut resource = None;

            let hr = dup.AcquireNextFrame(timeout_ms, &mut frame_info, &mut resource);
            if hr.as_ref().err().map(|e| e.code()) == Some(DXGI_ERROR_WAIT_TIMEOUT) {
                return Ok((false, FrameMetadata::default()));
            }
            if hr.as_ref().err().map(|e| e.code()) == Some(DXGI_ERROR_ACCESS_LOST) {
                // Desktop switch / UAC / resolution change: recreate duplication next time
                self.duplication = None;
                return Ok((false, FrameMetadata::default()));
            }
            if let Err(e) = hr {
                return Err(format!("AcquireNextFrame error: {}", e));
            }

            let resource = resource.ok_or_else(|| "Acquired resource is null".to_string())?;
            let desktop_texture: ID3D11Texture2D = resource.cast().map_err(|e| e.to_string())?;

            if let Some(ref working_tex) = self.working_texture {
                // Copy frame to our internal retained GPU texture
                self.context.CopyResource(working_tex, &desktop_texture);
            }

            // CRITICAL: Release frame back to DXGI compositor AFTER copying to retained working texture.
            // This prevents compositor stalls while keeping texture memory valid for the encoder.
            let _ = dup.ReleaseFrame();

            let dur_ns = t_start.elapsed().as_nanos() as u64;
            let meta = FrameMetadata {
                width: self.width,
                height: self.height,
                cursor_x: frame_info.PointerPosition.Position.x,
                cursor_y: frame_info.PointerPosition.Position.y,
                cursor_visible: frame_info.PointerPosition.Visible.as_bool(),
                dirty_rects_count: frame_info.TotalMetadataBufferSize,
                accumulated_frames: frame_info.AccumulatedFrames,
                duration_ns: dur_ns,
            };

            Ok((true, meta))
        }
    }

    /// Copies retained working GPU texture to CPU staging memory (used for software MFT fallback).
    pub fn read_pixels_bgra(&self, out_buf: &mut [u8]) -> Result<usize, String> {
        let expected_size = (self.width * self.height * 4) as usize;
        if out_buf.len() < expected_size {
            return Err(format!("Output buffer too small: {} < {}", out_buf.len(), expected_size));
        }

        let working_tex = self.working_texture.as_ref().ok_or_else(|| "Working texture null".to_string())?;
        let staging_tex = self.staging_texture.as_ref().ok_or_else(|| "Staging texture null".to_string())?;

        unsafe {
            self.context.CopyResource(staging_tex, working_tex);

            let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
            self.context.Map(staging_tex, 0, D3D11_MAP_READ, 0, Some(&mut mapped))
                .map_err(|e| format!("Map staging texture failed: {}", e))?;

            let src_pitch = mapped.RowPitch as usize;
            let row_bytes = (self.width * 4) as usize;
            let src_ptr = mapped.pData as *const u8;

            for y in 0..self.height as usize {
                let src_row = src_ptr.add(y * src_pitch);
                let dst_row = &mut out_buf[y * row_bytes..(y + 1) * row_bytes];
                std::ptr::copy_nonoverlapping(src_row, dst_row.as_mut_ptr(), row_bytes);
            }

            self.context.Unmap(staging_tex, 0);
            Ok(expected_size)
        }
    }

    pub fn get_d3d_device(&self) -> &ID3D11Device {
        &self.device
    }

    pub fn get_working_texture(&self) -> Option<&ID3D11Texture2D> {
        self.working_texture.as_ref()
    }
}
