import math
import time
import logging
import threading
from typing import Optional, Dict, Any, Tuple, Callable

logger = logging.getLogger("InputWorker")

ALLOWED_ACTIONS = {
    # Mouse move / trackpad
    "mouse_move", "move", "touch_move", "cursor_move",
    # Mouse down
    "mouse_down", "touch_down", "down", "pointer_down",
    # Mouse up
    "mouse_up", "touch_up", "up", "pointer_up",
    # Clicks / taps
    "mouse_click", "click", "tap", "touch_tap", "pointer_click",
    # Right clicks
    "right_click", "context_menu", "secondary_click",
    # Double clicks
    "mouse_double_click", "double_click", "double_tap",
    # Scroll / Wheel
    "mouse_wheel", "wheel", "scroll",
    # Keyboard down / up
    "key_down", "keydown",
    "key_up", "keyup",
    # Text / unicode input
    "unicode_char", "char", "text", "type_text", "string", "input_text",
    # Shortcuts / combos
    "special", "command", "shortcut", "hotkey", "key_combination", "combo",
    # Clipboard
    "clipboard", "set_clipboard", "copy_text", "paste_text"
}

ALLOWED_BUTTONS = {"left", "right", "middle", "primary", "secondary", "center", 0, 1, 2}

RELEASE_ACTIONS = {"key_up", "keyup", "mouse_up", "touch_up", "up", "pointer_up"}

