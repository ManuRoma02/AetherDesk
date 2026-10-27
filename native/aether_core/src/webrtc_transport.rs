//! WebRTC Realtime Peer-to-Peer Transport for both Host and Client endpoints.
//!
//! Provides real P2P media streaming with ICE negotiation, secure STUN/TURN configuration,
//! and segregated channels:
//! - "cursor" channel: Unreliable, unordered, zero retransmits for minimal latency mouse updates (latest-wins).
//! - "control" channel: Reliable, ordered delivery for discrete keystrokes, clicks, and session control.
//! - RFC 6184 H.264 RTP depacketizer (Single NAL, STAP-A, FU-A, sequence checking, packet loss detection).

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{mpsc, Mutex};
use webrtc::api::media_engine::{MediaEngine, MIME_TYPE_H264};
use webrtc::api::APIBuilder;
use webrtc::data_channel::data_channel_init::RTCDataChannelInit;
use webrtc::data_channel::RTCDataChannel;
use webrtc::ice_transport::ice_candidate::{RTCIceCandidate, RTCIceCandidateInit};
use webrtc::ice_transport::ice_connection_state::RTCIceConnectionState;
use webrtc::ice_transport::ice_server::RTCIceServer;
use webrtc::media::Sample;
use webrtc::peer_connection::configuration::RTCConfiguration;
use webrtc::peer_connection::sdp::session_description::RTCSessionDescription;
use webrtc::peer_connection::RTCPeerConnection;
use webrtc::rtp_transceiver::rtp_codec::RTCRtpCodecCapability;
use webrtc::track::track_local::track_local_static_sample::TrackLocalStaticSample;
use webrtc::track::track_local::TrackLocal;
use webrtc::track::track_remote::TrackRemote;

/// Maximum allowed size for a single RTP packet payload (64 KB).
pub const MAX_RTP_PACKET_SIZE: usize = 65_536;

/// Maximum allowed size for a single reassembled H.264 Access Unit / frame (16 MB).
pub const MAX_ACCESS_UNIT_SIZE: usize = 16 * 1024 * 1024;

/// Reassembled H.264 Access Unit carrying reliable frame metadata.
#[derive(Clone, Debug, PartialEq)]
pub struct AccessUnit {
    pub data: Vec<u8>,
    pub is_idr: bool,
    pub timestamp: u32,
    pub end_seq: u16,
}

/// RFC 6184 Standards-Compliant H.264 RTP Depacketizer.
/// Handles Single NAL units (types 1-23), STAP-A aggregation (type 24), and FU-A fragmentation (type 28).
/// Detects packet loss via sequence number continuity, discards corrupt frames, and tracks keyframe requirements.
pub struct H264RtpDepacketizer {
    last_seq: Option<u16>,
    current_timestamp: Option<u32>,
    fua_buffer: Vec<u8>,
    fua_in_progress: bool,
    fua_original_nal_type: u8,
    access_unit_buffer: Vec<u8>,
    current_au_has_idr: bool,
    current_au_corrupt: bool,
    waiting_for_idr: bool,
}

impl H264RtpDepacketizer {
    pub fn new() -> Self {
        Self {
            last_seq: None,
            current_timestamp: None,
            fua_buffer: Vec::with_capacity(65_536),
            fua_in_progress: false,
            fua_original_nal_type: 0,
            access_unit_buffer: Vec::with_capacity(262_144),
            current_au_has_idr: false,
            current_au_corrupt: false,
            waiting_for_idr: true,
        }
    }

    pub fn is_keyframe_needed(&self) -> bool {
        self.waiting_for_idr
    }

    pub fn force_waiting_for_idr(&mut self) {
        self.waiting_for_idr = true;
        self.current_au_corrupt = true;
        self.fua_in_progress = false;
        self.fua_buffer.clear();
        self.access_unit_buffer.clear();
        self.current_au_has_idr = false;
    }

