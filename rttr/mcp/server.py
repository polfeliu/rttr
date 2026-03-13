"""RTTR MCP server — generic UI automation and POM testing tools.

Provides element discovery, interaction, screenshots, and POM dispatch
tools for Windows UIA-based UI testing.

The target application is configured via the ``RTTR_APP_MODULE`` environment
variable (format: ``"module.path:ClassName"``).  Default:
``motionlab_integration_tests.pom.app:MotionLabApp``.

Usage::

    python -m rttr.mcp.server
"""

from __future__ import annotations

import base64
import contextlib
import importlib
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

from rttr.base import UIA_ERRORS

if TYPE_CHECKING:
    from pywinauto.base_wrapper import BaseWrapper

# ---------------------------------------------------------------------------
# Server-level logging
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup_server_logger() -> logging.Logger:
    """Configure and return the MCP server logger.

    Returns:
        Configured Logger instance writing to .github/logs/rttr_mcp.log.
    """
    _log = logging.getLogger("rttr_mcp")
    if _log.handlers:
        return _log
    _log.setLevel(logging.DEBUG)
    log_dir = _REPO_ROOT / ".github" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "rttr_mcp.log",
        maxBytes=5 * 1024 * 1024,
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
    """Decorator: log entry, exit, duration, and exceptions for an MCP tool.

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
# Dynamic app class loading
# ---------------------------------------------------------------------------

_APP_MODULE_SPEC = os.environ.get(
    "RTTR_APP_MODULE", "motionlab_integration_tests.pom.app:MotionLabApp"
)
_app_class: type | None = None


def _get_app_class() -> type:
    """Dynamically load and cache the application POM class.

    Reads from the ``RTTR_APP_MODULE`` environment variable
    (format: ``module.path:ClassName``).

    Returns:
        The application class (e.g. MotionLabApp).
    """
    global _app_class  # noqa: PLW0603
    if _app_class is None:
        module_path, _, class_name = _APP_MODULE_SPEC.partition(":")
        mod = importlib.import_module(module_path)
        _app_class = getattr(mod, class_name)
    return _app_class


def _get_app_instance() -> Any:  # noqa: ANN401
    """Return the active app instance via the class's ``get_active_instance()`` method.

    Returns:
        The active application POM instance.
    """
    return _get_app_class().get_active_instance()


# ---------------------------------------------------------------------------
# Element registry
# ---------------------------------------------------------------------------

_ELEMENT_REGISTRY: dict[int, Any] = {}
_RUNTIME_TO_SLOT: dict[str, int] = {}
_slot_counter: int = 0


def _runtime_key(el: BaseWrapper) -> str:
    """Return a stable string key for an element.

    Returns:
        A string representing the element's UIA runtime ID.
    """
    try:
        rid = el.element_info.runtime_id
        return ".".join(str(x) for x in rid)
    except UIA_ERRORS:
        return ""


def _register_element(el: BaseWrapper) -> int:
    """Register an element in the cache and return its slot ID.

    Returns:
        Integer slot ID.
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


def _all_process_windows(app_inst: Any) -> list[Any]:  # noqa: ANN401
    """Return all top-level windows belonging to the app process.

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
        A dict with element properties.
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
# POM worker subprocess
# ---------------------------------------------------------------------------


class PomEphemeralServer:
    """Manages the lifecycle of the ephemeral POM worker subprocess."""

    def __init__(self) -> None:
        """Initialise with no subprocess yet started."""
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock: threading.Lock = threading.Lock()

    def _start(self) -> None:
        """Launch the worker subprocess."""
        worker = Path(__file__).parent / "pom_worker.py"
        self._proc = subprocess.Popen(
            [sys.executable, str(worker), _APP_MODULE_SPEC],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _ensure_running(self) -> subprocess.Popen[bytes]:
        """Return the active worker process, starting lazily if needed.

        Returns:
            The running subprocess handle.
        """
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None  # noqa: S101
        return self._proc

    def call(self, payload: dict[str, Any]) -> Any:  # noqa: ANN401
        """Send one request to the worker and return the parsed result.

        Args:
            payload: A dict sent as JSON.

        Returns:
            The result field from the worker's response.

        Raises:
            RuntimeError: If the worker exits or reports an error.
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

    def _call_raw(self, payload: dict[str, Any]) -> Any:  # noqa: ANN401
        """Send one request and parse the response.

        Args:
            payload: A dict sent as JSON.

        Returns:
            The result field.

        Raises:
            RuntimeError: On worker failure.
        """
        with self._lock:
            proc = self._ensure_running()
            assert proc.stdin and proc.stdout  # noqa: S101
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            proc.stdin.flush()
            response_line = proc.stdout.readline()
            if not response_line:
                raise RuntimeError(
                    "POM worker exited unexpectedly. Call pom_reload() to restart."
                )
            response: dict[str, Any] = json.loads(response_line)
            if not response.get("ok"):
                raise RuntimeError(f"POM worker error:\n{response.get('error', '(no details)')}")
            return response["result"]

    def reload(self) -> None:
        """Kill the current worker and start a fresh one."""
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
# MCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(name="RTTR_MCP")