def validate_input_command(cmd: Any) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Strict validation of incoming input command structure and bounds.
    Protects host against malformed, malicious, or out-of-range payloads.
    """
    if not isinstance(cmd, dict):
        return False, "Input command must be a dictionary", None

    action = str(
        cmd.get("type", "")
        or cmd.get("action", "")
        or cmd.get("event", "")
        or cmd.get("name", "")
        or ""
    ).lower().strip()

    nx = cmd.get("x") if cmd.get("x") is not None else cmd.get("norm_x")
    if nx is None:
        nx = cmd.get("clientX") or cmd.get("touchX") or cmd.get("px")

    ny = cmd.get("y") if cmd.get("y") is not None else cmd.get("norm_y")
    if ny is None:
        ny = cmd.get("clientY") or cmd.get("touchY") or cmd.get("py")

    dx = cmd.get("dx")
    dy = cmd.get("dy")
    btn = cmd.get("button", "left")

    # Inferred action from flags or coordinates
    if "down" in cmd and not action:
        action = "mouse_down" if cmd["down"] else "mouse_up"
    elif not action and nx is not None and ny is not None:
        action = "click" if ("button" in cmd or "click" in cmd) else "mouse_move"

    if not action:
        return False, "Missing action or identifiable event type", None

    if action not in ALLOWED_ACTIONS:
        return False, f"Action '{action}' is not in allowed input actions", None

    # Validate coordinates
    clean_nx = None
    if nx is not None:
        try:
            clean_nx = float(nx)
            if not math.isfinite(clean_nx) or clean_nx < -50000.0 or clean_nx > 50000.0:
                return False, f"Coordinate x/norm_x out of reasonable range: {nx}", None
        except (ValueError, TypeError):
            return False, f"Coordinate x/norm_x must be numeric: {nx}", None

    clean_ny = None
    if ny is not None:
        try:
            clean_ny = float(ny)
            if not math.isfinite(clean_ny) or clean_ny < -50000.0 or clean_ny > 50000.0:
                return False, f"Coordinate y/norm_y out of reasonable range: {ny}", None
        except (ValueError, TypeError):
            return False, f"Coordinate y/norm_y must be numeric: {ny}", None

    # Validate relative offsets
    clean_dx = None
    if dx is not None:
        try:
            clean_dx = float(dx)
            if not math.isfinite(clean_dx) or abs(clean_dx) > 50000.0:
                return False, f"Relative offset dx out of bounds: {dx}", None
        except (ValueError, TypeError):
            return False, f"Relative offset dx must be numeric: {dx}", None

    clean_dy = None
    if dy is not None:
        try:
            clean_dy = float(dy)
            if not math.isfinite(clean_dy) or abs(clean_dy) > 50000.0:
                return False, f"Relative offset dy out of bounds: {dy}", None
        except (ValueError, TypeError):
            return False, f"Relative offset dy must be numeric: {dy}", None

    # Validate button
    if btn is not None:
        if isinstance(btn, int):
            btn = {0: "left", 1: "middle", 2: "right"}.get(btn, "left")
        btn_str = str(btn).lower().strip()
        if btn_str not in ALLOWED_BUTTONS:
            return False, f"Invalid mouse button: {btn}", None

    # Validate wheel delta
    delta = cmd.get("delta") or cmd.get("dy")
    clean_delta = 0
    if action in ("mouse_wheel", "wheel", "scroll") and delta is not None:
        try:
            clean_delta = int(delta)
            if abs(clean_delta) > 50000:
                return False, f"Wheel delta out of bounds: {delta}", None
        except (ValueError, TypeError):
            return False, f"Wheel delta must be integer: {delta}", None

    # Validate key codes
    vk = cmd.get("vk")
    clean_vk = 0
    if vk is not None:
        try:
            clean_vk = int(vk)
            if clean_vk < 0 or clean_vk > 255:
                return False, f"Virtual key code vk out of byte range [0, 255]: {vk}", None
        except (ValueError, TypeError):
            return False, f"Virtual key code vk must be integer: {vk}", None

    # Validate text/character input
    text_val = cmd.get("text") or cmd.get("char") or cmd.get("string")
    if text_val is not None:
        if not isinstance(text_val, str):
            text_val = str(text_val)
        if len(text_val) > 4096:
            return False, f"Text payload exceeds maximum allowed length (4096): {len(text_val)}", None

    # Validate shortcut / key combinations
    keys_list = cmd.get("keys") or cmd.get("vk_list")
    clean_keys = None
    if keys_list is not None:
        if not isinstance(keys_list, list) or len(keys_list) > 16:
            return False, "Key combinations must be a list with at most 16 items", None
        clean_keys = []
        for k in keys_list:
            if isinstance(k, int):
                if k < 1 or k > 255:
                    return False, f"Invalid key code in combination: {k}", None
                clean_keys.append(k)
            elif isinstance(k, str):
                if len(k) > 32:
                    return False, f"Key name in combination too long: {k}", None
                clean_keys.append(k)
            else:
                return False, f"Unsupported item in key combination: {k}", None

    # Preserve correlation sequence and transport identifier
    clean_seq = None
    seq_val = cmd.get("seq")
    if seq_val is not None:
        try:
            clean_seq = int(seq_val)
        except (ValueError, TypeError):
            clean_seq = None

    clean_transport = str(cmd.get("transport", ""))[:32] if cmd.get("transport") else None

    sanitized = {
        "action": action,
        "norm_x": clean_nx,
        "norm_y": clean_ny,
        "dx": clean_dx,
        "dy": clean_dy,
        "button": btn,
        "delta": clean_delta,
        "vk": clean_vk,
        "key": cmd.get("key"),
        "text": text_val,
        "keys": clean_keys,
        "command": cmd.get("command") or cmd.get("shortcut"),
        "seq": clean_seq,
        "transport": clean_transport,
    }
    return True, None, sanitized


class CoalescingQueue:
    """
    Bounded event queue with intelligent mouse move coalescing and reserved slots for release events.
    Replaces pending consecutive move events with the newest point while strictly preserving
    order and delivery of all discrete events (clicks, keys) and reserving headroom for key_up/mouse_up.
    """

    def __init__(self, maxsize: int = 256, reserve_releases: Optional[int] = None):
        self.maxsize = maxsize
        if reserve_releases is None:
            self.reserve_releases = min(16, maxsize // 8) if maxsize >= 8 else 0
        else:
            self.reserve_releases = min(reserve_releases, maxsize)
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._idle_cond = threading.Condition(self._lock)
        self._items = []
        self._executing = False

    def put(self, cmd: dict) -> bool:
        with self._lock:
            action = cmd.get("action", "")
            is_move = action in ("mouse_move", "move", "touch_move", "cursor_move")
            is_release = action in RELEASE_ACTIONS

            # Coalesce consecutive mouse moves
            if is_move and self._items:
                last_cmd = self._items[-1]
                if last_cmd.get("action") in ("mouse_move", "move", "touch_move", "cursor_move"):
                    last_is_rel = (last_cmd.get("dx") is not None or last_cmd.get("dy") is not None) and (last_cmd.get("norm_x") is None and last_cmd.get("norm_y") is None)
                    curr_is_rel = (cmd.get("dx") is not None or cmd.get("dy") is not None) and (cmd.get("norm_x") is None and cmd.get("norm_y") is None)

                    if last_is_rel and curr_is_rel:
                        # Sum relative deltas
                        last_cmd["dx"] = (last_cmd.get("dx") or 0.0) + (cmd.get("dx") or 0.0)
                        last_cmd["dy"] = (last_cmd.get("dy") or 0.0) + (cmd.get("dy") or 0.0)
                        if cmd.get("seq") is not None:
                            last_cmd["seq"] = cmd.get("seq")
                        if cmd.get("transport") is not None:
                            last_cmd["transport"] = cmd.get("transport")
                        return True
                    elif not last_is_rel and not curr_is_rel:
                        # Absolute coordinates: overwrite with latest target
                        last_cmd["norm_x"] = cmd.get("norm_x")
                        last_cmd["norm_y"] = cmd.get("norm_y")
                        last_cmd["dx"] = cmd.get("dx")
                        last_cmd["dy"] = cmd.get("dy")
                        if cmd.get("seq") is not None:
                            last_cmd["seq"] = cmd.get("seq")
                        if cmd.get("transport") is not None:
                            last_cmd["transport"] = cmd.get("transport")
                        return True

            # Reserve headroom specifically for key_up/mouse_up events
            effective_limit = self.maxsize if is_release else max(1, self.maxsize - self.reserve_releases)

            if len(self._items) >= effective_limit:
                if is_move:
                    return False
                # Try to drop oldest mouse move to make room
                for i in range(len(self._items)):
                    if self._items[i].get("action") in ("mouse_move", "move", "touch_move", "cursor_move"):
                        self._items.pop(i)
                        break
                else:
                    # Queue is full of discrete events and no capacity available
                    return False

            self._items.append(cmd)
            self._not_empty.notify()
            return True

    def pop_for_execution(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Atomically pop the next command and mark executing state under queue lock."""
        with self._lock:
            if not self._items:
                if not self._not_empty.wait(timeout=timeout):
                    return None
                if not self._items:
                    return None
            cmd = self._items.pop(0)
            self._executing = True
            return cmd

    def finish_execution(self):
        """Atomically mark command completion and notify idle waiters under queue lock."""
        with self._lock:
            self._executing = False
            self._idle_cond.notify_all()

    def is_idle(self) -> bool:
        """Atomically check whether the queue has no pending items and no active execution."""
        with self._lock:
            return len(self._items) == 0 and not self._executing

    def get(self, timeout: Optional[float] = None) -> Optional[dict]:
        with self._lock:
            if not self._items:
                if not self._not_empty.wait(timeout=timeout):
                    return None
                if not self._items:
                    return None
            return self._items.pop(0)

    def clear(self):
        with self._lock:
            self._items.clear()
            if not self._executing:
                self._idle_cond.notify_all()

    def qsize(self) -> int:
        with self._lock:
            return len(self._items)