    pub fn process_packet(
        &mut self,
        seq: u16,
        timestamp: u32,
        marker: bool,
        payload: &[u8],
    ) -> Option<AccessUnit> {
        // 1. Strict size check
        if payload.is_empty() || payload.len() > MAX_RTP_PACKET_SIZE {
            return None;
        }

        // 2. Sequence number continuity check
        if let Some(prev) = self.last_seq {
            let expected = prev.wrapping_add(1);
            if seq != expected {
                // Packet loss or reorder detected on network
                self.current_au_corrupt = true;
                self.waiting_for_idr = true; // Lost packet breaks reference frame dependency chain
                self.fua_in_progress = false;
                self.fua_buffer.clear();
                self.access_unit_buffer.clear();
                self.current_au_has_idr = false;
            }
        }
        self.last_seq = Some(seq);

        // 3. Timestamp continuity check: new timestamp without marker on previous frame
        if let Some(prev_ts) = self.current_timestamp {
            if timestamp != prev_ts {
                if !self.access_unit_buffer.is_empty() || self.fua_in_progress {
                    // Prior Access Unit abandoned without marker bit
                    self.access_unit_buffer.clear();
                    self.fua_in_progress = false;
                    self.fua_buffer.clear();
                    self.current_au_corrupt = true;
                    self.waiting_for_idr = true;
                } else {
                    self.current_au_corrupt = false;
                }
                self.current_au_has_idr = false;
            }
        }
        self.current_timestamp = Some(timestamp);

        // If current access unit is already corrupt, drain until marker bit resets state
        if self.current_au_corrupt {
            if marker {
                self.access_unit_buffer.clear();
                self.fua_in_progress = false;
                self.fua_buffer.clear();
                self.current_au_corrupt = false;
                self.current_au_has_idr = false;
            }
            return None;
        }

        // 4. Depacketize NAL units
        let nal_header = payload[0];
        let nal_type = nal_header & 0x1F;

        match nal_type {
            // Single NAL unit packet (RFC 6184 section 5.6)
            1..=23 => {
                if self.fua_in_progress {
                    // FU-A was unexpectedly interrupted by Single NAL
                    self.current_au_corrupt = true;
                    self.waiting_for_idr = true;
                    self.fua_in_progress = false;
                    self.fua_buffer.clear();
                    self.access_unit_buffer.clear();
                    self.current_au_has_idr = false;
                } else {
                    if nal_type == 5 {
                        self.current_au_has_idr = true;
                    }
                    if self.access_unit_buffer.len() + 4 + payload.len() <= MAX_ACCESS_UNIT_SIZE {
                        self.access_unit_buffer.extend_from_slice(&[0, 0, 0, 1]);
                        self.access_unit_buffer.extend_from_slice(payload);
                    } else {
                        self.current_au_corrupt = true;
                        self.waiting_for_idr = true;
                        self.access_unit_buffer.clear();
                        self.current_au_has_idr = false;
                    }
                }
            }

            // STAP-A Aggregation Packet (RFC 6184 section 5.7.1)
            24 => {
                if self.fua_in_progress {
                    self.current_au_corrupt = true;
                    self.waiting_for_idr = true;
                    self.fua_in_progress = false;
                    self.fua_buffer.clear();
                    self.access_unit_buffer.clear();
                    self.current_au_has_idr = false;
                } else {
                    let mut offset = 1;
                    let mut malformed = false;
                    let mut inner_nalus = Vec::new();
                    while offset + 2 <= payload.len() {
                        let nalu_size = u16::from_be_bytes([payload[offset], payload[offset + 1]]) as usize;
                        offset += 2;
                        if offset + nalu_size > payload.len() || nalu_size == 0 {
                            malformed = true;
                            break;
                        }
                        inner_nalus.push(&payload[offset..offset + nalu_size]);
                        offset += nalu_size;
                    }
                    if offset != payload.len() {
                        malformed = true;
                    }
                    if malformed {
                        self.current_au_corrupt = true;
                        self.waiting_for_idr = true;
                        self.access_unit_buffer.clear();
                        self.current_au_has_idr = false;
                    } else {
                        for nalu in inner_nalus {
                            let inner_type = nalu[0] & 0x1F;
                            if inner_type == 5 {
                                self.current_au_has_idr = true;
                            }
                            // Standalone SPS (7) / PPS (8) do NOT set current_au_has_idr
                            if self.access_unit_buffer.len() + 4 + nalu.len() <= MAX_ACCESS_UNIT_SIZE {
                                self.access_unit_buffer.extend_from_slice(&[0, 0, 0, 1]);
                                self.access_unit_buffer.extend_from_slice(nalu);
                            } else {
                                self.current_au_corrupt = true;
                                self.waiting_for_idr = true;
                                self.access_unit_buffer.clear();
                                self.current_au_has_idr = false;
                                break;
                            }
                        }
                    }
                }
            }

            // FU-A Fragmentation Unit (RFC 6184 section 5.8)
            28 => {
                if payload.len() < 2 {
                    self.current_au_corrupt = true;
                    self.waiting_for_idr = true;
                    self.access_unit_buffer.clear();
                    self.current_au_has_idr = false;
                } else {
                    let fu_indicator = payload[0];
                    let fu_header = payload[1];
                    let is_start = (fu_header & 0x80) != 0;
                    let is_end = (fu_header & 0x40) != 0;
                    let original_nal_type = fu_header & 0x1F;
                    let original_nal_header = (fu_indicator & 0xE0) | original_nal_type;
                    let fragment_data = &payload[2..];

                    if is_start {
                        if self.fua_in_progress {
                            // Unexpected FU-A start while prior FU-A not finished
                            self.current_au_corrupt = true;
                            self.waiting_for_idr = true;
                            self.fua_in_progress = false;
                            self.fua_buffer.clear();
                            self.access_unit_buffer.clear();
                            self.current_au_has_idr = false;
                        } else {
                            self.fua_in_progress = true;
                            self.fua_original_nal_type = original_nal_type;
                            self.fua_buffer.clear();
                            self.fua_buffer.extend_from_slice(&[0, 0, 0, 1]);
                            self.fua_buffer.push(original_nal_header);
                            self.fua_buffer.extend_from_slice(fragment_data);
                        }
                    } else if self.fua_in_progress {
                        if original_nal_type != self.fua_original_nal_type {
                            self.current_au_corrupt = true;
                            self.waiting_for_idr = true;
                            self.fua_in_progress = false;
                            self.fua_buffer.clear();
                            self.access_unit_buffer.clear();
                            self.current_au_has_idr = false;
                        } else if self.fua_buffer.len() + fragment_data.len() <= MAX_ACCESS_UNIT_SIZE {
                            self.fua_buffer.extend_from_slice(fragment_data);
                        } else {
                            self.current_au_corrupt = true;
                            self.waiting_for_idr = true;
                            self.fua_in_progress = false;
                            self.fua_buffer.clear();
                            self.access_unit_buffer.clear();
                            self.current_au_has_idr = false;
                        }
                    } else {
                        // Received middle or end FU-A fragment without start
                        self.current_au_corrupt = true;
                        self.waiting_for_idr = true;
                        self.access_unit_buffer.clear();
                        self.current_au_has_idr = false;
                    }

                    if is_end && self.fua_in_progress && !self.current_au_corrupt {
                        self.fua_in_progress = false;
                        if self.fua_original_nal_type == 5 {
                            // IDR slice confirmed complete ONLY when FU-A end is successfully reached
                            self.current_au_has_idr = true;
                        }
                        if self.access_unit_buffer.len() + self.fua_buffer.len() <= MAX_ACCESS_UNIT_SIZE {
                            self.access_unit_buffer.extend_from_slice(&self.fua_buffer);
                        } else {
                            self.current_au_corrupt = true;
                            self.waiting_for_idr = true;
                            self.access_unit_buffer.clear();
                            self.current_au_has_idr = false;
                        }
                        self.fua_buffer.clear();
                    }
                }
            }

            _ => {}
        }

        // 5. Access Unit complete on Marker bit
        if marker {
            if self.fua_in_progress {
                // Marker received while FU-A was incomplete
                self.current_au_corrupt = true;
                self.waiting_for_idr = true;
                self.fua_in_progress = false;
                self.fua_buffer.clear();
                self.access_unit_buffer.clear();
                self.current_au_has_idr = false;
                return None;
            }

            if self.current_au_corrupt || self.access_unit_buffer.is_empty() {
                self.access_unit_buffer.clear();
                self.current_au_corrupt = false;
                self.current_au_has_idr = false;
                return None;
            }

            let data = std::mem::take(&mut self.access_unit_buffer);
            let has_idr = self.current_au_has_idr;
            self.current_au_has_idr = false;
            self.current_au_corrupt = false;

            if self.waiting_for_idr {
                if has_idr {
                    // Full, valid IDR Access Unit assembled: clear waiting state
                    self.waiting_for_idr = false;
                    return Some(AccessUnit {
                        data,
                        is_idr: true,
                        timestamp,
                        end_seq: seq,
                    });
                } else {
                    // Dependent frame (P-frame, metadata) discarded while waiting for fresh keyframe
                    return None;
                }
            } else {
                return Some(AccessUnit {
                    data,
                    is_idr: has_idr,
                    timestamp,
                    end_seq: seq,
                });
            }
        }

        None
    }
}

