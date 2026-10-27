//! AetherCore: High-Performance Native Realtime Remote Desktop Engine.
//!
//! Licensed under MIT OR Apache-2.0 (Clean-room implementation, zero AGPL code).
//! Provides versioned, memory-safe C-ABI interface for DXGI capture, Media Foundation MFT H.264
//! encoding/decoding, WebRTC P2P transport, and real telemetry for both Host and Client.

pub mod dxgi_capture;
pub mod mf_encoder;
pub mod mf_decoder;
pub mod webrtc_transport;
pub mod telemetry;

use std::ffi::{c_char, c_void, CStr};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use dxgi_capture::DxgiDuplicator;
use mf_encoder::{EncoderConfig, MfH264Encoder};
use mf_decoder::MfH264Decoder;
use telemetry::{PipelineTelemetry, TelemetryTracker};
use webrtc_transport::{ice_state_to_code, IceConfig, WebRtcClientSession, WebRtcHostSession};

pub const AETHER_CORE_ABI_VERSION: u32 = 1;

pub const AETHER_OK: i32 = 0;
pub const AETHER_ERR_NULL_PTR: i32 = -1;
pub const AETHER_ERR_INVALID_PARAM: i32 = -2;
pub const AETHER_ERR_NOT_INITIALIZED: i32 = -3;
pub const AETHER_ERR_CAPTURE_FAILED: i32 = -4;
pub const AETHER_ERR_ENCODE_FAILED: i32 = -5;
pub const AETHER_ERR_WEBRTC_FAILED: i32 = -6;
pub const AETHER_ERR_BUFFER_TOO_SMALL: i32 = -7;
pub const AETHER_ERR_DECODE_FAILED: i32 = -8;

static ENGINE_INITIALIZED: AtomicBool = AtomicBool::new(false);

#[repr(C)]
pub struct AetherHostConfig {
    pub output_index: u32,
    pub target_bitrate_kbps: u32,
    pub fps: u32,
    pub stun_servers_csv: *const c_char,
    pub turn_server: *const c_char,
    pub turn_username: *const c_char,
    pub turn_credential: *const c_char,
}

#[repr(C)]
pub struct AetherClientConfig {
    pub stun_servers_csv: *const c_char,
    pub turn_server: *const c_char,
    pub turn_username: *const c_char,
    pub turn_credential: *const c_char,
}

pub struct AetherHostSession {
    pub runtime: tokio::runtime::Runtime,
    pub duplicator: DxgiDuplicator,
    pub encoder: MfH264Encoder,
    pub webrtc: WebRtcHostSession,
    pub telemetry: TelemetryTracker,
    pub raw_frame_buffer: Vec<u8>,
    pub h264_buffer: Vec<u8>,
    pub frame_counter: u64,
    pub lock: Mutex<()>,
}

pub struct AetherClientSession {
    pub runtime: tokio::runtime::Runtime,
    pub decoder: MfH264Decoder,
    pub webrtc: WebRtcClientSession,
    pub telemetry: TelemetryTracker,
    pub raw_rgb_buffer: Vec<u8>,
    pub lock: Mutex<()>,
}

#[no_mangle]
pub extern "C" fn aether_core_abi_version() -> u32 {
    AETHER_CORE_ABI_VERSION
}

