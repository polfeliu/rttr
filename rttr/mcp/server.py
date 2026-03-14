from __future__ import annotations

import base64
import contextlib
import datetime
import io
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pythoncom
from fastmcp import FastMCP
from pywinauto import Desktop
from pywinauto.keyboard import send_keys as pw_send_keys

from rttr.base import UIA_ERRORS, BaseModel

if TYPE_CHECKING:
    from pywinauto.base_wrapper import BaseWrapper

# ---------------------------------------------------------------------------
# Dynamic app class loading
# ---------------------------------------------------------------------------
# Set RTTR_APP_MODULE to "module.path:ClassName" to specify the application
# class.  Falls back to a BaseModel stub if not set.

_APP_SPEC = os.environ.get("RTTR_APP_MODULE", "")
_AppClass: type[BaseModel] = BaseModel  # default, overridden below

if _APP_SPEC:
    _mod_path, _, _cls_name = _APP_SPEC.partition(":")
    if _mod_path and _cls_name:
        import importlib

        _app_mod = importlib.import_module(_mod_path)
        _AppClass = getattr(_app_mod, _cls_name)

# ---------------------------------------------------------------------------
# Server-level logging
# ---------------------------------------------------------------------------
# Rotating log: up to 5 MB per file, 3 backup copies.
# Log path: .github/logs/mcp_server.log (relative to repo root).


def _setup_server_logger() -> logging.Logger:
    """Configure and return the MCP server logger.

    Returns:
        Configured Logger instance writing to .github/logs/mcp_server.log.
    """
    _log = logging.getLogger("rttr_mcp")
    if _log.handlers:
        return _log  # Already configured (e.g. on re-import in tests)
    _log.setLevel(logging.DEBUG)
    log_dir = Path(__file__).parents[3] / ".github" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "mcp_server.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    )
    _log.addHandler(handler)
    return _log


server_logger = _setup_server_logger()