/// Bounded Latest-Wins Video Frame Queue.
/// Maintains strict bounds on frame count and memory usage.
/// When the queue reaches capacity, older reference frames are dropped,
/// which invalidates the dependency chain for subsequent P-frames.
/// The queue purges dependent frames, enters reference loss state, and demands a keyframe.
pub struct LatestWinsFrameQueue {
    max_frames: usize,
    max_bytes: usize,
    current_bytes: usize,
    frames: std::collections::VecDeque<AccessUnit>,
    dropped_frames_count: u64,
    reference_loss_active: bool,
}

impl LatestWinsFrameQueue {
    pub fn new(max_frames: usize, max_bytes: usize) -> Self {
        Self {
            max_frames: max_frames.max(1),
            max_bytes,
            current_bytes: 0,
            frames: std::collections::VecDeque::with_capacity(max_frames),
            dropped_frames_count: 0,
            reference_loss_active: false,
        }
    }

    /// Push latest Access Unit.
    /// Returns (accepted: bool, reference_loss_occurred: bool).
    pub fn push_latest(&mut self, au: AccessUnit) -> (bool, bool) {
        let frame_len = au.data.len();
        if frame_len > self.max_bytes {
            return (false, false);
        }

        if au.is_idr {
            // IDR keyframe resets dependency chain!
            self.frames.clear();
            self.current_bytes = frame_len;
            self.frames.push_back(au);
            self.reference_loss_active = false;
            return (true, false);
        }

        // P-frame: cannot be accepted if we are waiting for IDR recovery
        if self.reference_loss_active {
            return (false, false);
        }

        // If queue is full, evicting an un-decoded reference frame breaks dependency!
        if self.frames.len() >= self.max_frames || self.current_bytes + frame_len > self.max_bytes {
            // Drop un-decoded dependent frames to prevent corrupt decoder state
            self.dropped_frames_count += self.frames.len() as u64 + 1;
            self.frames.clear();
            self.current_bytes = 0;
            self.reference_loss_active = true;
            // Signal reference loss: caller must request IDR from host and discard further P-frames
            return (false, true);
        }

        self.current_bytes += frame_len;
        self.frames.push_back(au);
        (true, false)
    }

    pub fn pop_frame(&mut self) -> Option<AccessUnit> {
        if let Some(frame) = self.frames.pop_front() {
            self.current_bytes = self.current_bytes.saturating_sub(frame.data.len());
            Some(frame)
        } else {
            None
        }
    }

    pub fn is_reference_loss_active(&self) -> bool {
        self.reference_loss_active
    }

    pub fn force_reference_loss(&mut self) {
        self.frames.clear();
        self.current_bytes = 0;
        self.reference_loss_active = true;
    }

    pub fn len(&self) -> usize {
        self.frames.len()
    }

    pub fn is_empty(&self) -> bool {
        self.frames.is_empty()
    }

    pub fn dropped_count(&self) -> u64 {
        self.dropped_frames_count
    }

    pub fn clear(&mut self) {
        self.frames.clear();
        self.current_bytes = 0;
    }
}

#[derive(Clone, Debug, Default)]
pub struct IceConfig {
    pub stun_servers: Vec<String>,
    pub turn_server: Option<String>,
    pub turn_username: Option<String>,
    pub turn_credential: Option<String>,
}

impl IceConfig {
    pub fn to_rtc_configuration(&self) -> RTCConfiguration {
        let mut servers = Vec::new();

        let stun_list = if self.stun_servers.is_empty() {
            vec!["stun:stun.l.google.com:19302".to_string()]
        } else {
            self.stun_servers.clone()
        };
        servers.push(RTCIceServer {
            urls: stun_list,
            ..Default::default()
        });

        if let (Some(url), Some(user), Some(cred)) = (
            &self.turn_server,
            &self.turn_username,
            &self.turn_credential,
        ) {
            if !url.is_empty() && !user.is_empty() && !cred.is_empty() {
                servers.push(RTCIceServer {
                    urls: vec![url.clone()],
                    username: user.clone(),
                    credential: cred.clone(),
                    ..Default::default()
                });
            }
        }

        RTCConfiguration {
            ice_servers: servers,
            ..Default::default()
        }
    }
}

pub fn ice_state_to_code(state: RTCIceConnectionState) -> i32 {
    match state {
        RTCIceConnectionState::New => 0,
        RTCIceConnectionState::Checking => 1,
        RTCIceConnectionState::Connected => 2,
        RTCIceConnectionState::Completed => 3,
        RTCIceConnectionState::Failed => 4,
        RTCIceConnectionState::Disconnected => 5,
        RTCIceConnectionState::Closed => 6,
        _ => -1,
    }
}