#[no_mangle]
pub extern "C" fn aether_core_init() -> i32 {
    ENGINE_INITIALIZED.store(true, Ordering::SeqCst);
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_core_shutdown() -> i32 {
    ENGINE_INITIALIZED.store(false, Ordering::SeqCst);
    AETHER_OK
}

fn parse_ice_config(
    stun_csv: *const c_char,
    turn_server: *const c_char,
    turn_user: *const c_char,
    turn_cred: *const c_char,
) -> IceConfig {
    let mut stun_servers = Vec::new();
    if !stun_csv.is_null() {
        if let Ok(csv) = unsafe { CStr::from_ptr(stun_csv).to_str() } {
            for s in csv.split(',') {
                let trimmed = s.trim();
                if !trimmed.is_empty() {
                    stun_servers.push(trimmed.to_string());
                }
            }
        }
    }

    let turn_url = if !turn_server.is_null() {
        unsafe { CStr::from_ptr(turn_server).to_str().ok().map(|s| s.to_string()) }
    } else {
        None
    };

    let turn_username = if !turn_user.is_null() {
        unsafe { CStr::from_ptr(turn_user).to_str().ok().map(|s| s.to_string()) }
    } else {
        None
    };

    let turn_credential = if !turn_cred.is_null() {
        unsafe { CStr::from_ptr(turn_cred).to_str().ok().map(|s| s.to_string()) }
    } else {
        None
    };

    IceConfig {
        stun_servers,
        turn_server: turn_url,
        turn_username,
        turn_credential,
    }
}

// =============================================================================
// HOST C-ABI EXPORTS
// =============================================================================

#[no_mangle]
pub extern "C" fn aether_host_create(
    config: *const AetherHostConfig,
    handle_out: *mut *mut c_void,
) -> i32 {
    if config.is_null() || handle_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }

    let cfg = unsafe { &*config };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(_) => return AETHER_ERR_WEBRTC_FAILED,
    };

    let duplicator = match DxgiDuplicator::new(cfg.output_index) {
        Ok(dup) => dup,
        Err(_) => return AETHER_ERR_CAPTURE_FAILED,
    };

    let width = duplicator.width;
    let height = duplicator.height;

    let enc_cfg = EncoderConfig {
        width,
        height,
        target_bitrate_kbps: cfg.target_bitrate_kbps,
        fps: cfg.fps,
    };
    let encoder = match MfH264Encoder::new(enc_cfg) {
        Ok(enc) => enc,
        Err(_) => return AETHER_ERR_ENCODE_FAILED,
    };

    let is_hw = encoder.is_hardware_accelerated();
    let telemetry = TelemetryTracker::new(is_hw);
    telemetry.set_bitrate(cfg.target_bitrate_kbps);

    let ice_config = parse_ice_config(
        cfg.stun_servers_csv,
        cfg.turn_server,
        cfg.turn_username,
        cfg.turn_credential,
    );

    let webrtc = match runtime.block_on(WebRtcHostSession::create(ice_config)) {
        Ok(session) => session,
        Err(_) => return AETHER_ERR_WEBRTC_FAILED,
    };

    let raw_buf_size = (width * height * 4) as usize;
    let h264_buf_size = raw_buf_size;

    let session = Box::new(AetherHostSession {
        runtime,
        duplicator,
        encoder,
        webrtc,
        telemetry,
        raw_frame_buffer: vec![0u8; raw_buf_size],
        h264_buffer: vec![0u8; h264_buf_size],
        frame_counter: 0,
        lock: Mutex::new(()),
    });

    unsafe {
        *handle_out = Box::into_raw(session) as *mut c_void;
    }
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_host_create_offer(
    handle: *mut c_void,
    sdp_out: *mut c_char,
    sdp_max_len: usize,
    sdp_len_out: *mut usize,
) -> i32 {
    if handle.is_null() || sdp_out.is_null() || sdp_len_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    let _guard = session.lock.lock().unwrap();

    match session.runtime.block_on(session.webrtc.create_offer()) {
        Ok(offer_sdp) => {
            let bytes = offer_sdp.as_bytes();
            if bytes.len() + 1 > sdp_max_len {
                return AETHER_ERR_BUFFER_TOO_SMALL;
            }
            unsafe {
                std::ptr::copy_nonoverlapping(bytes.as_ptr(), sdp_out as *mut u8, bytes.len());
                *sdp_out.add(bytes.len()) = 0;
                *sdp_len_out = bytes.len();
            }
            AETHER_OK
        }
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_host_set_answer(
    handle: *mut c_void,
    sdp_answer: *const c_char,
) -> i32 {
    if handle.is_null() || sdp_answer.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    let _guard = session.lock.lock().unwrap();

    let sdp_str = match unsafe { CStr::from_ptr(sdp_answer).to_str() } {
        Ok(s) => s,
        Err(_) => return AETHER_ERR_INVALID_PARAM,
    };

    match session.runtime.block_on(session.webrtc.set_remote_answer(sdp_str)) {
        Ok(_) => AETHER_OK,
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_host_add_ice_candidate(
    handle: *mut c_void,
    candidate_json: *const c_char,
) -> i32 {
    if handle.is_null() || candidate_json.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    let _guard = session.lock.lock().unwrap();

    let json_str = match unsafe { CStr::from_ptr(candidate_json).to_str() } {
        Ok(s) => s,
        Err(_) => return AETHER_ERR_INVALID_PARAM,
    };

    match session.runtime.block_on(session.webrtc.add_remote_ice_candidate(json_str)) {
        Ok(_) => AETHER_OK,
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_host_poll_ice_candidate(
    handle: *mut c_void,
    cand_out: *mut c_char,
    cand_max_len: usize,
    cand_len_out: *mut usize,
) -> i32 {
    if handle.is_null() || cand_out.is_null() || cand_len_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    let _guard = session.lock.lock().unwrap();

    if let Some(cand_json) = session.runtime.block_on(session.webrtc.poll_outgoing_ice_candidate()) {
        let bytes = cand_json.as_bytes();
        if bytes.len() + 1 > cand_max_len {
            return AETHER_ERR_BUFFER_TOO_SMALL;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), cand_out as *mut u8, bytes.len());
            *cand_out.add(bytes.len()) = 0;
            *cand_len_out = bytes.len();
        }
        AETHER_OK
    } else {
        unsafe {
            *cand_len_out = 0;
        }
        AETHER_OK
    }
}

#[no_mangle]
pub extern "C" fn aether_host_get_ice_state(handle: *mut c_void) -> i32 {
    if handle.is_null() {
        return -1;
    }
    let session = unsafe { &*(handle as *const AetherHostSession) };
    ice_state_to_code(session.webrtc.get_ice_connection_state())
}

/// Executes one single step of the capture -> encode -> transmit pipeline.
/// Uses D3D11 surface-aware GPU path when supported, avoiding CPU readback.
#[no_mangle]
pub extern "C" fn aether_host_step_pipeline(
    handle: *mut c_void,
    force_keyframe: i32,
) -> i32 {
    if handle.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    let _guard = session.lock.lock().unwrap();

    // 1. Capture screen via DXGI Desktop Duplication
    let (has_frame, meta) = match session.duplicator.acquire_frame(16) {
        Ok(res) => res,
        Err(_) => return AETHER_ERR_CAPTURE_FAILED,
    };

    if !has_frame {
        return AETHER_OK;
    }
    session.telemetry.record_capture(meta.duration_ns);

    let is_key = force_keyframe != 0 || (session.frame_counter % 90 == 0);
    session.frame_counter += 1;

    // 2. Hardware surface encoding vs CPU readback fallback
    let mut encode_res = Err("Unattempted".to_string());
    if session.encoder.is_hardware_accelerated() {
        if let Some(tex) = session.duplicator.get_working_texture() {
            encode_res = session.encoder.encode_d3d11_texture(tex, is_key, &mut session.h264_buffer);
            if encode_res.is_ok() {
                session.telemetry.set_zero_copy_flag(true);
            }
        }
    }

    // Software fallback if hardware encoding failed or unsupported
    if encode_res.is_err() {
        if let Err(_) = session.duplicator.read_pixels_bgra(&mut session.raw_frame_buffer) {
            return AETHER_ERR_CAPTURE_FAILED;
        }
        session.telemetry.set_zero_copy_flag(false);
        encode_res = session.encoder.encode_memory_buffer(&session.raw_frame_buffer, is_key, &mut session.h264_buffer);
    }

    let (bytes_encoded, _, enc_dur_ns) = match encode_res {
        Ok(res) => res,
        Err(_) => return AETHER_ERR_ENCODE_FAILED,
    };
    session.telemetry.record_encode(enc_dur_ns);

    // 3. Send H.264 NALU sample over WebRTC video track
    if bytes_encoded > 0 {
        let t_queue_start = Instant::now();
        let payload = &session.h264_buffer[0..bytes_encoded];
        if let Err(_) = session.runtime.block_on(session.webrtc.write_video_sample(payload, 16)) {
            return AETHER_ERR_WEBRTC_FAILED;
        }
        let queue_dur_ns = t_queue_start.elapsed().as_nanos() as u64;
        session.telemetry.record_queue(queue_dur_ns);
    }

    session.telemetry.tick_frame();
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_host_poll_cursor(
    handle: *mut c_void,
    x_out: *mut i32,
    y_out: *mut i32,
    seq_out: *mut u64,
) -> i32 {
    if handle.is_null() || x_out.is_null() || y_out.is_null() || seq_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    if let Some((x, y, seq)) = session.runtime.block_on(session.webrtc.poll_cursor_position()) {
        unsafe {
            *x_out = x;
            *y_out = y;
            *seq_out = seq;
        }
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn aether_host_poll_control(
    handle: *mut c_void,
    buf_out: *mut u8,
    buf_max_len: usize,
    len_out: *mut usize,
) -> i32 {
    if handle.is_null() || buf_out.is_null() || len_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    if let Some(msg_bytes) = session.runtime.block_on(session.webrtc.poll_control_message()) {
        if msg_bytes.len() > buf_max_len {
            return AETHER_ERR_BUFFER_TOO_SMALL;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(msg_bytes.as_ptr(), buf_out, msg_bytes.len());
            *len_out = msg_bytes.len();
        }
        1
    } else {
        unsafe {
            *len_out = 0;
        }
        0
    }
}

#[no_mangle]
pub extern "C" fn aether_host_is_control_saturated(handle: *mut c_void) -> i32 {
    if handle.is_null() {
        return 0;
    }
    let session = unsafe { &*(handle as *const AetherHostSession) };
    if session.webrtc.is_control_saturated() {
        1
    } else {
        0
    }
}

#[no_mangle]
pub extern "C" fn aether_host_adjust_bitrate(handle: *mut c_void, bitrate_kbps: u32) -> i32 {
    if handle.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherHostSession) };
    let _guard = session.lock.lock().unwrap();
    session.encoder.adjust_bitrate(bitrate_kbps);
    session.telemetry.set_bitrate(bitrate_kbps);
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_host_get_telemetry(
    handle: *mut c_void,
    telemetry_out: *mut PipelineTelemetry,
) -> i32 {
    if handle.is_null() || telemetry_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &*(handle as *const AetherHostSession) };
    unsafe {
        *telemetry_out = session.telemetry.snapshot();
    }
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_host_destroy(handle: *mut c_void) -> i32 {
    if !handle.is_null() {
        unsafe {
            let session = Box::from_raw(handle as *mut AetherHostSession);
            let _ = session.runtime.block_on(session.webrtc.close());
            drop(session);
        }
    }
    AETHER_OK
}

// =============================================================================
// CLIENT C-ABI EXPORTS
// =============================================================================

#[no_mangle]
pub extern "C" fn aether_client_create(
    config: *const AetherClientConfig,
    handle_out: *mut *mut c_void,
) -> i32 {
    if config.is_null() || handle_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }

    let cfg = unsafe { &*config };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt,
        Err(_) => return AETHER_ERR_WEBRTC_FAILED,
    };

    let decoder = match MfH264Decoder::new() {
        Ok(dec) => dec,
        Err(_) => return AETHER_ERR_DECODE_FAILED,
    };

    let ice_config = parse_ice_config(
        cfg.stun_servers_csv,
        cfg.turn_server,
        cfg.turn_username,
        cfg.turn_credential,
    );

    let webrtc = match runtime.block_on(WebRtcClientSession::create(ice_config)) {
        Ok(session) => session,
        Err(_) => return AETHER_ERR_WEBRTC_FAILED,
    };

    let raw_buf_size = 3840 * 2160 * 4;
    let telemetry = TelemetryTracker::new(false);

    let session = Box::new(AetherClientSession {
        runtime,
        decoder,
        webrtc,
        telemetry,
        raw_rgb_buffer: vec![0u8; raw_buf_size],
        lock: Mutex::new(()),
    });

    unsafe {
        *handle_out = Box::into_raw(session) as *mut c_void;
    }
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_client_set_offer(
    handle: *mut c_void,
    sdp_offer: *const c_char,
) -> i32 {
    if handle.is_null() || sdp_offer.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    let _guard = session.lock.lock().unwrap();

    let sdp_str = match unsafe { CStr::from_ptr(sdp_offer).to_str() } {
        Ok(s) => s,
        Err(_) => return AETHER_ERR_INVALID_PARAM,
    };

    match session.runtime.block_on(session.webrtc.set_remote_offer(sdp_str)) {
        Ok(_) => AETHER_OK,
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_client_create_answer(
    handle: *mut c_void,
    sdp_out: *mut c_char,
    sdp_max_len: usize,
    sdp_len_out: *mut usize,
) -> i32 {
    if handle.is_null() || sdp_out.is_null() || sdp_len_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    let _guard = session.lock.lock().unwrap();

    match session.runtime.block_on(session.webrtc.create_answer()) {
        Ok(ans_sdp) => {
            let bytes = ans_sdp.as_bytes();
            if bytes.len() + 1 > sdp_max_len {
                return AETHER_ERR_BUFFER_TOO_SMALL;
            }
            unsafe {
                std::ptr::copy_nonoverlapping(bytes.as_ptr(), sdp_out as *mut u8, bytes.len());
                *sdp_out.add(bytes.len()) = 0;
                *sdp_len_out = bytes.len();
            }
            AETHER_OK
        }
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_client_add_ice_candidate(
    handle: *mut c_void,
    candidate_json: *const c_char,
) -> i32 {
    if handle.is_null() || candidate_json.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    let _guard = session.lock.lock().unwrap();

    let json_str = match unsafe { CStr::from_ptr(candidate_json).to_str() } {
        Ok(s) => s,
        Err(_) => return AETHER_ERR_INVALID_PARAM,
    };

    match session.runtime.block_on(session.webrtc.add_remote_ice_candidate(json_str)) {
        Ok(_) => AETHER_OK,
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_client_poll_ice_candidate(
    handle: *mut c_void,
    cand_out: *mut c_char,
    cand_max_len: usize,
    cand_len_out: *mut usize,
) -> i32 {
    if handle.is_null() || cand_out.is_null() || cand_len_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    let _guard = session.lock.lock().unwrap();

    if let Some(cand_json) = session.runtime.block_on(session.webrtc.poll_outgoing_ice_candidate()) {
        let bytes = cand_json.as_bytes();
        if bytes.len() + 1 > cand_max_len {
            return AETHER_ERR_BUFFER_TOO_SMALL;
        }
        unsafe {
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), cand_out as *mut u8, bytes.len());
            *cand_out.add(bytes.len()) = 0;
            *cand_len_out = bytes.len();
        }
        AETHER_OK
    } else {
        unsafe {
            *cand_len_out = 0;
        }
        AETHER_OK
    }
}

#[no_mangle]
pub extern "C" fn aether_client_get_ice_state(handle: *mut c_void) -> i32 {
    if handle.is_null() {
        return -1;
    }
    let session = unsafe { &*(handle as *const AetherClientSession) };
    ice_state_to_code(session.webrtc.get_ice_connection_state())
}

#[no_mangle]
pub extern "C" fn aether_client_poll_video_frame(
    handle: *mut c_void,
    out_rgba: *mut u8,
    max_len: usize,
    len_out: *mut usize,
    width_out: *mut u32,
    height_out: *mut u32,
    decode_ns_out: *mut u64,
) -> i32 {
    if handle.is_null() || out_rgba.is_null() || len_out.is_null() || width_out.is_null() || height_out.is_null() || decode_ns_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    let _guard = session.lock.lock().unwrap();

    // Check if depacketizer detected packet loss or queue eviction and requires an IDR keyframe from host
    if session.webrtc.take_keyframe_request() {
        session.decoder.flush();
        let _ = session.runtime.block_on(session.webrtc.send_control(br#"{"type":"KEYFRAME_REQ"}"#));
    }

    if let Some(h264_frame) = session.runtime.block_on(session.webrtc.poll_video_frame()) {
        let slice = unsafe { std::slice::from_raw_parts_mut(out_rgba, max_len) };
        match session.decoder.decode_frame(&h264_frame, slice) {
            Ok((written, w, h, dur_ns)) => {
                if written > 0 {
                    session.telemetry.record_decode(dur_ns);
                    unsafe {
                        *len_out = written;
                        *width_out = w;
                        *height_out = h;
                        *decode_ns_out = dur_ns;
                    }
                    1 // Frame successfully decoded
                } else {
                    unsafe {
                        *len_out = 0;
                    }
                    0 // Need more input / partial slice
                }
            }
            Err(_) => {
                // Decode error: flush decoder to reset state and request clean IDR keyframe
                session.decoder.flush();
                let _ = session.runtime.block_on(session.webrtc.send_control(br#"{"type":"KEYFRAME_REQ"}"#));
                AETHER_ERR_DECODE_FAILED
            }
        }
    } else {
        unsafe {
            *len_out = 0;
        }
        0 // No frame available in queue
    }
}

#[no_mangle]
pub extern "C" fn aether_client_send_cursor(
    handle: *mut c_void,
    x: i32,
    y: i32,
    seq: u64,
) -> i32 {
    if handle.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    match session.runtime.block_on(session.webrtc.send_cursor(x, y, seq)) {
        Ok(_) => AETHER_OK,
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_client_send_control(
    handle: *mut c_void,
    data: *const u8,
    len: usize,
) -> i32 {
    if handle.is_null() || data.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &mut *(handle as *mut AetherClientSession) };
    let slice = unsafe { std::slice::from_raw_parts(data, len) };
    match session.runtime.block_on(session.webrtc.send_control(slice)) {
        Ok(_) => AETHER_OK,
        Err(_) => AETHER_ERR_WEBRTC_FAILED,
    }
}

#[no_mangle]
pub extern "C" fn aether_client_get_telemetry(
    handle: *mut c_void,
    telemetry_out: *mut PipelineTelemetry,
) -> i32 {
    if handle.is_null() || telemetry_out.is_null() {
        return AETHER_ERR_NULL_PTR;
    }
    let session = unsafe { &*(handle as *const AetherClientSession) };
    unsafe {
        *telemetry_out = session.telemetry.snapshot();
    }
    AETHER_OK
}

#[no_mangle]
pub extern "C" fn aether_client_destroy(handle: *mut c_void) -> i32 {
    if !handle.is_null() {
        unsafe {
            let mut session = Box::from_raw(handle as *mut AetherClientSession);
            let _ = session.runtime.block_on(session.webrtc.close());
            session.decoder.flush();
            drop(session);
        }
    }
    AETHER_OK
}
