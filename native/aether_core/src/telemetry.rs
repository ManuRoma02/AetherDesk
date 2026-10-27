//! Real-time telemetry tracking and performance metrics for AetherCore.
//! Measures actual stage-by-stage latencies with monotonic clocks. Zero fake numbers.
//! Unmeasured values are explicitly flagged so they can be exposed as null/N/A.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::time::Instant;

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct PipelineTelemetry {
    pub capture_ns: u64,
    pub encode_ns: u64,
    pub queue_ns: u64,
    pub network_rtt_ns: u64,
    pub clock_offset_ns: i64,
    pub decode_ns: u64,
    pub render_ns: u64,
    pub fps: f32,
    pub bitrate_kbps: u32,
    pub is_hardware_encoder: u32,
    pub is_zero_copy: u32,
    pub has_capture: u32,
    pub has_encode: u32,
    pub has_queue: u32,
    pub has_rtt: u32,
    pub has_clock_offset: u32,
    pub has_decode: u32,
    pub has_render: u32,
    pub has_fps: u32,
}

pub struct TelemetryTracker {
    last_capture_ns: AtomicU64,
    has_capture: AtomicBool,
    last_encode_ns: AtomicU64,
    has_encode: AtomicBool,
    last_queue_ns: AtomicU64,
    has_queue: AtomicBool,
    network_rtt_ns: AtomicU64,
    has_rtt: AtomicBool,
    clock_offset_ns: AtomicU64,
    has_clock_offset: AtomicBool,
    last_decode_ns: AtomicU64,
    has_decode: AtomicBool,
    last_render_ns: AtomicU64,
    has_render: AtomicBool,
    frame_count: AtomicU32,
    last_fps_calc: std::sync::Mutex<Instant>,
    current_fps: std::sync::Mutex<f32>,
    has_fps: AtomicBool,
    current_bitrate_kbps: AtomicU32,
    is_hardware_mft: AtomicU32,
    is_zero_copy: AtomicU32,
}

impl TelemetryTracker {
    pub fn new(is_hardware: bool) -> Self {
        Self {
            last_capture_ns: AtomicU64::new(0),
            has_capture: AtomicBool::new(false),
            last_encode_ns: AtomicU64::new(0),
            has_encode: AtomicBool::new(false),
            last_queue_ns: AtomicU64::new(0),
            has_queue: AtomicBool::new(false),
            network_rtt_ns: AtomicU64::new(0),
            has_rtt: AtomicBool::new(false),
            clock_offset_ns: AtomicU64::new(0),
            has_clock_offset: AtomicBool::new(false),
            last_decode_ns: AtomicU64::new(0),
            has_decode: AtomicBool::new(false),
            last_render_ns: AtomicU64::new(0),
            has_render: AtomicBool::new(false),
            frame_count: AtomicU32::new(0),
            last_fps_calc: std::sync::Mutex::new(Instant::now()),
            current_fps: std::sync::Mutex::new(0.0),
            has_fps: AtomicBool::new(false),
            current_bitrate_kbps: AtomicU32::new(0),
            is_hardware_mft: AtomicU32::new(if is_hardware { 1 } else { 0 }),
            is_zero_copy: AtomicU32::new(0),
        }
    }

    pub fn record_capture(&self, duration_ns: u64) {
        self.last_capture_ns.store(duration_ns, Ordering::Relaxed);
        self.has_capture.store(true, Ordering::Relaxed);
    }

    pub fn record_encode(&self, duration_ns: u64) {
        self.last_encode_ns.store(duration_ns, Ordering::Relaxed);
        self.has_encode.store(true, Ordering::Relaxed);
    }

    pub fn record_queue(&self, duration_ns: u64) {
        self.last_queue_ns.store(duration_ns, Ordering::Relaxed);
        self.has_queue.store(true, Ordering::Relaxed);
    }

    pub fn record_rtt(&self, rtt_ns: u64) {
        self.network_rtt_ns.store(rtt_ns, Ordering::Relaxed);
        self.has_rtt.store(true, Ordering::Relaxed);
    }

    pub fn record_clock_offset(&self, offset_ns: i64) {
        self.clock_offset_ns.store(offset_ns as u64, Ordering::Relaxed);
        self.has_clock_offset.store(true, Ordering::Relaxed);
    }

    pub fn record_decode(&self, duration_ns: u64) {
        self.last_decode_ns.store(duration_ns, Ordering::Relaxed);
        self.has_decode.store(true, Ordering::Relaxed);
    }

    pub fn record_render(&self, duration_ns: u64) {
        self.last_render_ns.store(duration_ns, Ordering::Relaxed);
        self.has_render.store(true, Ordering::Relaxed);
    }

    pub fn set_bitrate(&self, kbps: u32) {
        self.current_bitrate_kbps.store(kbps, Ordering::Relaxed);
    }

    pub fn set_hardware_flag(&self, is_hw: bool) {
        self.is_hardware_mft.store(if is_hw { 1 } else { 0 }, Ordering::Relaxed);
    }

    pub fn set_zero_copy_flag(&self, zero_copy: bool) {
        self.is_zero_copy.store(if zero_copy { 1 } else { 0 }, Ordering::Relaxed);
    }

    pub fn tick_frame(&self) {
        let count = self.frame_count.fetch_add(1, Ordering::Relaxed) + 1;
        if count >= 30 {
            if let Ok(mut last) = self.last_fps_calc.lock() {
                let elapsed = last.elapsed().as_secs_f32();
                if elapsed > 0.0 {
                    if let Ok(mut fps_slot) = self.current_fps.lock() {
                        *fps_slot = (count as f32) / elapsed;
                        self.has_fps.store(true, Ordering::Relaxed);
                    }
                    self.frame_count.store(0, Ordering::Relaxed);
                    *last = Instant::now();
                }
            }
        }
    }

    pub fn snapshot(&self) -> PipelineTelemetry {
        let fps = self.current_fps.lock().map(|f| *f).unwrap_or(0.0);
        PipelineTelemetry {
            capture_ns: self.last_capture_ns.load(Ordering::Relaxed),
            encode_ns: self.last_encode_ns.load(Ordering::Relaxed),
            queue_ns: self.last_queue_ns.load(Ordering::Relaxed),
            network_rtt_ns: self.network_rtt_ns.load(Ordering::Relaxed),
            clock_offset_ns: self.clock_offset_ns.load(Ordering::Relaxed) as i64,
            decode_ns: self.last_decode_ns.load(Ordering::Relaxed),
            render_ns: self.last_render_ns.load(Ordering::Relaxed),
            fps,
            bitrate_kbps: self.current_bitrate_kbps.load(Ordering::Relaxed),
            is_hardware_encoder: self.is_hardware_mft.load(Ordering::Relaxed),
            is_zero_copy: self.is_zero_copy.load(Ordering::Relaxed),
            has_capture: if self.has_capture.load(Ordering::Relaxed) { 1 } else { 0 },
            has_encode: if self.has_encode.load(Ordering::Relaxed) { 1 } else { 0 },
            has_queue: if self.has_queue.load(Ordering::Relaxed) { 1 } else { 0 },
            has_rtt: if self.has_rtt.load(Ordering::Relaxed) { 1 } else { 0 },
            has_clock_offset: if self.has_clock_offset.load(Ordering::Relaxed) { 1 } else { 0 },
            has_decode: if self.has_decode.load(Ordering::Relaxed) { 1 } else { 0 },
            has_render: if self.has_render.load(Ordering::Relaxed) { 1 } else { 0 },
            has_fps: if self.has_fps.load(Ordering::Relaxed) { 1 } else { 0 },
        }
    }
}