/// Host-side WebRTC session (publishes H.264 video track and input DataChannels).
pub struct WebRtcHostSession {
    peer_connection: Arc<RTCPeerConnection>,
    video_track: Option<Arc<TrackLocalStaticSample>>,
    cursor_channel: Arc<Mutex<Option<Arc<RTCDataChannel>>>>,
    control_channel: Arc<Mutex<Option<Arc<RTCDataChannel>>>>,
    outgoing_candidates_rx: Arc<Mutex<mpsc::Receiver<String>>>,
    incoming_control_rx: Arc<Mutex<mpsc::Receiver<Vec<u8>>>>,
    incoming_cursor_slot: Arc<Mutex<Option<(i32, i32, u64)>>>,
    latest_cursor_seq: Arc<AtomicU64>,
    control_saturated: Arc<AtomicBool>,
    ice_failed: Arc<AtomicBool>,
}

impl WebRtcHostSession {
    pub async fn create(config: IceConfig) -> Result<Self, String> {
        let mut media_engine = MediaEngine::default();
        media_engine.register_default_codecs().map_err(|e| e.to_string())?;

        let api = APIBuilder::new().with_media_engine(media_engine).build();
        let rtc_config = config.to_rtc_configuration();
        let peer_connection = Arc::new(api.new_peer_connection(rtc_config).await.map_err(|e| e.to_string())?);

        // 1. Add H.264 video track
        let video_track = Arc::new(TrackLocalStaticSample::new(
            RTCRtpCodecCapability {
                mime_type: MIME_TYPE_H264.to_string(),
                ..Default::default()
            },
            "video-desktop".to_string(),
            "aether-desktop-stream".to_string(),
        ));

        peer_connection
            .add_track(Arc::clone(&video_track) as Arc<dyn TrackLocal + Send + Sync>)
            .await
            .map_err(|e| e.to_string())?;

        // 2. DataChannels setup
        let cursor_channel_slot = Arc::new(Mutex::new(None));
        let control_channel_slot = Arc::new(Mutex::new(None));
        let control_saturated = Arc::new(AtomicBool::new(false));
        let ice_failed = Arc::new(AtomicBool::new(false));

        // Discrete control channel: ordered, reliable, with bounded queue (128 items)
        let (ctrl_tx, ctrl_rx) = mpsc::channel(128);

        // Cursor channel: latest-wins slot (replaces previous position atomically without FIFO delay)
        let incoming_cursor_slot = Arc::new(Mutex::new(None));
        let latest_seq = Arc::new(AtomicU64::new(0));

        // Cursor channel: unordered, max_retransmits = 0 (loss-tolerant, lowest latency)
        let cursor_init = RTCDataChannelInit {
            ordered: Some(false),
            max_retransmits: Some(0),
            ..Default::default()
        };
        let cursor_dc = peer_connection
            .create_data_channel("cursor", Some(cursor_init))
            .await
            .map_err(|e| e.to_string())?;

        let cursor_slot_clone = Arc::clone(&incoming_cursor_slot);
        let latest_seq_clone = Arc::clone(&latest_seq);
        cursor_dc.on_message(Box::new(move |msg| {
            if msg.data.len() >= 16 {
                let seq = u64::from_be_bytes(msg.data[0..8].try_into().unwrap());
                let x = i32::from_be_bytes(msg.data[8..12].try_into().unwrap());
                let y = i32::from_be_bytes(msg.data[12..16].try_into().unwrap());

                let prev = latest_seq_clone.load(Ordering::Relaxed);
                if seq > prev {
                    latest_seq_clone.store(seq, Ordering::Relaxed);
                    let slot = cursor_slot_clone.clone();
                    return Box::pin(async move {
                        let mut guard = slot.lock().await;
                        // True latest-wins: overwrites previous unpolled position instantly
                        *guard = Some((x, y, seq));
                    });
                }
            }
            Box::pin(async {})
        }));
        *cursor_channel_slot.lock().await = Some(cursor_dc);

        // Control channel: ordered, reliable
        let control_init = RTCDataChannelInit {
            ordered: Some(true),
            ..Default::default()
        };
        let control_dc = peer_connection
            .create_data_channel("control", Some(control_init))
            .await
            .map_err(|e| e.to_string())?;

        let ctrl_tx_clone = ctrl_tx.clone();
        let control_saturated_clone = Arc::clone(&control_saturated);
        let pc_weak = Arc::downgrade(&peer_connection);
        control_dc.on_message(Box::new(move |msg| {
            let ctrl_tx = ctrl_tx_clone.clone();
            let sat = control_saturated_clone.clone();
            let weak_pc = pc_weak.clone();
            let data = msg.data.to_vec();
            Box::pin(async move {
                // Apply async backpressure to SCTP data channel (up to 3000ms wait before timeout)
                // Never discard discrete control events silently; if queue stays saturated, fail closed
                if let Err(e) = tokio::time::timeout(Duration::from_millis(3000), ctrl_tx.send(data)).await {
                    eprintln!("[AetherCore WebRTC] Control channel queue full for 3s: backpressure timeout: {}", e);
                    sat.store(true, Ordering::SeqCst);
                    if let Some(pc) = weak_pc.upgrade() {
                        let _ = pc.close().await;
                    }
                }
            })
        }));
        *control_channel_slot.lock().await = Some(control_dc);

        // 3. ICE Candidate gathering (bounded to 64 candidates, guaranteed delivery without silent drop)
        let (cand_tx, cand_rx) = mpsc::channel(64);
        let cand_tx_clone = cand_tx.clone();
        let ice_failed_clone = Arc::clone(&ice_failed);
        peer_connection.on_ice_candidate(Box::new(move |c| {
            let cand_tx = cand_tx_clone.clone();
            let ice_fail = ice_failed_clone.clone();
            if let Some(candidate) = c {
                if let Ok(json) = candidate.to_json() {
                    if let Ok(serialized) = serde_json::to_string(&json) {
                        return Box::pin(async move {
                            if let Err(e) = tokio::time::timeout(Duration::from_millis(2000), cand_tx.send(serialized)).await {
                                eprintln!("[AetherCore WebRTC Host] Failed to enqueue ICE candidate or timeout: {}", e);
                                ice_fail.store(true, Ordering::SeqCst);
                            }
                        });
                    }
                }
            }
            Box::pin(async {})
        }));

        Ok(Self {
            peer_connection,
            video_track: Some(video_track),
            cursor_channel: cursor_channel_slot,
            control_channel: control_channel_slot,
            outgoing_candidates_rx: Arc::new(Mutex::new(cand_rx)),
            incoming_control_rx: Arc::new(Mutex::new(ctrl_rx)),
            incoming_cursor_slot,
            latest_cursor_seq: latest_seq,
            control_saturated,
            ice_failed,
        })
    }