def _log_tool[F: Callable[..., Any]](func: F) -> F:
    """Decorator: log entry, exit, duration, and exceptions for an MCP tool function.

    Returns:
        The wrapped function.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        name = func.__name__
        server_logger.info(">> tool: %s  args=%s kwargs=%s", name, args, kwargs)
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
            dt = time.monotonic() - t0
            server_logger.info("<< tool: %s  ok  %.3fs", name, dt)
            return result
        except Exception as exc:
            dt = time.monotonic() - t0
            server_logger.exception("<< tool: %s  ERROR %.3fs: %s", name, dt, exc)
            raise

    return wrapper  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Module-level element registry
# ---------------------------------------------------------------------------
# UIA elements are often not backed by a Win32 HWND, so we maintain a
# session-scoped registry mapping integer slot IDs ΓåÆ pywinauto element wrappers.
# Callers use the returned 'handle' (slot ID) to reference elements across tool calls.

_ELEMENT_REGISTRY: dict[int, Any] = {}
_RUNTIME_TO_SLOT: dict[str, int] = {}  # dedup by runtime_id
_slot_counter: int = 0


def _runtime_key(el: BaseWrapper) -> str:
    """Return a stable string key for an element (runtime_id or fallback).

    Returns:
        A string like "1.2.3.4" representing the element's UIA runtime ID.
    """
    try:
        rid = el.element_info.runtime_id
        return ".".join(str(x) for x in rid)
    except UIA_ERRORS:
        return ""


def _register_element(el: BaseWrapper) -> int:
    """Register an element in the cache and return its slot ID.

    Returns:
        Integer slot ID that can be passed back to interaction tools.
    """
    global _slot_counter
    key = _runtime_key(el)
    if key and key in _RUNTIME_TO_SLOT:
        return _RUNTIME_TO_SLOT[key]
    _slot_counter += 1
    slot = _slot_counter
    _ELEMENT_REGISTRY[slot] = el
    if key:
        _RUNTIME_TO_SLOT[key] = slot
    return slot


def _resolve_element(slot: int) -> BaseWrapper:
    """Look up an element from the registry by slot ID.

    Returns:
        The cached pywinauto element wrapper.

    Raises:
        RuntimeError: If the slot ID is unknown.
    """
    el = _ELEMENT_REGISTRY.get(slot)
    if el is None:
        raise RuntimeError(f"Element slot {slot} not found. Re-run find_elements to refresh.")
    return el


def _get_app() -> BaseModel:
    """Return the active application instance.

    Raises:
        RuntimeError: If the application class has no ``get_active_instance`` method.
    """
    get_active = getattr(_AppClass, "get_active_instance", None)
    if get_active is not None:
        return get_active()
    raise RuntimeError(
        f"{_AppClass.__name__} has no get_active_instance() classmethod. "
        "Launch the application first via app_launch."
    )


def _all_process_windows(app_inst: BaseModel) -> list[Any]:
    """Return all top-level windows belonging to the application process.

    Returns:
        List of pywinauto window wrappers.
    """
    pythoncom.CoInitialize()
    pid = app_inst.element.process_id()
    result = []
    for w in Desktop(backend="uia").windows():
        try:
            if w.process_id() == pid:
                result.append(w)
        except UIA_ERRORS:
            continue
    return result


def _element_to_dict(el: BaseWrapper, *, register: bool = True) -> dict[str, Any]:
    """Serialize a pywinauto element to a plain dict.

    Returns:
        A dict with handle (slot ID), control_type, class_name, auto_id, title,
        rect, center, is_enabled, and is_visible fields.
    """
    d: dict[str, Any] = {}
    if register:
        d["handle"] = _register_element(el)
    try:
        info = el.element_info
        d["control_type"] = str(info.control_type)
        d["class_name"] = str(info.class_name)
        d["auto_id"] = str(info.auto_id)
    except UIA_ERRORS:
        # Fallback: use wrapper methods when element_info properties fail
        try:
            d["control_type"] = el.friendly_class_name()
        except UIA_ERRORS:
            d["control_type"] = ""
        try:
            d["class_name"] = el.class_name()
        except UIA_ERRORS:
            d["class_name"] = ""
        d["auto_id"] = ""
    try:
        d["title"] = el.window_text()
    except UIA_ERRORS:
        d["title"] = ""
    try:
        r = el.rectangle()
        d["rect"] = {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom}
        d["center"] = {"x": (r.left + r.right) // 2, "y": (r.top + r.bottom) // 2}
    except UIA_ERRORS:
        d["rect"] = None
        d["center"] = None
    try:
        d["is_enabled"] = bool(el.is_enabled())
    except UIA_ERRORS:
        d["is_enabled"] = None
    try:
        d["is_visible"] = bool(el.is_visible())
    except UIA_ERRORS:
        d["is_visible"] = None
    return d


# ---------------------------------------------------------------------------
# Ephemeral POM worker
# ---------------------------------------------------------------------------


class PomEphemeralServer:
    """Manages the lifecycle of the ephemeral POM worker subprocess.

    The worker process imports all POM modules once at startup.  All
    ``pom_*`` MCP tool calls are forwarded to it via a newline-delimited
    JSON protocol over stdin/stdout.

    Call ``reload()`` (or the ``pom_reload`` MCP tool) to kill the current
    worker and start a fresh one, picking up any edits made to POM files.
    """

    def __init__(self) -> None:
        """Initialise the server with no subprocess yet started."""
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock: threading.Lock = threading.Lock()

    def _start(self) -> None:
        """Launch the worker subprocess.

        Uses the same Python interpreter as the MCP server so the correct
        virtual-environment packages are found.
        """
        worker = Path(__file__).parent / "pom_worker.py"
        cmd = [sys.executable, str(worker)]
        if _APP_SPEC:
            cmd.append(_APP_SPEC)
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _ensure_running(self) -> subprocess.Popen[bytes]:
        """Return the active worker process, starting it lazily if needed.

        Returns:
            The running subprocess handle.
        """
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None  # noqa: S101
        return self._proc

    def call(self, payload: dict[str, Any]) -> Any:  # noqa: ANN401 ΓÇö JSON-deserialized result, type depends on POM method
        """Send one request to the worker and return the parsed result.

        Args:
            payload: A dict that will be JSON-encoded and sent as one line.

        Returns:
            The ``result`` field from the worker's JSON response.

        Raises:
            RuntimeError: If the worker exits before responding or reports
                an error.
        """
        action = payload.get("action", "unknown")
        widget = payload.get("widget", "")
        method = payload.get("method", "")
        label = f"{action}({widget}.{method})" if method else action
        server_logger.info(">> pom: %s", label)
        t0 = time.monotonic()
        try:
            result = self._call_raw(payload)
            dt = time.monotonic() - t0
            server_logger.info("<< pom: %s  ok  %.3fs", label, dt)
            return result
        except Exception as exc:
            dt = time.monotonic() - t0
            server_logger.exception("<< pom: %s  ERROR %.3fs: %s", label, dt, exc)
            raise

    def _call_raw(self, payload: dict[str, Any]) -> Any:  # noqa: ANN401 ΓÇö JSON-deserialized result, type depends on POM method
        """Send one request to the worker and return the parsed result.

        Args:
            payload: A dict that will be JSON-encoded and sent as one line.

        Returns:
            The ``result`` field from the worker's JSON response.

        Raises:
            RuntimeError: If the worker exits before responding or reports
                an error.
        """
        with self._lock:
            proc = self._ensure_running()
            assert proc.stdin and proc.stdout  # noqa: S101
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            proc.stdin.flush()
            response_line = proc.stdout.readline()
            if not response_line:
                raise RuntimeError(
                    "POM worker exited unexpectedly before responding. "
                    "Call pom_reload() to start a fresh worker."
                )
            response: dict[str, Any] = json.loads(response_line)
            if not response.get("ok"):
                raise RuntimeError(f"POM worker error:\n{response.get('error', '(no details)')}")
            return response["result"]

    def reload(self) -> None:
        """Kill the current worker and immediately start a fresh process.

        The new process imports all POM modules from disk so any edits made
        since the last start are picked up.
        """
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
            self._start()


_pom_server = PomEphemeralServer()

# ---------------------------------------------------------------------------
# Agent activity log
# ---------------------------------------------------------------------------
# Maps agent name ΓåÆ JSONL session file path for this MCP server run.
# First call from a given agent creates a new timestamped session file;
# subsequent calls within the same server process append to the same file.

_ACTIVITY_SESSION_FILES: dict[str, Path] = {}
_ACTIVITY_LOCK = threading.Lock()


def _get_activity_session_file(agent: str) -> Path:
    """Return (and lazily create) the JSONL session file for *agent*.

    Returns:
        Path to the agent's current session JSONL file.
    """
    with _ACTIVITY_LOCK:
        if agent not in _ACTIVITY_SESSION_FILES:
            root = Path(__file__).parents[3]  # repo root
            log_dir = root / ".github" / "agent_activity" / agent
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            _ACTIVITY_SESSION_FILES[agent] = log_dir / f"{ts}_session.jsonl"
        return _ACTIVITY_SESSION_FILES[agent]


# ---------------------------------------------------------------------------
# MCP instance
# ---------------------------------------------------------------------------


_mcp_name = os.environ.get("RTTR_MCP_NAME", "RTTR_MCP")
mcp = FastMCP(name=_mcp_name)


# ---------------------------------------------------------------------------
# Application lifecycle tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def app_launch(
    app_path: str = "",
) -> dict[str, Any]:
    """Launch the application or connect to an already-running instance.

    If the application is already running this tool simply attaches to it and
    returns immediately.  If it is not running, the executable at
    ``app_path`` is started and the tool waits until the main window is
    ready before returning.

    Args:
        app_path: File system path to the application executable.  Only used
            when the process is not already running.

    Returns:
        Dict with ``ok`` (True) and a human-readable ``message``.

    Raises:
        RuntimeError: If the application cannot be launched or connected to
            within the startup timeout.
    """
    pythoncom.CoInitialize()
    kwargs: dict[str, Any] = {}
    if app_path:
        kwargs["app_path"] = app_path
    app_inst = _AppClass(**kwargs)
    launch_fn = getattr(app_inst, "launch_or_connect", None)
    if launch_fn is not None:
        launch_fn()
    return {"ok": True, "message": f"{_AppClass.__name__} is running."}


@mcp.tool
@_log_tool
def app_stop() -> dict[str, Any]:
    """Close the application.

    Sends a graceful close request to the main window first.  If the
    process is still alive after a short wait it is forcibly killed.

    Returns:
        Dict with ``ok`` (True) and a human-readable ``message``.

    Raises:
        RuntimeError: If no running application instance can be found.
    """
    pythoncom.CoInitialize()
    app_inst = _get_app()
    stop_fn = getattr(app_inst, "stop", None)
    if stop_fn is not None:
        stop_fn()
    else:
        raise RuntimeError(f"{_AppClass.__name__} has no stop() method.")
    return {"ok": True, "message": f"{_AppClass.__name__} has been stopped."}


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def dump_all() -> dict[str, Any]:
    """Return structured JSON trees for all top-level application windows.

    Each entry contains handle, title, class_name and a recursive UINode tree.
    UINode fields: control_type, class_name, title, auto_id, is_visible, is_enabled,
    center_x, center_y, children[].

    Note: element handles are NOT embedded in UINode ΓÇô use find_elements to get handles.
    """
    app_inst = _get_app()
    return {"windows": app_inst.dump_all(app_inst.element)}


@mcp.tool
@_log_tool
def list_windows() -> list[dict[str, Any]]:
    """Return a list of all top-level windows belonging to the application process.

    Includes the main window and any dialogs/popups currently open.

    Returns:
        List of dicts, each with handle, title, class_name, process_id, and rect keys.
    """
    app_inst = _get_app()
    result = []
    for w in _all_process_windows(app_inst):
        try:
            r = w.rectangle()
            result.append({
                "handle": _register_element(w),
                "title": w.window_text() or "",
                "class_name": w.class_name() or "",
                "process_id": w.process_id(),
                "rect": {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom},
            })
        except UIA_ERRORS:
            continue
    return result


@mcp.tool
@_log_tool
def find_elements(
    control_type: str | None = None,
    title: str | None = None,
    title_re: str | None = None,
    class_name: str | None = None,
    auto_id: str | None = None,
    enabled_only: bool = False,
    visible_only: bool = True,
) -> list[dict[str, Any]]:
    """Search for UI elements across all application windows matching the given criteria.

    All criteria are optional and ANDed together.
    The handle can be used with click_control, element_info, get_value, type_text, etc.

    Args:
        control_type: UIA control type string, e.g. "Button", "Edit", "ComboBox".
        title: Exact title/text of the element.
        title_re: Regex pattern for the element title.
        class_name: Window class name (Qt class name, e.g. "QPushButton").
        auto_id: UIA AutomationId.
        enabled_only: If True, only return elements that are enabled.
        visible_only: If True (default), only return elements that are visible.

    Returns:
        List of dicts, each with handle, control_type, class_name, title, auto_id,
        rect, center, is_enabled, and is_visible.

    Examples:
        find_elements(control_type="Button")          # all buttons
        find_elements(title_re="(?i)scope")           # elements titled Scope (case-insensitive)
        find_elements(control_type="Edit")            # all text inputs
        find_elements(class_name="QComboBox")         # all combo boxes
    """
    app_inst = _get_app()
    compiled_re = re.compile(title_re) if title_re else None
    matches = []

    def _collect(el: BaseWrapper) -> None:
        """Recursively collect elements matching the search criteria."""
        try:
            info = el.element_info
            el_control_type = str(info.control_type)
            el_class = str(info.class_name)
            el_auto_id = str(info.auto_id)
        except UIA_ERRORS:
            try:
                el_control_type = el.friendly_class_name()
            except UIA_ERRORS:
                el_control_type = ""
            try:
                el_class = el.class_name()
            except UIA_ERRORS:
                el_class = ""
            el_auto_id = ""

        try:
            el_title = el.window_text()
        except UIA_ERRORS:
            el_title = ""

        try:
            is_visible = bool(el.is_visible())
            is_enabled = bool(el.is_enabled())
        except UIA_ERRORS:
            is_visible, is_enabled = True, True

        # Apply filters
        passes = True
        if control_type and el_control_type.lower() != control_type.lower():
            passes = False
        if title and el_title != title:
            passes = False
        if compiled_re and not compiled_re.search(el_title):
            passes = False
        if class_name and el_class != class_name:
            passes = False
        if auto_id and el_auto_id != auto_id:
            passes = False
        if visible_only and not is_visible:
            passes = False
        if enabled_only and not is_enabled:
            passes = False

        if passes:
            matches.append(_element_to_dict(el))

        # Recurse into children
        try:
            for child in el.children():
                _collect(child)
        except UIA_ERRORS:
            pass

    for win in _all_process_windows(app_inst):
        _collect(win)

    return matches


@mcp.tool
@_log_tool
def element_info(handle: int) -> dict[str, Any]:
    """Return detailed properties of a single element identified by its handle.

    Use this to deep-dive into a specific control after discovering it via
    find_elements or dump_str.

    Returns:
        Dict with handle, control_type, class_name, title, auto_id, rect, center,
        is_enabled, is_visible, value, and children (with their handles).
    """
    el = _resolve_element(handle)

    d = _element_to_dict(el)

    try:
        d["value"] = el.legacy_v_get_value()
    except UIA_ERRORS:
        try:
            d["value"] = el.get_value()
        except UIA_ERRORS:
            d["value"] = None

    children: list[dict[str, Any]] = []
    with contextlib.suppress(*UIA_ERRORS):
        children.extend(_element_to_dict(child) for child in el.children())
    d["children"] = children

    return d


@mcp.tool
@_log_tool
def screenshot() -> dict[str, Any]:
    """Capture a screenshot of the entire application main window as a base64 PNG.

    Use this to visually confirm the current state of the application after
    performing interactions, or to orient yourself before starting exploration.

    Returns:
        Dict with a ``png_base64`` key containing the base64-encoded PNG image.

    Raises:
        RuntimeError: If no application windows can be found.
    """
    app_inst = _get_app()
    try:
        img = app_inst.element.capture_as_image()
    except UIA_ERRORS:
        wins = _all_process_windows(app_inst)
        if not wins:
            raise RuntimeError("No application windows found")
        img = wins[0].capture_as_image()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"png_base64": encoded}


@mcp.tool
@_log_tool
def screenshot_window(handle: int) -> dict[str, Any]:
    """Return a base64 PNG screenshot of a specific window identified by handle.

    The handle must be a top-level window handle returned by list_windows.

    Returns:
        Dict with a ``png_base64`` key containing the base64-encoded PNG image.

    Raises:
        RuntimeError: If capture_as_image fails for the given handle.
    """
    el = _resolve_element(handle)
    try:
        img = el.capture_as_image()
    except Exception as e:
        raise RuntimeError(f"capture_as_image failed: {e}") from e
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"png_base64": encoded}


# ---------------------------------------------------------------------------
# Interaction tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def click_control(handle: int) -> dict[str, Any]:
    """Click a UI element identified by its handle.

    Searches all application windows (including dialogs) for the element and
    performs a human-like click using click_input(). Waits 0.5s after clicking
    for the UI to settle.

    Use find_elements or dump_str first to discover handles.

    Returns:
        Dict with ``ok`` (True) and ``handle`` keys.

    Raises:
        RuntimeError: If click_input fails on the resolved element.
    """
    el = _resolve_element(handle)
    with contextlib.suppress(*UIA_ERRORS):
        el.set_focus()
    try:
        el.click_input()
    except Exception as e:
        raise RuntimeError(f"click_input failed: {e}") from e
    time.sleep(0.5)
    return {"ok": True, "handle": handle}


@mcp.tool
@_log_tool
def double_click_control(handle: int) -> dict[str, Any]:
    """Double-click a UI element identified by its handle.

    Returns:
        Dict with ``ok`` (True) and ``handle`` keys.

    Raises:
        RuntimeError: If double_click_input fails on the resolved element.
    """
    el = _resolve_element(handle)
    try:
        el.double_click_input()
    except Exception as e:
        raise RuntimeError(f"double_click_input failed: {e}") from e
    time.sleep(0.5)
    return {"ok": True, "handle": handle}


@mcp.tool
@_log_tool
def right_click_control(handle: int) -> dict[str, Any]:
    """Right-click a UI element identified by its handle.

    Useful for revealing context menus. After calling this, use dump_str or
    find_elements to discover what menu items appeared.

    Returns:
        Dict with ``ok`` (True) and ``handle`` keys.

    Raises:
        RuntimeError: If right_click_input fails on the resolved element.
    """
    el = _resolve_element(handle)
    try:
        el.right_click_input()
    except Exception as e:
        raise RuntimeError(f"right_click_input failed: {e}") from e
    time.sleep(0.5)
    return {"ok": True, "handle": handle}


@mcp.tool
@_log_tool
def type_text(handle: int, text: str, clear_first: bool = False) -> dict[str, Any]:
    """Type text into a UI element identified by its handle.

    Focuses the element, then types the text using keyboard simulation.
    Typical use case: filling in Edit boxes or search fields.

    Args:
        handle: The element handle (from find_elements or element_info).
        text: The text to type. Special characters are passed as-is.
        clear_first: If True, select-all and delete existing content first.

    Returns:
        Dict with ``ok`` (True), ``handle``, and ``text`` keys.

    Raises:
        RuntimeError: If focusing or typing fails on the resolved element.
    """
    el = _resolve_element(handle)
    try:
        el.set_focus()
        el.click_input()
        if clear_first:
            pw_send_keys("^a")
            time.sleep(0.1)
            pw_send_keys("{DELETE}")
            time.sleep(0.1)
        el.type_keys(text, with_spaces=True)
    except Exception as e:
        raise RuntimeError(f"type_text failed: {e}") from e
    time.sleep(0.3)
    return {"ok": True, "handle": handle, "text": text}


@mcp.tool
@_log_tool
def send_keys(keys: str) -> dict[str, Any]:
    """Send keyboard input to the currently focused element.

    Uses pywinauto key syntax. Common patterns:
      "{ENTER}"          - press Enter
      "{ESC}"            - press Escape
      "{TAB}"            - press Tab
      "{F1}" to "{F12}"  - function keys
      "^a"               - Ctrl+A (select all)
      "^c"               - Ctrl+C (copy)
      "^z"               - Ctrl+Z (undo)
      "{VK_DOWN}"        - arrow down
      "{VK_UP}"          - arrow up

    Args:
        keys: The key sequence in pywinauto syntax.

    Returns:
        Dict with ``ok`` (True) and ``keys`` keys.

    Raises:
        RuntimeError: If the key sequence cannot be sent.
    """
    try:
        pw_send_keys(keys)
    except Exception as e:
        raise RuntimeError(f"send_keys failed: {e}") from e
    time.sleep(0.3)
    return {"ok": True, "keys": keys}


@mcp.tool
@_log_tool
def hover_control(handle: int) -> dict[str, Any]:
    """Hover the mouse over a UI element to reveal tooltips or trigger hover states.

    After hovering, call screenshot() to see if any tooltip appeared.

    Returns:
        Dict with ``ok`` (True) and ``handle`` keys.

    Raises:
        RuntimeError: If the hover action fails on the resolved element.
    """
    el = _resolve_element(handle)
    try:
        el.move_mouse_input()
    except Exception as e:
        raise RuntimeError(f"hover failed: {e}") from e
    time.sleep(1.0)
    return {"ok": True, "handle": handle}


# ---------------------------------------------------------------------------
# Value reading / state tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def get_value(handle: int) -> dict[str, Any]:
    """Read the current value or text content of a UI element.

    Tries multiple strategies:
    - legacy_v_get_value() for Edit boxes and spinners (the editable text)
    - get_value() for range controls
    - window_text() as a fallback
    - get_toggle_state() / is_checked() for CheckBox and RadioButton

    Also returns item_texts() for ListBox / ComboBox elements.

    Returns:
        Dict with handle, text, value, is_checked, is_enabled, is_visible, and items keys.
    """
    el = _resolve_element(handle)
    result: dict[str, Any] = {"handle": handle}

    try:
        result["text"] = el.window_text()
    except UIA_ERRORS:
        result["text"] = ""

    try:
        result["value"] = el.legacy_v_get_value()
    except UIA_ERRORS:
        try:
            result["value"] = el.get_value()
        except UIA_ERRORS:
            result["value"] = None

    try:
        result["is_checked"] = bool(el.get_toggle_state())
    except UIA_ERRORS:
        try:
            result["is_checked"] = bool(el.is_checked())
        except UIA_ERRORS:
            result["is_checked"] = None

    try:
        result["is_enabled"] = bool(el.is_enabled())
    except UIA_ERRORS:
        result["is_enabled"] = None

    try:
        result["is_visible"] = bool(el.is_visible())
    except UIA_ERRORS:
        result["is_visible"] = None

    try:
        result["items"] = el.item_texts()
    except UIA_ERRORS:
        result["items"] = None

    return result


@mcp.tool
@_log_tool
def select_combo_item(handle: int, item: str) -> dict[str, Any]:
    """Select an item in a ComboBox by its visible text.

    Args:
        handle: The ComboBox element handle (find via find_elements or dump_str).
        item: The exact text of the item to select.

    Returns:
        Dict with ``ok`` (True), ``handle``, and ``selected`` keys.

    Raises:
        RuntimeError: If the item selection fails.
    """
    el = _resolve_element(handle)
    try:
        el.select(item)
    except Exception as e:
        raise RuntimeError(f"select_combo_item failed: {e}") from e
    time.sleep(0.3)
    return {"ok": True, "handle": handle, "selected": item}


# ---------------------------------------------------------------------------
# POM tools  (dynamic ΓÇô no server restart needed)
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def class_name_tree(max_depth: int = 10) -> str:
    """Return a compact tree of Qt class_name values for the entire app window.

    Unlike dump_str (which shows UIA control types), this shows the raw Qt
    class names that are used as stable anchors in the POMs
    (e.g. ``ScopeView``, ``DriveExplorerItemHeaderView``).  Useful for
    discovering which class_name to use when writing a new POM.

    Args:
        max_depth: Maximum nesting depth to render (default 10).

    Returns:
        A plain-text indented tree where each line is
        ``<indent><class_name>  '<title>'``.
    """
    app_inst = _get_app()
    lines: list[str] = []

    def _walk(el: BaseWrapper, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            cls = str(el.element_info.class_name)
        except UIA_ERRORS:
            try:
                cls = el.class_name()
            except UIA_ERRORS:
                cls = "?"
        try:
            title = str(el.window_text())
        except UIA_ERRORS:
            title = ""
        lines.append(f"{'  ' * depth}{cls}  {title!r}")
        try:
            for child in el.children():
                _walk(child, depth + 1)
        except UIA_ERRORS:
            pass

    _walk(app_inst.element, 0)
    return "\n".join(lines)


@mcp.tool
@_log_tool
def pom_reload() -> dict[str, Any]:
    """Kill and restart the ephemeral POM worker process.

    Call this tool after editing any POM file to ensure the next
    ``pom_call``, ``pom_list_widgets``, or ``pom_list_methods`` call executes
    against the updated code.  The new worker imports every POM module fresh
    from disk when it starts.

    Returns:
        Dict with ``ok`` (True) and a human-readable ``message`` key.
    """
    _pom_server.reload()
    return {"ok": True, "message": "POM worker restarted. All POM imports are fresh."}


@mcp.tool
@_log_tool
def pom_list_widgets() -> list[str]:
    """List the POM widget names available on the application.

    These names correspond to ``@property`` accessors on the app class that
    return a POM widget object.  Pass one of these names to
    ``pom_list_methods`` or ``pom_call``.

    The result reflects the POM code loaded at the last worker start.
    Call ``pom_reload`` to pick up structural changes to the app POM.

    Returns:
        A list of widget name strings, e.g.
        ["drive_explorer", "scope", "signal_generator", ΓÇª].
    """
    result: list[str] = _pom_server.call({"action": "pom_list_widgets"})
    return result


@mcp.tool
@_log_tool
def pom_list_methods(widget: str) -> list[dict[str, str]]:
    """List the public methods available on a POM widget, with signatures.

    Args:
        widget: Widget name as returned by ``pom_list_widgets``
            (e.g. ``"scope"``).

    Returns:
        A list of dicts, each with ``name``, ``signature``, and ``doc`` keys.

    Raises:
        RuntimeError: If the widget name is unknown or the worker fails.
    """
    result: list[dict[str, str]] = _pom_server.call({
        "action": "pom_list_methods",
        "widget": widget,
    })
    return result


@mcp.tool
@_log_tool
def pom_call(
    widget: str,
    method: str,
    kwargs: dict[str, Any] | None = None,
) -> Any:  # noqa: ANN401 ΓÇö dynamic POM dispatch, return type depends on called method
    """Call any public POM method by widget name and method name.

    The call is executed inside the ephemeral POM worker subprocess, which
    imported every POM module fresh at its last start.  Use ``pom_reload``
    after editing POM files to pick up changes without restarting the MCP
    server.

    Args:
        widget: Widget name as returned by ``pom_list_widgets``
            (e.g. ``"scope"``, ``"drive_explorer"``).
        method: Method name as returned by ``pom_list_methods``
            (e.g. ``"start"``, ``"get_drive_names"``).
        kwargs: Optional keyword arguments to pass to the method.  Omit or
            pass ``null`` for methods that take no arguments.

    Returns:
        The method's return value serialised for JSON transport.  pywinauto
        elements are returned as descriptive dicts with ``runtime_id``,
        ``title``, ``class_name``, and ``control_type`` fields.

    Raises:
        RuntimeError: If the widget or method is unknown, the call raises,
            or the worker process fails.
    """
    return _pom_server.call({
        "action": "pom_call",
        "widget": widget,
        "method": method,
        "kwargs": kwargs or {},
    })


@mcp.tool
@_log_tool
def pom_chain(steps: list[dict[str, Any]]) -> Any:  # noqa: ANN401 ΓÇö dynamic POM chain, return type depends on called methods
    """Walk a chain of attribute accesses and method calls on the app POM.

    Lets you traverse nested POM objects without needing top-level widget
    shortcuts.  Each step resolves one attribute or method on the object
    returned by the previous step, starting from the app class.

    Step schema:

    * ``{"step": "name"}``
      ΓÇô pure attribute / property access; the result becomes the next receiver.
    * ``{"step": "name", "kwargs": {...}}``
      ΓÇô method call; the result becomes the next receiver.  Use ``{}`` for
      zero-argument methods.

    Examples::

        # Read CoE timeouts
        pom_chain([
            {"step": "toolbar"},
            {"step": "tools"},
            {"step": "open_coe_timeouts", "kwargs": {}},
            {"step": "get_timeouts",       "kwargs": {}},
        ])

        # Apply CoE timeouts
        pom_chain([
            {"step": "toolbar"},
            {"step": "tools"},
            {"step": "open_coe_timeouts", "kwargs": {}},
            {"step": "apply_timeouts",    "kwargs": {"sdo_read_timeout": 1500000}},
        ])

    Args:
        steps: Ordered list of step dicts as described above.

    Returns:
        The serialised return value of the final step.

    Raises:
        RuntimeError: If any step name cannot be resolved or a call raises.
    """
    return _pom_server.call({"action": "pom_chain", "steps": steps})


@mcp.tool
def log_agent_activity(
    agent: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one activity entry to the agent's current session log.

    Agents should call this tool to track their work for efficiency analysis:
    task start/end, test runs with results, POM changes, etc.
    All entries are timestamped with wall-clock time from the MCP server.

    One JSONL session file per agent is created per MCP server process lifetime
    (i.e. per VS Code session).  Entries are appended line by line.

    Args:
        agent: Agent identifier ΓÇö one of ``pom_builder``, ``test_builder``,
            ``test_debugger``, ``feedback_steering``, or any custom name.
        action: Short action label.  Recommended values:
            ``task_started``, ``task_completed``, ``test_run_started``,
            ``test_run_completed``, ``pom_method_added``, ``pom_reload``,
            ``custom``.
        details: Optional dict of structured metadata.  Examples:
            - For ``test_run_completed``: ``{"files": ["test_scope.py"],
              "passed": 42, "failed": 0, "duration_seconds": 95}``
            - For ``task_started``: ``{"task_file": "10-high_test_builder_ΓÇªtxt"}``
            - For ``pom_method_added``: ``{"pom": "scope.py",
              "method": "get_signals_for_chart"}``

    Returns:
        Dict with ``ok`` (True), ``log_file`` path, and the ``entry`` that
        was written.
    """
    log_file = _get_activity_session_file(agent)
    entry: dict[str, Any] = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "agent": agent,
        "action": action,
        "details": details or {},
    }
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return {"ok": True, "log_file": str(log_file), "entry": entry}


if __name__ == "__main__":
    mcp.run()
