# AetherDesk Codebase Drift Audit Registry

Multi-session, multi-tool audit conducted across Era 1 (Initial Build, Oct 21), Era 2 (Multi-Monitor, Discovery & UI Bento, Oct 22), and Era 3 (Streaming Flow Control, DPAPI & Mutual Auth, Oct 22-23).

---

## View 1: By Finding (Master List)

| ID | Finding | Files | Type | Severity | Status |
|---|---|---|---|---|---|
| F-01 | Unauthenticated host actions (privacy mode, monitor switch, quality, file start/end) | `src/host_daemon.py` | Logic mismatch (Auth bypass) | Critical | Fixed |
| F-02 | Auth verification skipped when `binary` is missing/None | `src/host_daemon.py`, `tests/test_daemon.py` | Logic mismatch (Auth bypass) | Critical | Fixed |
| F-03 | Reversed unauthenticated fallback default (`.get("authenticated", True)`) | `src/host_daemon.py` | Logic mismatch (Reversed fallback) | Critical | Fixed |
| F-04 | Host daemon bypasses DPAPI container (`SecurityManager.get_master_key()`) & default password mismatch (`default_password` vs `123456`) | `src/host_daemon.py`, `src/security.py` | Logic mismatch / Semantic divergence | Critical | Fixed |
| F-05 | Client ignores `CLIENT_DISCONNECTED` control signal | `src/client.py`, `src/signaling_broker.py` | Event handler gap | Moderate | Fixed |
| F-06 | Host daemon leaks file handles on client abort mid-transfer | `src/host_daemon.py`, `src/transfer.py` | Resource leak | Moderate | Fixed |
| F-07 | Rate limiting methods (`is_rate_limited`, `record_failed_attempt`) uncalled in production | `src/security.py`, `src/host_daemon.py`, `src/signaling_broker.py` | Dead code / Ghost security | Moderate | Fixed |
| F-08 | Direct IP connection requires local broker running on target host | `src/client.py`, `src/discovery.py`, `src/host_daemon.py` | Architectural assumption gap | Moderate | Fixed |
| F-09 | E2E integration test hardcodes local machine device ID & config | `tests/test_windows_subsystem_e2e.py` | Test coupling / Fragile assertion | Moderate | Fixed |
| F-10 | Dual parallel Host Control Panels (`host_tray.py` vs `client_viewer.py`) | `src/host_tray.py`, `src/client_viewer.py` | Duplicate implementation | Moderate | Fixed |
| F-11 | Stale branding paths (`RemoteDesktop`, `RemoteDesktopClient`, `RemoteDesktop_Transfers`) | `src/security.py`, `src/client.py`, `src/transfer.py`, `src/host_service.py` | Legacy naming drift | Cosmetic | Fixed |
| F-12 | Documentation & badge divergence in `README.md` and `DESIGN.md` (38 vs 75 tests, v1.2.0 vs v1.3.5, banned emojis present) | `README.md`, `DESIGN.md`, `src/client_viewer.py`, `src/host_tray.py` | Doc/code mismatch | Cosmetic | Fixed |
| F-13 | DesktopCapture GDI attributes initialized after `switch_desktop()` causing silent thread crash | `src/capture.py` | Initialization race | Critical | Fixed |

---

## View 2: By File Era

| Era | Date / Commits | Dominant Architectural Pattern | Files Following Pattern |
|---|---|---|---|
| **Era 1: Foundation** | 2026-10-21 (`docs: add design spec` -> `54cc1eb`) | Monolithic scripts, `RemoteDesktop` branding, 100k PBKDF2 iterations, plaintext master key in JSON, `default_password` fallback, fire-and-forget message routing, GDI-only capture. | `src/protocol.py`, `src/transfer.py`, `src/host_service.py`, `tests/test_e2e.py`, `tests/test_daemon.py` |
| **Era 2: Discovery & Bento UI** | 2026-10-22 (`9023cbf` -> `613601f`) | Rebrand to `AetherDesk`, Apple Dark Bento UI, UDP LAN discovery beacon (port 8766), direct IP address routing, multi-monitor crop & virtual coordinate mapping, Dual-Bank OTA updater (`src/ota.py`). | `src/discovery.py`, `src/ota.py`, `src/input_handler.py`, `DESIGN.md`, `RemoteClient.spec` |
| **Era 3: Performance, DPAPI & Mutual Auth** | 2026-10-22 to 2026-10-23 (`778769a` -> `d002333`) | RFC 5116 anti-replay framing (`AEP1`), 600k PBKDF2 iterations, Windows DPAPI secret protection, visible `"123456"` password provisioning, flow control (`FRAME_ACK`), DXcam capture fallback, 50Hz mouse throttling. | `src/security.py`, `src/capture.py`, `src/client_viewer.py` (Host tab added), `src/host_daemon.py` (partial adaptation) |

