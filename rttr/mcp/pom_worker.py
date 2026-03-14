"""Ephemeral POM worker subprocess for the RTTR MCP server.

Runs as a persistent child process of the MCP server.  The target application
class is loaded dynamically from a command-line argument (format:
``module.path:ClassName``).

Protocol:
    One newline-delimited JSON object per request on stdin.
    One newline-delimited JSON object per response on stdout.
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import json
import sys
import traceback
from typing import Any, get_type_hints

import pythoncom

# ---------------------------------------------------------------------------
# Dynamic app class loading from CLI argument
# ---------------------------------------------------------------------------

_DEFAULT_APP_SPEC = ""
_app_spec = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_APP_SPEC

if not _app_spec:
    print(
        json.dumps({"ok": False, "error": "No app spec provided. Pass module:Class as argv[1]."}),
        flush=True,
    )
    sys.exit(1)

_module_path, _, _class_name = _app_spec.partition(":")
_app_module = importlib.import_module(_module_path)
AppClass: type = getattr(_app_module, _class_name)

# Import BaseModel from rttr for widget type detection
from rttr.base import BaseModel  # noqa: E402

# Initialise COM on the main thread so pywinauto / UIA calls work.
pythoncom.CoInitialize()


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise(obj: Any) -> Any:  # noqa: ANN401
    """Recursively convert a POM method return value to a JSON-safe structure.

    Args:
        obj: Any Python value returned by a POM method.

    Returns:
        A JSON-serialisable representation of *obj*.
    """
    if hasattr(obj, "element_info"):
        d: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            d["runtime_id"] = list(obj.element_info.runtime_id)
        with contextlib.suppress(Exception):
            d["title"] = obj.window_text()
        with contextlib.suppress(Exception):
            d["class_name"] = str(obj.element_info.class_name)
        with contextlib.suppress(Exception):
            d["control_type"] = str(obj.element_info.control_type)
        return d
    if isinstance(obj, list):
        return [_serialise(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _handle_pom_list_widgets(_payload: dict[str, Any]) -> list[str]:
    """Return the names of all POM widget properties on the app class.

    Args:
        _payload: Request payload (unused).

    Returns:
        Sorted list of widget name strings.
    """
    names: list[str] = []
    for name in dir(AppClass):
        if name.startswith("_"):
            continue
        member = getattr(AppClass, name, None)
        if not isinstance(member, property):
            continue
        try:
            if member.fget is None:
                continue
            hints = get_type_hints(member.fget)
            ret = hints.get("return")
            if isinstance(ret, type) and issubclass(ret, BaseModel):
                names.append(name)
        except (NameError, AttributeError, TypeError):
            pass
    return sorted(names)


def _handle_pom_list_methods(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return public methods of a named widget.

    Args:
        payload: Must include ``widget`` key.

    Returns:
        List of dicts with ``name``, ``signature``, and ``doc``.

    Raises:
        RuntimeError: If the widget name is unknown.
    """
    widget_name: str = payload["widget"]
    app_inst = AppClass.get_active_instance()
    widget_obj = getattr(app_inst, widget_name, None)
    if widget_obj is None:
        raise RuntimeError(
            f"Unknown widget '{widget_name}'. Use pom_list_widgets() to see valid names."
        )
    results: list[dict[str, str]] = []
    for name in dir(widget_obj):
        if name.startswith("_"):
            continue
        fn = getattr(type(widget_obj), name, None)
        if fn is None or not callable(fn):
            continue
        try:
            sig = str(inspect.signature(fn))
        except (ValueError, TypeError):
            sig = "(...)"
        doc = (inspect.getdoc(fn) or "").split("\n")[0]
        results.append({"name": name, "signature": sig, "doc": doc})
    return results


def _handle_pom_call(payload: dict[str, Any]) -> Any:  # noqa: ANN401
    """Execute a POM method and return its serialised result.

    Args:
        payload: Must include ``widget``, ``method``, and optional ``kwargs``.

    Returns:
        Serialised return value of the POM method.

    Raises:
        RuntimeError: If the widget or method is invalid.
    """
    widget_name: str = payload["widget"]
    method_name: str = payload["method"]
    kwargs: dict[str, Any] = payload.get("kwargs") or {}

    app_inst = AppClass.get_active_instance()
    widget_obj = getattr(app_inst, widget_name, None)
    if widget_obj is None:
        raise RuntimeError(
            f"Unknown widget '{widget_name}'. Use pom_list_widgets() to see valid names."
        )
    method_fn = getattr(widget_obj, method_name, None)
    if method_fn is None or not callable(method_fn):
        raise RuntimeError(f"Unknown method '{method_name}' on widget '{widget_name}'.")
    return _serialise(method_fn(**kwargs))


def _handle_pom_chain(payload: dict[str, Any]) -> Any:  # noqa: ANN401
    """Walk a chain of attribute accesses and method calls.

    Args:
        payload: Must include ``steps`` key.

    Returns:
        Serialised return value of the final step.
    """
    steps: list[dict[str, Any]] = payload["steps"]
    current: Any = AppClass.get_active_instance()

    for i, step in enumerate(steps):
        name: str = step["step"]
        has_kwargs = "kwargs" in step
        kwargs: dict[str, Any] = step.get("kwargs") or {}

        attr = getattr(current, name, _MISSING)
        if attr is _MISSING:
            raise RuntimeError(f"Step {i}: {type(current).__name__!r} has no attribute {name!r}")
        if has_kwargs:
            if not callable(attr):
                raise RuntimeError(
                    f"Step {i}: {name!r} on {type(current).__name__!r} is not callable"
                )
            current = attr(**kwargs)
        else:
            current = attr

    return _serialise(current)


_MISSING = object()

# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_HANDLERS = {
    "pom_list_widgets": _handle_pom_list_widgets,
    "pom_list_methods": _handle_pom_list_methods,
    "pom_call": _handle_pom_call,
    "pom_chain": _handle_pom_chain,
}


def _dispatch(payload: dict[str, Any]) -> Any:  # noqa: ANN401
    """Route a request to the appropriate handler.

    Args:
        payload: Parsed JSON request.

    Returns:
        Handler's return value.

    Raises:
        RuntimeError: If the action is unknown.
    """
    action: str = payload.get("action", "")
    handler = _HANDLERS.get(action)
    if handler is None:
        raise RuntimeError(f"Unknown action '{action}'. Valid: {sorted(_HANDLERS)}")
    return handler(payload)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    """Read JSON requests from stdin, write responses to stdout."""
    proto_out = sys.stdout
    sys.stdout = sys.stderr

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            result = _dispatch(payload)
            print(json.dumps({"ok": True, "result": result}), file=proto_out, flush=True)
        except Exception:  # noqa: BLE001 — IPC boundary; must serialize any error
            print(
                json.dumps({"ok": False, "error": traceback.format_exc()}),
                file=proto_out,
                flush=True,
            )


if __name__ == "__main__":
    main()