class InputWorker:
    """
    Dedicated worker thread executing input events off the network loop.
    Enforces thread-safety, bounds checking, order preservation, and cleanup on disconnect.
    """

    def __init__(self, input_handler, get_bounds_cb: Optional[Callable[[], Tuple[Any, Any]]] = None, maxsize: int = 256, clipboard: Any = None, reserve_releases: Optional[int] = None):
        self.input_handler = input_handler
        self.get_bounds_cb = get_bounds_cb
        self.clipboard = clipboard
        self.queue = CoalescingQueue(maxsize=maxsize, reserve_releases=reserve_releases)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._exec_lock = threading.Lock()
        self._release_pending = False

    def start(self):
        """Start the worker thread."""
        if self._running and self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker_loop, name="InputWorkerThread", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        """Stop worker loop, wait for in-flight event, safely release stuck inputs, and close handler."""
        self._running = False
        self.queue.clear()
        with self.queue._lock:
            self.queue._not_empty.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("InputWorker thread did not terminate within timeout; skipping input_handler close to avoid race condition")
                return False
        ok = self.release_all(timeout=timeout)
        if not ok:
            logger.error("InputWorker.stop: could not acquire lock to release inputs within timeout.")
        if hasattr(self.input_handler, "close"):
            try:
                self.input_handler.close()
            except Exception as e:
                logger.debug(f"InputWorker.stop: input_handler close error: {e}")
        return ok

    def enqueue(self, cmd: dict) -> bool:
        """Enqueue validated input command for asynchronous injection. Returns False if rejected."""
        ok = self.queue.put(cmd)
        action = cmd.get("action", "")
        seq = cmd.get("seq")
        transport = cmd.get("transport")
        if not ok:
            if action in RELEASE_ACTIONS:
                self._handle_rejected_release(cmd)
            logger.warning(f"[INPUT_ENQUEUE] REJECTED (queue full): seq={seq} transport={transport} action={action}")
            return False
        if action not in ("mouse_move", "move", "touch_move", "cursor_move"):
            logger.info(f"[INPUT_ENQUEUE] seq={seq} transport={transport} action={action} queue_depth={len(self.queue._items)}")
        else:
            cnt = getattr(self, "_enqueued_move_count", 0) + 1
            self._enqueued_move_count = cnt
            if cnt % 100 == 0:
                logger.debug(f"[INPUT_ENQUEUE] transport={transport} move_count={cnt} queue_depth={len(self.queue._items)} last_seq={seq}")
        return True

    def _handle_rejected_release(self, cmd: dict):
        """
        Emergency safety recovery: when a release action (key_up / mouse_up) cannot be queued,
        safely drain pending queued events so no stale 'down' executes later, and release all inputs
        through the single synchronized protected path (release_all) coordinating with the worker.
        """
        action = cmd.get("action", "")
        logger.error(
            f"Input queue saturated: cannot enqueue release '{action}' in order. "
            "Purging pending queue and releasing all inputs through synchronized worker path."
        )
        self.clear()
        self.release_all(timeout=1.0)

    def clear(self):
        """Clear all pending events in queue (e.g. on session disconnect)."""
        self.queue.clear()

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait until all queued events AND any currently executing command have finished."""
        deadline = time.time() + timeout
        with self.queue._lock:
            while not (len(self.queue._items) == 0 and not self.queue._executing):
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.queue._idle_cond.wait(timeout=min(0.01, max(0.001, remaining)))
            return True

    def release_all(self, timeout: float = 1.0) -> bool:
        """Release all keys and mouse buttons safely through the input handler."""
        # Use bounded timeout lock to prevent permanent deadlock; NEVER run concurrent injection without lock
        acquired = self._exec_lock.acquire(timeout=timeout)
        if not acquired:
            self._release_pending = True
            logger.error(
                f"InputWorker lock acquisition timed out ({timeout}s) in release_all. "
                "Active command still executing; concurrent injection aborted to prevent state corruption. "
                "Scheduled release_pending for execution as soon as lock is freed."
            )
            return False

        try:
            self._release_pending = False
            if hasattr(self.input_handler, "release_all_inputs"):
                self.input_handler.release_all_inputs()
            elif hasattr(self.input_handler, "release_all_keys"):
                self.input_handler.release_all_keys()
            return True
        except Exception as e:
            logger.error(f"Error releasing inputs on disconnect: {e}")
            return False
        finally:
            self._exec_lock.release()

    def recover_inputs(self, timeout: float = 2.0) -> bool:
        """
        Recover input state after a worker timeout or incident.
        Clears pending queue and safely releases all inputs under lock.
        """
        self.clear()
        return self.release_all(timeout=timeout)

    def execute_now(self, cmd: dict):
        """Direct synchronous execution (with bounds query and thread lock)."""
        with self.queue._lock:
            self.queue._executing = True
        try:
            with self._exec_lock:
                self._execute_command(cmd)
        finally:
            self.queue.finish_execution()

    def _worker_loop(self):
        """Background thread main loop."""
        # Attach thread to active desktop
        try:
            if hasattr(self.input_handler, "_ensure_input_desktop"):
                self.input_handler._ensure_input_desktop(force=True)
        except Exception:
            pass

        while self._running:
            cmd = self.queue.pop_for_execution(timeout=0.05)
            if not cmd or not self._running:
                if cmd:
                    self.queue.finish_execution()
                if self._release_pending:
                    with self._exec_lock:
                        if self._release_pending:
                            self._release_pending = False
                            if hasattr(self.input_handler, "release_all_inputs"):
                                self.input_handler.release_all_inputs()
                            elif hasattr(self.input_handler, "release_all_keys"):
                                self.input_handler.release_all_keys()
                continue

            action = cmd.get("action", "")
            seq = cmd.get("seq")
            transport = cmd.get("transport")
            is_move = action in ("mouse_move", "move", "touch_move", "cursor_move")
            status = "worker_error"
            try:
                with self._exec_lock:
                    if not is_move:
                        logger.info(f"[INPUT_DISPATCH] seq={seq} transport={transport} action={action}")
                    else:
                        cnt = getattr(self, "_dispatched_move_count", 0) + 1
                        self._dispatched_move_count = cnt
                        if cnt % 100 == 0:
                            logger.debug(f"[INPUT_DISPATCH] transport={transport} move_count={cnt} last_seq={seq}")

                    try:
                        self._execute_command(cmd)
                        status = "handler_completed"
                    except Exception as exc:
                        status = "handler_error"
                        logger.error(
                            f"[INPUT_ERROR] seq={seq} transport={transport} action={action} "
                            f"exception_type={type(exc).__name__}"
                        )

                    if self._release_pending:
                        try:
                            if hasattr(self.input_handler, "release_all_inputs"):
                                self.input_handler.release_all_inputs()
                            elif hasattr(self.input_handler, "release_all_keys"):
                                self.input_handler.release_all_keys()
                            self._release_pending = False
                        except Exception as exc:
                            logger.error(
                                f"[INPUT_RELEASE] seq={seq} transport={transport} action={action} "
                                f"exception_type={type(exc).__name__}"
                            )
            except Exception as exc:
                status = "worker_error"
                logger.error(
                    f"[INPUT_WORKER_ERROR] seq={seq} transport={transport} action={action} "
                    f"exception_type={type(exc).__name__}"
                )
            finally:
                try:
                    if not is_move:
                        logger.info(f"[INPUT_COMPLETE] seq={seq} transport={transport} action={action} status={status}")
                    else:
                        cnt_comp = getattr(self, "_completed_move_count", 0) + 1
                        self._completed_move_count = cnt_comp
                        if cnt_comp % 100 == 0:
                            logger.debug(
                                f"[INPUT_COMPLETE] transport={transport} move_count={cnt_comp} "
                                f"last_seq={seq} status={status}"
                            )
                finally:
                    self.queue.finish_execution()

    def _execute_command(self, cmd: dict):
        """Execute a single sanitized input command on the input handler."""
        action = cmd.get("action", "")
        nx = cmd.get("norm_x")
        ny = cmd.get("norm_y")
        dx = cmd.get("dx")
        dy = cmd.get("dy")
        btn = cmd.get("button", "left")
        seq = cmd.get("seq")

        mon_bounds = None
        virt_bounds = None
        if self.get_bounds_cb:
            try:
                mon_bounds, virt_bounds = self.get_bounds_cb()
            except Exception:
                pass

        # 1. Mouse Move / Drag / Trackpad
        if action in ("mouse_move", "move", "touch_move", "cursor_move"):
            if dx is not None and dy is not None and (nx is None or ny is None):
                self.input_handler.move_mouse_relative(float(dx), float(dy), seq=seq)
            elif nx is not None and ny is not None:
                self.input_handler.move_mouse(float(nx), float(ny), monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 2. Mouse Down
        elif action in ("mouse_down", "touch_down", "down", "pointer_down"):
            self.input_handler.mouse_down(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 3. Mouse Up
        elif action in ("mouse_up", "touch_up", "up", "pointer_up"):
            self.input_handler.mouse_up(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 4. Click / Tap
        elif action in ("mouse_click", "click", "tap", "touch_tap", "pointer_click"):
            self.input_handler.mouse_click(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 5. Right Click
        elif action in ("right_click", "context_menu", "secondary_click"):
            self.input_handler.mouse_click("right", norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 6. Double Click
        elif action in ("mouse_double_click", "double_click", "double_tap"):
            self.input_handler.mouse_double_click(btn, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 7. Mouse Wheel / Scroll
        elif action in ("mouse_wheel", "wheel", "scroll"):
            delta = cmd.get("delta") or 0
            self.input_handler.mouse_wheel(delta, norm_x=nx, norm_y=ny, monitor_bounds=mon_bounds, virtual_bounds=virt_bounds, seq=seq)

        # 8. Keyboard Key Down
        elif action in ("key_down", "keydown"):
            vk = cmd.get("vk", 0)
            char = cmd.get("text")
            if (not vk or vk == 0) and char:
                self.input_handler.send_unicode_char(char, seq=seq)
            elif vk:
                self.input_handler.key_down(int(vk), seq=seq)
            elif cmd.get("key"):
                k = str(cmd.get("key")).lower()
                from src.input_handler import VK_MAP
                if k in VK_MAP:
                    self.input_handler.key_down(VK_MAP[k], seq=seq)
                else:
                    self.input_handler.send_unicode_char(cmd.get("key"), seq=seq)

        # 9. Keyboard Key Up
        elif action in ("key_up", "keyup"):
            vk = cmd.get("vk", 0)
            if vk:
                self.input_handler.key_up(int(vk), seq=seq)
            elif cmd.get("key"):
                k = str(cmd.get("key")).lower()
                from src.input_handler import VK_MAP
                if k in VK_MAP:
                    self.input_handler.key_up(VK_MAP[k], seq=seq)

        # 10. Unicode Text / Typing
        elif action in ("unicode_char", "char", "text", "type_text", "string", "input_text"):
            text_val = cmd.get("text", "")
            if text_val:
                self.input_handler.send_unicode_char(text_val, seq=seq)

        # 11. Special Shortcuts / Combos / Hotkeys
        elif action in ("special", "command", "shortcut", "hotkey", "key_combination", "combo"):
            cmd_str = cmd.get("command", "")
            keys_list = cmd.get("keys")
            if keys_list:
                if isinstance(keys_list[0], int):
                    self.input_handler.send_key_combination(keys_list, seq=seq)
                else:
                    self.input_handler.send_key_combination_by_names(keys_list, seq=seq)
            elif cmd_str:
                self.input_handler.send_special(cmd_str, seq=seq)

        # 12. Direct Clipboard Set / Paste via Input Command
        elif action in ("clipboard", "set_clipboard", "copy_text", "paste_text"):
            text_val = cmd.get("text") or cmd.get("data") or ""
            if text_val:
                if self.clipboard:
                    self.clipboard.set_text(text_val, update_last=True)
                else:
                    try:
                        from src.transfer import ClipboardManager
                        ClipboardManager().set_text(text_val, update_last=True)
                    except Exception as e:
                        logger.warning(f"Failed to set clipboard text: {e}")