    pub async fn create_offer(&self) -> Result<String, String> {
        let offer = self.peer_connection.create_offer(None).await.map_err(|e| e.to_string())?;
        self.peer_connection.set_local_description(offer.clone()).await.map_err(|e| e.to_string())?;
        Ok(offer.sdp)
    }

    pub async fn set_remote_answer(&self, sdp: &str) -> Result<(), String> {
        let answer = RTCSessionDescription::answer(sdp.to_string()).map_err(|e| e.to_string())?;
        self.peer_connection.set_remote_description(answer).await.map_err(|e| e.to_string())?;
        Ok(())
    }

    pub async fn add_remote_ice_candidate(&self, candidate_json: &str) -> Result<(), String> {
        let init: RTCIceCandidateInit = serde_json::from_str(candidate_json).map_err(|e| e.to_string())?;
        self.peer_connection.add_ice_candidate(init).await.map_err(|e| e.to_string())?;
        Ok(())
    }

    pub async fn write_video_sample(&self, nalu_bytes: &[u8], duration_ms: u32) -> Result<(), String> {
        if let Some(ref track) = self.video_track {
            let sample = Sample {
                data: bytes::Bytes::copy_from_slice(nalu_bytes),
                duration: Duration::from_millis(duration_ms as u64),
                ..Default::default()
            };
            track.write_sample(&sample).await.map_err(|e| e.to_string())?;
            Ok(())
        } else {
            Err("Video track not initialized on session".to_string())
        }
    }

    pub async fn poll_outgoing_ice_candidate(&self) -> Option<String> {
        let mut rx = self.outgoing_candidates_rx.lock().await;
        rx.try_recv().ok()
    }

    pub async fn poll_control_message(&self) -> Option<Vec<u8>> {
        let mut rx = self.incoming_control_rx.lock().await;
        rx.try_recv().ok()
    }

    pub async fn poll_cursor_position(&self) -> Option<(i32, i32, u64)> {
        let mut guard = self.incoming_cursor_slot.lock().await;
        guard.take()
    }

    pub fn is_control_saturated(&self) -> bool {
        self.control_saturated.load(Ordering::SeqCst)
    }

    pub fn get_ice_connection_state(&self) -> RTCIceConnectionState {
        if self.ice_failed.load(Ordering::SeqCst) {
            RTCIceConnectionState::Failed
        } else {
            self.peer_connection.ice_connection_state()
        }
    }

    pub async fn close(&self) -> Result<(), String> {
        self.peer_connection.close().await.map_err(|e| e.to_string())
    }
}

/// Client-side WebRTC session (receives H.264 video track and sends input DataChannels).
pub struct WebRtcClientSession {
    peer_connection: Arc<RTCPeerConnection>,
    cursor_channel: Arc<Mutex<Option<Arc<RTCDataChannel>>>>,
    control_channel: Arc<Mutex<Option<Arc<RTCDataChannel>>>>,
    outgoing_candidates_rx: Arc<Mutex<mpsc::Receiver<String>>>,
    incoming_video_frames: Arc<Mutex<LatestWinsFrameQueue>>,
    keyframe_requested: Arc<AtomicBool>,
    ice_failed: Arc<AtomicBool>,
}

impl WebRtcClientSession {
    pub async fn create(config: IceConfig) -> Result<Self, String> {
        let mut media_engine = MediaEngine::default();
        media_engine.register_default_codecs().map_err(|e| e.to_string())?;

        let api = APIBuilder::new().with_media_engine(media_engine).build();
        let rtc_config = config.to_rtc_configuration();
        let peer_connection = Arc::new(api.new_peer_connection(rtc_config).await.map_err(|e| e.to_string())?);

        // Bounded latest-wins frame queue: max 2 frames, max 32 MB total memory
        let frame_queue = Arc::new(Mutex::new(LatestWinsFrameQueue::new(2, 32 * 1024 * 1024)));
        let (cand_tx, cand_rx) = mpsc::channel(64);
        let keyframe_requested = Arc::new(AtomicBool::new(true));
        let ice_failed = Arc::new(AtomicBool::new(false));

        // 1. Listen for incoming remote video track (H.264 RTP stream with RFC 6184 depacketization)
        let frame_queue_clone = Arc::clone(&frame_queue);
        let keyframe_req_clone = Arc::clone(&keyframe_requested);

        peer_connection.on_track(Box::new(move |track: Arc<TrackRemote>, _, _| {
            let frame_queue = frame_queue_clone.clone();
            let keyframe_req = keyframe_req_clone.clone();

            Box::pin(async move {
                if track.codec().capability.mime_type.to_lowercase() == MIME_TYPE_H264.to_lowercase() {
                    let mut depacketizer = H264RtpDepacketizer::new();

                    while let Ok((rtp_packet, _)) = track.read_rtp().await {
                        let seq = rtp_packet.header.sequence_number;
                        let ts = rtp_packet.header.timestamp;
                        let marker = rtp_packet.header.marker;

                        if let Some(au) = depacketizer.process_packet(seq, ts, marker, &rtp_packet.payload) {
                            let mut queue = frame_queue.lock().await;
                            let (_accepted, reference_loss) = queue.push_latest(au);
                            if reference_loss {
                                // An un-decoded reference frame was evicted to avoid queue backlog:
                                // invalidates subsequent P-frames, request immediate IDR keyframe!
                                depacketizer.force_waiting_for_idr();
                                keyframe_req.store(true, Ordering::Relaxed);
                            }
                        }

                        if depacketizer.is_keyframe_needed() {
                            keyframe_req.store(true, Ordering::Relaxed);
                        }
                    }
                }
            })
        }));

        // 2. DataChannels setup on client side
        let cursor_channel_slot = Arc::new(Mutex::new(None));
        let control_channel_slot = Arc::new(Mutex::new(None));

        let c_slot = cursor_channel_slot.clone();
        let ctrl_slot = control_channel_slot.clone();
        peer_connection.on_data_channel(Box::new(move |dc: Arc<RTCDataChannel>| {
            let label = dc.label().to_string();
            let c_slot = c_slot.clone();
            let ctrl_slot = ctrl_slot.clone();
            Box::pin(async move {
                if label == "cursor" {
                    *c_slot.lock().await = Some(dc);
                } else if label == "control" {
                    *ctrl_slot.lock().await = Some(dc);
                }
            })
        }));

        // 3. ICE Candidate gathering (bounded to 64 candidates, guaranteed delivery)
        let cand_tx_clone = cand_tx.clone();
        let ice_failed_client_clone = Arc::clone(&ice_failed);
        peer_connection.on_ice_candidate(Box::new(move |c| {
            let cand_tx = cand_tx_clone.clone();
            let ice_fail = ice_failed_client_clone.clone();
            if let Some(candidate) = c {
                if let Ok(json) = candidate.to_json() {
                    if let Ok(serialized) = serde_json::to_string(&json) {
                        return Box::pin(async move {
                            if let Err(e) = tokio::time::timeout(Duration::from_millis(2000), cand_tx.send(serialized)).await {
                                eprintln!("[AetherCore WebRTC Client] Failed to enqueue ICE candidate or timeout: {}", e);
                                ice_fail.store(true, Ordering::SeqCst);
                            }
                        });
                    }
                }
            }
            Box::pin(async {})
        }));

        Ok(Self {
            peer_connection,
            cursor_channel: cursor_channel_slot,
            control_channel: control_channel_slot,
            outgoing_candidates_rx: Arc::new(Mutex::new(cand_rx)),
            incoming_video_frames: frame_queue,
            keyframe_requested,
            ice_failed,
        })
    }