---

## View 3: By Responsibility

| Responsibility | Implementations Found | Consistency Assessment |
|---|---|---|
| **Host Control Panel & Daemon Launch** | `src/host_tray.py` (`HostControlPanel`) vs `src/client_viewer.py` (`_render_host_tab`, `_run_host_daemon_worker`) | **Fixed**. `client_viewer.py` cleanly shuts down `SignalingBroker` on daemon stop, matching `host_tray.py`. |
| **Master Key & Password Derivation** | `src/security.py` (`get_master_key`, `verify_password`) vs `src/host_daemon.py` (L100) | **Fixed**. `HostDaemon` calls `self.security.get_master_key()` and falls back to `self.security.get_password_display()` (`"123456"`). |
| **Authentication Enforcement** | `src/host_daemon.py` L106-L115 | **Fixed**. `AUTH_REQUEST` unconditionally requires and validates the encrypted `b"AETHER_AUTH_V1"` binary challenge token. |
| **Unauthenticated Command Gating** | `src/host_daemon.py` (`SWITCH_MONITOR`, `GET_MONITORS`, `PRIVACY_MODE`, `SET_QUALITY`, `FILE_START`, `FILE_END`) | **Fixed**. Prepend auth gate rejecting unauthenticated commands with `AUTH_FAILED`. |
| **Peer Disconnection Signaling** | `src/signaling_broker.py` L117 vs `src/client.py` L475 | **Fixed**. Client `_receive_loop` handles `CLIENT_DISCONNECTED` and halts streaming gracefully. Broker disconnect routing race fixed. |
| **Rate Limiting & Anti-Brute-Force** | `src/security.py` (`is_rate_limited`, `record_failed_attempt`) | **Fixed**. Wired into `HostDaemon.handle_client_message` on `AUTH_REQUEST`. |
| **Filesystem Paths** | `src/security.py`, `src/client.py`, `src/transfer.py`, `src/host_service.py` | **Fixed**. Migrated to `AetherDesk` primary paths with backward compatibility fallback to `RemoteDesktop`. |

---

## View 4: By Risk Priority

### Critical (Data, Security & Control Integrity)
- **F-01 [Fixed]**: `SWITCH_MONITOR`, `GET_MONITORS`, `PRIVACY_MODE`, `SET_QUALITY`, `FILE_START`, and `FILE_END` now check `session_state.get("authenticated")`.
- **F-02 [Fixed]**: Authentication fails with `AUTH_FAILED` when binary token is missing or invalid.
- **F-03 [Fixed]**: Default fallback changed to `.get("authenticated", False)` so unauthenticated connections trigger immediate stream teardown.
- **F-04 [Fixed]**: `HostDaemon` consumes `security.get_master_key()` to benefit from Windows DPAPI at rest, and defaults to `"123456"`.

### Moderate (System Reliability & Resilience)
- **F-05 [Fixed]**: `RemoteClient._receive_loop` cleanly disconnects upon receiving `CLIENT_DISCONNECTED`.
- **F-06 [Fixed]**: `FileTransferManager.cancel_all_transfers()` closes open handles upon client disconnection.
- **F-07 [Fixed]**: Brute-force rate limiting (`is_rate_limited`, `record_failed_attempt`, `reset_attempts`) wired into daemon.
- **F-08 [Fixed]**: Embedded `SignalingBroker` starts automatically when host daemon runs standalone.
- **F-09 [Fixed]**: Dynamic credentials lookup implemented in test suite.
- **F-10 [Fixed]**: Clean broker termination on host toggle in `client_viewer.py`.

### Cosmetic (Style, Documentation & Branding)
- **F-11 [Fixed]**: Storage directories updated to `AetherDesk` branding with seamless fallback to legacy paths.
- **F-12 [Fixed]**: Badges and documentation in `README.md` and glyph spec in `DESIGN.md` synchronized.