# ---------------------------------------------------------------------------
# Application lifecycle tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def app_launch(
    app_path: str = r"C:\Program Files (x86)\MotionLab3\motionlab.exe",
) -> dict[str, Any]:
    """Launch the target application or connect to a running instance.

    Args:
        app_path: File system path to the application executable.

    Returns:
        Dict with ``ok`` and ``message``.
    """
    pythoncom.CoInitialize()
    app_cls = _get_app_class()
    app_inst = app_cls(app_path=app_path)
    app_inst.launch_or_connect()
    return {"ok": True, "message": "Application is running."}


@mcp.tool
@_log_tool
def app_stop() -> dict[str, Any]:
    """Close the target application.

    Returns:
        Dict with ``ok`` and ``message``.
    """
    pythoncom.CoInitialize()
    app_inst = _get_app_instance()
    app_inst.stop()
    return {"ok": True, "message": "Application has been stopped."}


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def dump_all() -> dict[str, Any]:
    """Return structured JSON trees for all top-level application windows.

    Returns:
        Dict with a ``windows`` key containing recursive UINode trees.
    """
    app_inst = _get_app_instance()
    return {"windows": app_inst.dump_all()}


@mcp.tool
@_log_tool
def list_windows() -> list[dict[str, Any]]:
    """Return all top-level windows belonging to the application process.

    Returns:
        List of dicts with handle, title, class_name, process_id, and rect.
    """
    app_inst = _get_app_instance()
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
    """Search for UI elements across all application windows matching given criteria.

    All criteria are optional and ANDed together.

    Args:
        control_type: UIA control type string (e.g. "Button", "Edit").
        title: Exact title/text of the element.
        title_re: Regex pattern for the element title.
        class_name: Window class name.
        auto_id: UIA AutomationId.
        enabled_only: Only return enabled elements.
        visible_only: Only return visible elements (default True).

    Returns:
        List of element dicts with handle, control_type, class_name, etc.
    """
    app_inst = _get_app_instance()
    compiled_re = re.compile(title_re) if title_re else None
    matches: list[dict[str, Any]] = []

    def _collect(el: BaseWrapper) -> None:
        try:
            info = el.element_info
            el_ct = str(info.control_type)
            el_cls = str(info.class_name)
            el_aid = str(info.auto_id)
        except UIA_ERRORS:
            try:
                el_ct = el.friendly_class_name()
            except UIA_ERRORS:
                el_ct = ""
            try:
                el_cls = el.class_name()
            except UIA_ERRORS:
                el_cls = ""
            el_aid = ""
        try:
            el_title = el.window_text()
        except UIA_ERRORS:
            el_title = ""
        try:
            is_vis = bool(el.is_visible())
            is_en = bool(el.is_enabled())
        except UIA_ERRORS:
            is_vis, is_en = True, True

        passes = True
        if control_type and el_ct.lower() != control_type.lower():
            passes = False
        if title and el_title != title:
            passes = False
        if compiled_re and not compiled_re.search(el_title):
            passes = False
        if class_name and el_cls != class_name:
            passes = False
        if auto_id and el_aid != auto_id:
            passes = False
        if visible_only and not is_vis:
            passes = False
        if enabled_only and not is_en:
            passes = False

        if passes:
            matches.append(_element_to_dict(el))

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
    """Return detailed properties of a single element.

    Returns:
        Dict with element properties plus value and children.
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
    """Capture a screenshot of the application main window as base64 PNG.

    Returns:
        Dict with ``png_base64`` key.
    """
    app_inst = _get_app_instance()
    try:
        img = app_inst.element.capture_as_image()
    except UIA_ERRORS:
        wins = _all_process_windows(app_inst)
        if not wins:
            raise RuntimeError("No application windows found")
        img = wins[0].capture_as_image()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"png_base64": base64.b64encode(buf.getvalue()).decode("ascii")}


@mcp.tool
@_log_tool
def screenshot_window(handle: int) -> dict[str, Any]:
    """Return a base64 PNG screenshot of a specific window.

    Returns:
        Dict with ``png_base64`` key.
    """
    el = _resolve_element(handle)
    try:
        img = el.capture_as_image()
    except Exception as e:
        raise RuntimeError(f"capture_as_image failed: {e}") from e
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"png_base64": base64.b64encode(buf.getvalue()).decode("ascii")}


# ---------------------------------------------------------------------------
# Interaction tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def click_control(handle: int) -> dict[str, Any]:
    """Click a UI element by handle.

    Returns:
        Dict with ``ok`` and ``handle``.
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
    """Double-click a UI element by handle.

    Returns:
        Dict with ``ok`` and ``handle``.
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
    """Right-click a UI element by handle.

    Returns:
        Dict with ``ok`` and ``handle``.
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
    """Type text into a UI element.

    Args:
        handle: Element handle.
        text: Text to type.
        clear_first: If True, clear existing content first.

    Returns:
        Dict with ``ok``, ``handle``, and ``text``.
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
    """Send keyboard input to the focused element.

    Args:
        keys: Key sequence in pywinauto syntax.

    Returns:
        Dict with ``ok`` and ``keys``.
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
    """Hover the mouse over a UI element.

    Returns:
        Dict with ``ok`` and ``handle``.
    """
    el = _resolve_element(handle)
    try:
        el.move_mouse_input()
    except Exception as e:
        raise RuntimeError(f"hover failed: {e}") from e
    time.sleep(1.0)
    return {"ok": True, "handle": handle}