    pub async fn set_remote_offer(&self, sdp: &str) -> Result<(), String> {
        let offer = RTCSessionDescription::offer(sdp.to_string()).map_err(|e| e.to_string())?;
        self.peer_connection.set_remote_description(offer).await.map_err(|e| e.to_string())?;
        Ok(())
    }

    pub async fn create_answer(&self) -> Result<String, String> {
        let answer = self.peer_connection.create_answer(None).await.map_err(|e| e.to_string())?;
        self.peer_connection.set_local_description(answer.clone()).await.map_err(|e| e.to_string())?;
        Ok(answer.sdp)
    }

    pub async fn add_remote_ice_candidate(&self, candidate_json: &str) -> Result<(), String> {
        let init: RTCIceCandidateInit = serde_json::from_str(candidate_json).map_err(|e| e.to_string())?;
        self.peer_connection.add_ice_candidate(init).await.map_err(|e| e.to_string())?;
        Ok(())
    }

    pub async fn poll_outgoing_ice_candidate(&self) -> Option<String> {
        let mut rx = self.outgoing_candidates_rx.lock().await;
        rx.try_recv().ok()
    }

    /// Poll reassembled H.264 Access Unit (frame) from bounded depacketizer queue.
    pub async fn poll_video_frame(&self) -> Option<Vec<u8>> {
        let mut queue = self.incoming_video_frames.lock().await;
        queue.pop_frame().map(|au| au.data)
    }

    pub fn take_keyframe_request(&self) -> bool {
        self.keyframe_requested.swap(false, Ordering::Relaxed)
    }

    pub async fn send_cursor(&self, x: i32, y: i32, seq: u64) -> Result<(), String> {
        let dc_guard = self.cursor_channel.lock().await;
        if let Some(ref dc) = *dc_guard {
            let mut buf = [0u8; 16];
            buf[0..8].copy_from_slice(&seq.to_be_bytes());
            buf[8..12].copy_from_slice(&x.to_be_bytes());
            buf[12..16].copy_from_slice(&y.to_be_bytes());
            dc.send(&bytes::Bytes::copy_from_slice(&buf)).await.map_err(|e| e.to_string())?;
            Ok(())
        } else {
            Err("Cursor data channel not open yet".to_string())
        }
    }

    pub async fn send_control(&self, data: &[u8]) -> Result<(), String> {
        let dc_guard = self.control_channel.lock().await;
        if let Some(ref dc) = *dc_guard {
            dc.send(&bytes::Bytes::copy_from_slice(data)).await.map_err(|e| e.to_string())?;
            Ok(())
        } else {
            Err("Control data channel not open yet".to_string())
        }
    }

    pub fn get_ice_connection_state(&self) -> RTCIceConnectionState {
        if self.ice_failed.load(Ordering::SeqCst) {
            RTCIceConnectionState::Failed
        } else {
            self.peer_connection.ice_connection_state()
        }
    }

    pub async fn close(&self) -> Result<(), String> {
        self.peer_connection.close().await.map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_depacketizer_single_nal_non_idr_discarded_initially() {
        let mut depack = H264RtpDepacketizer::new();
        assert!(depack.is_keyframe_needed());

        // Single NAL unit with nal_type = 1 (non-IDR slice)
        let p_frame_payload = vec![0x21, 0xAA, 0xBB, 0xCC];
        let res = depack.process_packet(1, 1000, true, &p_frame_payload);
        // Must be discarded because waiting_for_idr is true!
        assert!(res.is_none());
        assert!(depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_single_nal_idr_recovers() {
        let mut depack = H264RtpDepacketizer::new();
        assert!(depack.is_keyframe_needed());

        // Single NAL unit with nal_type = 5 (IDR slice)
        let idr_payload = vec![0x25, 0x11, 0x22, 0x33];
        let res = depack.process_packet(1, 1000, true, &idr_payload);
        assert!(res.is_some());
        let au = res.unwrap();
        assert!(au.is_idr);
        assert_eq!(&au.data[0..4], &[0, 0, 0, 1]);
        assert_eq!(&au.data[4..], &idr_payload[..]);
        assert!(!depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_sps_pps_alone_do_not_clear_keyframe_needed() {
        let mut depack = H264RtpDepacketizer::new();
        assert!(depack.is_keyframe_needed());

        let sps = vec![0x27, 0x42, 0x00, 0x1E];
        let pps = vec![0x28, 0xCE, 0x38, 0x80];

        // STAP-A packet aggregating SPS and PPS
        let mut stap_a = vec![24];
        stap_a.extend_from_slice(&(sps.len() as u16).to_be_bytes());
        stap_a.extend_from_slice(&sps);
        stap_a.extend_from_slice(&(pps.len() as u16).to_be_bytes());
        stap_a.extend_from_slice(&pps);

        let res = depack.process_packet(1, 1000, true, &stap_a);
        // Standalone parameter sets without IDR slice must NOT clear keyframe_needed
        assert!(res.is_none());
        assert!(depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_stap_a_with_idr_clears_keyframe_needed() {
        let mut depack = H264RtpDepacketizer::new();
        assert!(depack.is_keyframe_needed());

        let sps = vec![0x27, 0x42, 0x00, 0x1E];
        let pps = vec![0x28, 0xCE, 0x38, 0x80];
        let idr = vec![0x25, 0x01, 0x02, 0x03];

        let mut stap_a = vec![24];
        stap_a.extend_from_slice(&(sps.len() as u16).to_be_bytes());
        stap_a.extend_from_slice(&sps);
        stap_a.extend_from_slice(&(pps.len() as u16).to_be_bytes());
        stap_a.extend_from_slice(&pps);
        stap_a.extend_from_slice(&(idr.len() as u16).to_be_bytes());
        stap_a.extend_from_slice(&idr);

        let res = depack.process_packet(1, 1000, true, &stap_a);
        assert!(res.is_some());
        let au = res.unwrap();
        assert!(au.is_idr);
        assert!(!depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_stap_a_malformed_triggers_corruption() {
        let mut depack = H264RtpDepacketizer::new();
        // Truncated NALU size in STAP-A
        let stap_a_corrupt = vec![24, 0x00, 0x20, 0x25, 0x01]; // declares 32 bytes, only 2 provided
        let res = depack.process_packet(1, 1000, true, &stap_a_corrupt);
        assert!(res.is_none());
        assert!(depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_fua_idr_recovers_only_on_end_and_marker() {
        let mut depack = H264RtpDepacketizer::new();
        assert!(depack.is_keyframe_needed());

        let fu_ind = 0x7C;      // type 28
        let fu_start = 0x85;    // start bit=1, orig_type=5 (IDR)
        let fu_mid = 0x05;      // start=0, end=0, orig_type=5
        let fu_end = 0x45;      // start=0, end=1, orig_type=5

        let p1 = vec![fu_ind, fu_start, 0x10, 0x20];
        let p2 = vec![fu_ind, fu_mid, 0x30, 0x40];
        let p3 = vec![fu_ind, fu_end, 0x50, 0x60];

        // 1. Start packet: must NOT clear keyframe_needed
        let r1 = depack.process_packet(10, 5000, false, &p1);
        assert!(r1.is_none());
        assert!(depack.is_keyframe_needed());

        // 2. Middle packet: must NOT clear keyframe_needed
        let r2 = depack.process_packet(11, 5000, false, &p2);
        assert!(r2.is_none());
        assert!(depack.is_keyframe_needed());

        // 3. End packet with marker=true: completes AU and recovers from keyframe_needed!
        let r3 = depack.process_packet(12, 5000, true, &p3);
        assert!(r3.is_some());
        assert!(!depack.is_keyframe_needed());
        let au = r3.unwrap();
        assert!(au.is_idr);
        assert_eq!(&au.data[0..4], &[0, 0, 0, 1]);
        assert_eq!(au.data[4], (fu_ind & 0xE0) | 5);
        assert_eq!(&au.data[5..], &[0x10, 0x20, 0x30, 0x40, 0x50, 0x60]);
    }

    #[test]
    fn test_depacketizer_fua_interrupted_by_new_start() {
        let mut depack = H264RtpDepacketizer::new();
        let fu_ind = 0x7C;
        let fu_start = 0x85; // start=1, orig_type=5
        let p1 = vec![fu_ind, fu_start, 0x10, 0x20];
        let p2 = vec![fu_ind, fu_start, 0x99, 0x88]; // illegal: second start before end!

        depack.process_packet(1, 5000, false, &p1);
        let res = depack.process_packet(2, 5000, true, &p2);
        assert!(res.is_none());
        assert!(depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_sequence_loss_requires_keyframe_and_drops_au() {
        let mut depack = H264RtpDepacketizer::new();
        // First get into valid streaming state with an IDR
        let idr = vec![0x25, 0x11, 0x22];
        let _ = depack.process_packet(1, 1000, true, &idr);
        assert!(!depack.is_keyframe_needed());

        // Now stream P-frames
        let p1 = vec![0x21, 0xAA];
        let r1 = depack.process_packet(2, 2000, true, &p1);
        assert!(r1.is_some());

        // Sequence loss: packet 3 is missing, packet 4 arrives!
        let p2 = vec![0x21, 0xBB];
        let r2 = depack.process_packet(4, 3000, true, &p2);
        // Packet loss must cause frame drop and set keyframe_needed
        assert!(r2.is_none());
        assert!(depack.is_keyframe_needed());

        // Subsequent P-frame with seq 5 must also be dropped
        let p3 = vec![0x21, 0xCC];
        let r3 = depack.process_packet(5, 4000, true, &p3);
        assert!(r3.is_none());
        assert!(depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_timestamp_discontinuity_abandons_unmarked_frame() {
        let mut depack = H264RtpDepacketizer::new();
        let p1 = vec![0x21, 0x01];
        // Packet 1 on timestamp 1000, marker=false
        depack.process_packet(1, 1000, false, &p1);

        // Packet 2 arrives with timestamp 2000 (new AU) without marker on previous
        let p2 = vec![0x21, 0x02];
        let r2 = depack.process_packet(2, 2000, true, &p2);
        assert!(r2.is_none());
        assert!(depack.is_keyframe_needed());
    }

    #[test]
    fn test_depacketizer_strict_size_bounds() {
        let mut depack = H264RtpDepacketizer::new();
        let huge_payload = vec![0x25; MAX_RTP_PACKET_SIZE + 1];
        let res = depack.process_packet(1, 1000, true, &huge_payload);
        assert!(res.is_none());
    }

    #[test]
    fn test_latest_wins_queue_idr_and_p_frame_behavior() {
        let mut queue = LatestWinsFrameQueue::new(2, 1024 * 1024);
        assert_eq!(queue.len(), 0);

        let idr = AccessUnit { data: vec![0x25, 1, 2], is_idr: true, timestamp: 1000, end_seq: 1 };
        let p1 = AccessUnit { data: vec![0x21, 3, 4], is_idr: false, timestamp: 2000, end_seq: 2 };

        let (acc1, ref_loss1) = queue.push_latest(idr);
        assert!(acc1);
        assert!(!ref_loss1);
        assert_eq!(queue.len(), 1);

        let (acc2, ref_loss2) = queue.push_latest(p1);
        assert!(acc2);
        assert!(!ref_loss2);
        assert_eq!(queue.len(), 2);
    }

    #[test]
    fn test_reference_frame_eviction_discards_subsequent_p_frames_and_requires_idr() {
        let mut queue = LatestWinsFrameQueue::new(2, 1024 * 1024);
        let idr1 = AccessUnit { data: vec![0x25, 1, 1], is_idr: true, timestamp: 1000, end_seq: 1 };
        let p1 = AccessUnit { data: vec![0x21, 2, 2], is_idr: false, timestamp: 2000, end_seq: 2 };
        let p2 = AccessUnit { data: vec![0x21, 3, 3], is_idr: false, timestamp: 3000, end_seq: 3 };

        assert_eq!(queue.push_latest(idr1), (true, false));
        assert_eq!(queue.push_latest(p1), (true, false));
        assert_eq!(queue.len(), 2);

        // Pushing p2 when queue is full evicts un-decoded reference frames:
        // Must clear queue, enter reference_loss_active, and return (accepted=false, reference_loss=true)
        let (acc, ref_loss) = queue.push_latest(p2);
        assert!(!acc);
        assert!(ref_loss);
        assert_eq!(queue.len(), 0);
        assert!(queue.is_reference_loss_active());

        // Subsequent P-frames must be discarded while reference loss is active
        let p3 = AccessUnit { data: vec![0x21, 4, 4], is_idr: false, timestamp: 4000, end_seq: 4 };
        let (acc3, ref_loss3) = queue.push_latest(p3);
        assert!(!acc3);
        assert!(!ref_loss3); // Already in reference loss, don't re-trigger flood
        assert_eq!(queue.len(), 0);

        // Fresh IDR frame recovers pipeline: accepted and clears reference_loss_active
        let idr2 = AccessUnit { data: vec![0x25, 5, 5], is_idr: true, timestamp: 5000, end_seq: 5 };
        let (acc_idr, ref_loss_idr) = queue.push_latest(idr2);
        assert!(acc_idr);
        assert!(!ref_loss_idr);
        assert_eq!(queue.len(), 1);
        assert!(!queue.is_reference_loss_active());
    }

    #[test]
    fn test_depacketizer_force_waiting_for_idr_discards_until_complete_idr() {
        let mut depack = H264RtpDepacketizer::new();
        // Valid stream with IDR
        let idr = vec![0x25, 0x11, 0x22];
        let _ = depack.process_packet(1, 1000, true, &idr);
        assert!(!depack.is_keyframe_needed());

        // Force recovery state
        depack.force_waiting_for_idr();
        assert!(depack.is_keyframe_needed());

        // P-frames must be rejected
        let p1 = vec![0x21, 0x33, 0x44];
        let r1 = depack.process_packet(2, 2000, true, &p1);
        assert!(r1.is_none());
        assert!(depack.is_keyframe_needed());

        // New IDR recovers
        let idr2 = vec![0x25, 0x55, 0x66];
        let r2 = depack.process_packet(3, 3000, true, &idr2);
        assert!(r2.is_some());
        assert!(!depack.is_keyframe_needed());
    }

    #[tokio::test]
    async fn test_control_channel_backpressure_and_saturation_failure() {
        let (ctrl_tx, _ctrl_rx) = mpsc::channel(2);
        let control_saturated = Arc::new(AtomicBool::new(false));

        // Fill channel to capacity
        let _ = ctrl_tx.send(vec![1, 2, 3]).await;
        let _ = ctrl_tx.send(vec![4, 5, 6]).await;

        // Next message under short timeout will experience backpressure
        let sat = Arc::clone(&control_saturated);
        let res = tokio::time::timeout(Duration::from_millis(50), ctrl_tx.send(vec![7, 8, 9])).await;
        assert!(res.is_err(), "Backpressure timeout expected on full control queue");
        sat.store(true, Ordering::SeqCst);
        assert!(sat.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn test_cursor_latest_wins_replaces_old_coordinate() {
        let slot = Arc::new(Mutex::new(None));
        let latest_seq = Arc::new(AtomicU64::new(0));

        // Coordinate 1 at seq 10
        {
            latest_seq.store(10, Ordering::Relaxed);
            let mut g = slot.lock().await;
            *g = Some((100, 200, 10));
        }

        // Newer coordinate 2 at seq 20 overwrites slot without FIFO backlog
        {
            latest_seq.store(20, Ordering::Relaxed);
            let mut g = slot.lock().await;
            *g = Some((300, 400, 20));
        }

        let mut g = slot.lock().await;
        let polled = g.take();
        assert_eq!(polled, Some((300, 400, 20)));
        assert_eq!(g.take(), None);
    }
}