# ---------------------------------------------------------------------------
# Value reading
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def get_value(handle: int) -> dict[str, Any]:
    """Read the current value or text of a UI element.

    Returns:
        Dict with handle, text, value, is_checked, is_enabled, is_visible, items.
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
        handle: ComboBox element handle.
        item: Exact text of the item to select.

    Returns:
        Dict with ``ok``, ``handle``, and ``selected``.
    """
    el = _resolve_element(handle)
    try:
        el.select(item)
    except Exception as e:
        raise RuntimeError(f"select_combo_item failed: {e}") from e
    time.sleep(0.3)
    return {"ok": True, "handle": handle, "selected": item}


# ---------------------------------------------------------------------------
# POM tools
# ---------------------------------------------------------------------------


@mcp.tool
@_log_tool
def class_name_tree(max_depth: int = 10) -> str:
    """Return a compact tree of class_name values for the application window.

    Args:
        max_depth: Maximum nesting depth (default 10).

    Returns:
        A plain-text indented tree.
    """
    app_inst = _get_app_instance()
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
            title_str = str(el.window_text())
        except UIA_ERRORS:
            title_str = ""
        lines.append(f"{'  ' * depth}{cls}  {title_str!r}")
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
    """Kill and restart the POM worker process to pick up code changes.

    Returns:
        Dict with ``ok`` and ``message``.
    """
    _pom_server.reload()
    return {"ok": True, "message": "POM worker restarted. All POM imports are fresh."}


@mcp.tool
@_log_tool
def pom_list_widgets() -> list[str]:
    """List available POM widget names.

    Returns:
        Sorted list of widget name strings.
    """
    result: list[str] = _pom_server.call({"action": "pom_list_widgets"})
    return result


@mcp.tool
@_log_tool
def pom_list_methods(widget: str) -> list[dict[str, str]]:
    """List public methods on a POM widget with signatures.

    Args:
        widget: Widget name from ``pom_list_widgets``.

    Returns:
        List of dicts with ``name``, ``signature``, and ``doc``.
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
) -> Any:  # noqa: ANN401
    """Call a POM method by widget and method name.

    Args:
        widget: Widget name.
        method: Method name.
        kwargs: Optional keyword arguments.

    Returns:
        Serialised return value of the POM method.
    """
    return _pom_server.call({
        "action": "pom_call",
        "widget": widget,
        "method": method,
        "kwargs": kwargs or {},
    })


@mcp.tool
@_log_tool
def pom_chain(steps: list[dict[str, Any]]) -> Any:  # noqa: ANN401
    """Walk a chain of attribute accesses and method calls on the app POM.

    Each step is ``{"step": "name"}`` for attribute access or
    ``{"step": "name", "kwargs": {...}}`` for a method call.

    Args:
        steps: Ordered list of step dicts.

    Returns:
        Serialised return value of the final step.
    """
    return _pom_server.call({"action": "pom_chain", "steps": steps})


if __name__ == "__main__":
    mcp.run()
