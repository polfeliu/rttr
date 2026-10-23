from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import math
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from comtypes import COMError
from pywinauto import Application, findwindows
from pywinauto.application import WindowSpecification
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.uia_element_info import UIAElementInfo

if TYPE_CHECKING:
    from collections.abc import Callable

    from pywinauto.base_wrapper import BaseWrapper

logger = logging.getLogger(__name__)

# Exceptions commonly raised during pywinauto / UIA element access.
# Diagnostic and resilient code paths catch these instead of bare ``except Exception``.
UIA_ERRORS: tuple[type[Exception], ...] = (
    COMError,
    ElementNotFoundError,
    AttributeError,
    RuntimeError,
    OSError,
)


# ------------------------------------------------------------------
class ControlType(StrEnum):
    """Enum for UI Control Types matching UIA string definitions."""

    WINDOW = "Window"
    PANE = "Pane"
    GROUP = "Group"
    BUTTON = "Button"
    EDIT = "Edit"
    TEXT = "Text"
    MENU = "Menu"
    MENU_BAR = "MenuBar"
    MENU_ITEM = "MenuItem"
    TAB = "Tab"
    TAB_ITEM = "TabItem"
    SPINNER = "Spinner"
    LIST = "List"
    LIST_ITEM = "ListItem"
    COMBO_BOX = "ComboBox"
    CHECK_BOX = "CheckBox"
    RADIO_BUTTON = "RadioButton"
    DOCUMENT = "Document"
    IMAGE = "Image"
    TREE = "Tree"
    TREE_ITEM = "TreeItem"
    TABLE = "Table"
    TITLE_BAR = "TitleBar"
    TOOL_BAR = "ToolBar"
    TOOL_TIP = "ToolTip"
    SCROLL_BAR = "ScrollBar"
    SLIDER = "Slider"
    PROGRESS_BAR = "ProgressBar"
    STATUS_BAR = "StatusBar"
    SEPARATOR = "Separator"
    HYPERLINK = "Hyperlink"
    THUMB = "Thumb"
    HEADER = "Header"
    HEADER_ITEM = "HeaderItem"
    WINDOW_CAPTION = "WindowCaption"
    CUSTOM = "Custom"
    DATA_ITEM = "DataItem"
    STATIC = "Static"

    @classmethod
    def from_string(cls, value: str | None) -> ControlType:
        """Convert a string value to a ControlType enum.

        Returns:
            The parsed ControlType member, or CUSTOM for unknown values.
        """
        if not value:
            return cls.CUSTOM
        try:
            return cls(value)
        except ValueError:
            return cls.CUSTOM


@dataclass
class UINode:
    """Dataclass representing a node in the UI automation tree."""

    control_type: ControlType
    class_name: str
    title: str
    auto_id: str
    is_visible: bool
    is_enabled: bool
    center_x: int = 0
    center_y: int = 0
    children: list[UINode] = field(default_factory=list)
    element: Any | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert the UINode to a dictionary representation.

        Returns:
            A dictionary containing the state and relationships of the node.
        """
        return {
            "control_type": self.control_type.value,
            "class_name": self.class_name,
            "title": self.title,
            "auto_id": self.auto_id,
            "is_visible": self.is_visible,
            "is_enabled": self.is_enabled,
            "children": [c.to_dict() for c in self.children],
        }


class BaseModel:
    """Base class for Page Object Models.

    Wraps a pywinauto WindowSpecification or UIAWrapper.
    """

    # Win32 class-name substrings for Qt popup-shadow windows.  These windows
    # can survive after their parent popup closes during automated testing and
    # cause ``Desktop(backend="uia").windows()`` to hang for 20+ seconds.
    _QT_SHADOW_MARKERS: tuple[str, ...] = ("PopupDropShadow", "SaveBits", "SysShadow")

    @staticmethod
    def _purge_qt_shadow_windows(pid: int | None = None) -> int:
        """Close orphaned Qt popup-shadow windows that block UIA enumeration.

        Uses fast Win32 API (``findwindows`` with ``backend="win32"``)
        to enumerate top-level windows and sends ``WM_CLOSE`` to any
        whose class name matches a known Qt shadow pattern.

        Args:
            pid: If given, only close shadow windows belonging to this
                process.  Otherwise close all matching shadows.

        Returns:
            Number of shadow windows closed.
        """
        wm_close = 0x0010
        closed = 0
        kwargs: dict[str, object] = {"backend": "win32"}
        if pid is not None:
            kwargs["process"] = pid
        for elem in findwindows.find_elements(**kwargs):
            cn = elem.class_name
            if any(marker in cn for marker in BaseModel._QT_SHADOW_MARKERS):
                ctypes.windll.user32.SendMessageW(elem.handle, wm_close, 0, 0)
                closed += 1
        if closed:
            logger.debug("Purged %d Qt shadow window(s) for pid=%s", closed, pid)
        return closed

    @staticmethod
    def _find_window_handles(
        pid: int | None = None,
        *,
        class_name: str | None = None,
        title: str | None = None,
        title_re: str | None = None,
    ) -> list[int]:
        """Find top-level window handles via Win32 enumeration (shadow-immune).

        Uses the fast Win32 backend (``EnumWindows``) to enumerate
        top-level windows.  Shadow windows (matching
        :attr:`_QT_SHADOW_MARKERS`) are skipped entirely, avoiding the
        20+ second UIA COM hang they cause.

        For ``class_name`` matching, UIA is queried per-window because
        Qt Win32 class names (e.g. ``Qt5152QWindowIcon``) differ from
        UIA automation class names (e.g. ``QMenu``).  Shadow windows
        are excluded before UIA is touched, so this remains fast.

        Args:
            pid: Process ID to filter by.
            class_name: UIA class name to match (e.g. ``"QMenu"``).
            title: Exact window title to match (Win32 ``GetWindowText``).
            title_re: Regex pattern for window title matching.

        Returns:
            List of matching Win32 window handles.
        """
        kwargs: dict[str, object] = {"backend": "win32"}
        if pid is not None:
            kwargs["process"] = pid
        handles: list[int] = []
        for elem in findwindows.find_elements(**kwargs):
            win32_cn = elem.class_name
            is_shadow = any(marker in win32_cn for marker in BaseModel._QT_SHADOW_MARKERS)
            # When searching by UIA class_name, only skip pure shadow
            # windows (e.g. "SysShadow").  Qt popup windows like
            # "Qt5152QWindowPopupDropShadowSaveBits" contain shadow
            # marker substrings but are real popups that must be probed.
            if is_shadow:
                if class_name is None:
                    # General enumeration: skip shadows to avoid UIA COM hang
                    continue
                if win32_cn in BaseModel._QT_SHADOW_MARKERS:
                    # Pure shadow window (exact match): always skip
                    continue
                # Otherwise: win32 name merely *contains* a marker
                # (e.g. Qt popup).  Fall through to the UIA probe below.
            if title is not None and elem.name != title:
                continue
            if title_re is not None and not re.search(title_re, elem.name):
                continue
            if class_name is not None:
                try:
                    uia_class = UIAElementInfo(elem.handle).class_name
                    if uia_class != class_name:
                        continue
                except Exception:  # noqa: BLE001 ΓÇö UIA probe can fail for destroyed windows
                    continue
            handles.append(elem.handle)
        return handles

    def __init__(self, element: BaseWrapper | WindowSpecification, name: str | None = None) -> None:
        """Initialize the base model.

        Args:
            element: The underlying PyWinAuto element this model wraps.
            name: A descriptive name for the model instance. Defaults to class name.
        """
        self.element = element
        self.name = name or self.__class__.__name__
        self.backend = "uia"

    def exists(self, timeout: int = 2) -> bool:
        """Check if the underlying element exists and is visible.

        Args:
            timeout: Time in seconds to wait for the element to exist.

        Returns:
            True if the element exists and is visible, False otherwise.
        """
        with contextlib.suppress(*UIA_ERRORS):
            return bool(self.element.exists(timeout=timeout) and self.element.is_visible())
        return False

    def wait_until_closed(self, timeout: int = 5) -> None:
        """Wait until the widget/dialog is no longer visible.

        Tries pywinauto's native ``wait_not("visible")`` first, which
        performs fresh element lookups each poll cycle.  Falls back to a
        polling loop using ``exists()`` for dialogs connected by handle
        where ``wait_not`` may time out on stale handles.

        No retry decorator is applied here: retrying a ``TimeoutError``
        triples the effective wait, which is counterproductive for a close-wait.

        Args:
            timeout: Maximum time to wait in seconds.

        Raises:
            TimeoutError: If the element is still visible after the timeout.
        """
        try:
            self.element.wait_not("visible", timeout=timeout)
            return
        except UIA_ERRORS:
            pass
        # Fallback: poll exists() for handle-based specs where wait_not
        # may not detect the window as gone.
        end = time.time() + timeout
        while time.time() < end:
            try:
                if not self.element.exists(timeout=0):
                    return
                if not self.element.is_visible():
                    return
            except UIA_ERRORS:
                return
            time.sleep(0.3)
        raise TimeoutError(f"Widget {self.name} did not close within {timeout}s.")

    # ------------------------------------------------------------------
    # Typed wait helpers
    # ------------------------------------------------------------------

    def wait_visible(
        self,
        element: BaseWrapper | WindowSpecification,
        timeout: int = 5,
    ) -> None:
        """Wait until *element* exists and is visible.

        Args:
            element: The pywinauto element to wait on.
            timeout: Maximum time in seconds.
        """
        element.wait("exists visible", timeout=timeout)

    def wait_exists(
        self,
        element: BaseWrapper | WindowSpecification,
        timeout: int = 5,
    ) -> None:
        """Wait until *element* exists in the UI tree.

        Args:
            element: The pywinauto element to wait on.
            timeout: Maximum time in seconds.
        """
        element.wait("exists", timeout=timeout)

    def wait_enabled(
        self,
        element: BaseWrapper | WindowSpecification,
        timeout: int = 30,
    ) -> None:
        """Wait until *element* is enabled.

        Args:
            element: The pywinauto element to wait on.
            timeout: Maximum time in seconds.
        """
        element.wait("enabled", timeout=timeout)

    def wait_exists_enabled(
        self,
        element: BaseWrapper | WindowSpecification,
        timeout: int = 5,
    ) -> None:
        """Wait until *element* exists and is enabled.

        Args:
            element: The pywinauto element to wait on.
            timeout: Maximum time in seconds.
        """
        element.wait("exists enabled", timeout=timeout)

    def is_open(self) -> bool:
        """Check whether this widget's panel/tab is currently the active view.

        Subclasses must override this to inspect a reliable sentinel element
        that is only visible when their own panel is shown.

        Returns:
            True if the widget's panel is currently open/active.

        Raises:
            NotImplementedError: Always, unless the subclass overrides this.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement is_open() "
            "to report whether its panel is currently visible."
        )

    # ------------------------------------------------------------------
    # Assertion helpers
    # ------------------------------------------------------------------

    def _assert(self, condition: object, msg: str, *, target: object = None) -> None:
        """Log and assert a condition, including spatial data for the target element.

        When *target* is provided (a pywinauto element or BaseModel), its
        screen rect is used for the overlay highlight.  Otherwise falls back
        to ``self.element`` (the whole widget).

        Args:
            condition: Truthy value means pass, falsy means fail.
            msg: Descriptive assertion message.
            target: Optional specific UI element to highlight (more precise
                than the whole widget).

        Raises:
            AssertionError: When *condition* is falsy.
        """
        from rttr.assertion_overlays import show_assertion  # noqa: PLC0415

        status = "PASS" if condition else "FAIL"
        # Resolve the element to highlight — prefer explicit target
        highlight_element = target
        if highlight_element is not None and isinstance(highlight_element, BaseModel):
            highlight_element = highlight_element.element
        if highlight_element is None:
            highlight_element = self.element

        rect_str = ""
        rect_tuple: tuple[int, int, int, int] | None = None
        try:
            rect = highlight_element.rectangle()  # type: ignore[attr-defined]
            rect_tuple = (rect.left, rect.top, rect.right, rect.bottom)
            rect_str = (
                f" widget={self.name}"
                f" rect=({rect.left},{rect.top},{rect.right},{rect.bottom})"
                f" center=({(rect.left + rect.right) // 2},{(rect.top + rect.bottom) // 2})"
            )
        except Exception:  # noqa: BLE001
            rect_str = f" widget={self.name}"

        if condition:
            logger.info("[ASSERT] %s: %s%s", status, msg, rect_str)
        else:
            logger.error("[ASSERT] %s: %s%s", status, msg, rect_str)

        # Show real-time screen overlay (best-effort, non-blocking)
        show_assertion(passed=bool(condition), rect=rect_tuple)

        if not condition:
            raise AssertionError(msg)

    def assert_is_open(self, msg: str = "") -> None:
        """Assert this widget is currently open/visible."""
        self._assert(self.is_open(), msg or f"{self.name} should be open")

    def assert_is_closed(self, msg: str = "") -> None:
        """Assert this widget is currently closed/hidden."""
        self._assert(not self.is_open(), msg or f"{self.name} should be closed")

    def assert_exists(self, msg: str = "", timeout: int = 2) -> None:
        """Assert the underlying element exists and is visible."""
        self._assert(self.exists(timeout=timeout), msg or f"{self.name} should exist")

    def assert_not_exists(self, msg: str = "") -> None:
        """Assert the underlying element does not exist or is hidden."""
        self._assert(not self.exists(timeout=0), msg or f"{self.name} should not exist")

    # ------------------------------------------------------------------
    # Element-targeted assertions — highlight the specific UI element
    # ------------------------------------------------------------------

    def assert_element_visible(self, element: object, msg: str = "") -> None:
        """Assert a UI element is visible, highlighting it on screen.

        Args:
            element: A pywinauto element (WindowSpecification or BaseWrapper).
            msg: Optional assertion message.
        """
        self._assert(
            element is not None and element.is_visible(),  # type: ignore[attr-defined]
            msg or f"Element should be visible in {self.name}",
            target=element,
        )

    def assert_element_enabled(self, element: object, msg: str = "") -> None:
        """Assert a UI element is enabled, highlighting it on screen.

        Args:
            element: A pywinauto element (WindowSpecification or BaseWrapper).
            msg: Optional assertion message.
        """
        self._assert(
            element is not None and element.is_enabled(),  # type: ignore[attr-defined]
            msg or f"Element should be enabled in {self.name}",
            target=element,
        )

    def assert_element_exists(self, element: object, msg: str = "") -> None:
        """Assert a UI element is not None (was found), highlighting it.

        Args:
            element: The element returned by a POM lookup method.
            msg: Optional assertion message.
        """
        self._assert(
            element is not None,
            msg or f"Element should exist in {self.name}",
            target=element,
        )

    def dump_state(self) -> str:
        """Extract a clean, high-detail text representation of the UI state.

        Returns:
            A formatted string representing the widget's current UI state.
        """

        def _to_rid(raw: object) -> tuple[int, ...]:
            """Normalise a runtime_id that may be a bare int.

            Returns:
                A tuple of integers representing the runtime ID.
            """
            if isinstance(raw, int):
                return (raw,)
            return tuple(raw)  # type: ignore[arg-type]

        lines = [f"=== STATE DUMP: {self.name} ==="]
        try:
            root_rid: tuple[int, ...] = _to_rid(self.element.element_info.runtime_id)
            depth_map: dict[tuple[int, ...], int] = {root_rid: 0}
            for el in [self.element, *self.element.descendants()]:
                with contextlib.suppress(*UIA_ERRORS, TypeError):
                    info = el.element_info
                    rid = _to_rid(info.runtime_id)
                    parent_info = info.parent
                    depth = (
                        depth_map.get(_to_rid(parent_info.runtime_id), 0) + 1
                        if parent_info is not None
                        else 0
                    )
                    depth_map[rid] = depth
                    title = ""
                    with contextlib.suppress(*UIA_ERRORS):
                        title = el.window_text().replace("\n", "\\n")
                    c_type, c_name, auto_id = "?", "", ""
                    with contextlib.suppress(*UIA_ERRORS):
                        c_type = info.control_type
                        c_name = info.class_name
                        auto_id = info.auto_id
                    rect_str = "rect=(0,0,0,0)"
                    with contextlib.suppress(*UIA_ERRORS):
                        rect = el.rectangle()
                        rect_str = f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom})"
                    line = f"{'  ' * depth}- [{c_type}] title='{title}'"
                    if c_name:
                        line += f" class='{c_name}'"
                    if auto_id:
                        line += f" id='{auto_id}'"
                    line += f" {rect_str}"
                    lines.append(line)
        except UIA_ERRORS:
            pass
        return "\n".join(lines)

    def find_element(
        self,
        *,
        control_type: ControlType | str | None = None,
        title: str | None = None,
        title_re: str | None = None,
        auto_id: str | None = None,
        class_name: str | None = None,
        **kwargs: Any,  # noqa: ANN401 ΓÇö forwarded to pywinauto child_window()
    ) -> WindowSpecification:
        """Find a child element of this model.

        Args:
            control_type: The ControlType or string to filter by.
            title: The exact title text to filter by.
            title_re: A regex for the title text.
            auto_id: The automation ID to filter by.
            class_name: The window class name to filter by.
            **kwargs: Additional selector criteria passed to child_window.

        Returns:
            The found pywinauto child element.
        """
        if control_type is not None:
            kwargs["control_type"] = (
                control_type.value if isinstance(control_type, ControlType) else control_type
            )
        if title is not None:
            kwargs["title"] = title
        if title_re is not None:
            kwargs["title_re"] = title_re
        if auto_id is not None:
            kwargs["auto_id"] = auto_id
        if class_name is not None:
            kwargs["class_name"] = class_name

        # Log the lookup for observability: which parent and which selector
        try:
            parent_handle = getattr(self.element, "handle", None)
        except UIA_ERRORS:
            parent_handle = None
        selector = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        logger.info(
            "[FIND] %s.find_element parent_handle=%s selector={%s}",
            self.name,
            parent_handle,
            selector,
        )

        return self.element.child_window(**kwargs)

    def click(self, target_control: BaseWrapper | WindowSpecification | UINode) -> None:
        """Move the mouse smoothly to the element centre and click it.

        Args:
            target_control: The PyWinAuto control or UINode to click.

        Raises:
            RuntimeError: If the click fails or times out.
            ValueError: If the UINode has no attached element.
        """
        # Resolve UINode to pywinauto element
        if isinstance(target_control, UINode):
            if target_control.element is None:
                raise ValueError(
                    f"UINode '{target_control.title}' has no attached pywinauto element."
                )
            target_control = target_control.element

        try:
            # Only WindowSpecification needs to wait for resolution/visibility.
            # Resolved wrappers (BaseWrapper) are already found.
            if isinstance(target_control, WindowSpecification):
                self.wait_visible(target_control, timeout=2)
            rect = target_control.rectangle()
            center_x = (rect.left + rect.right) // 2
            center_y = (rect.top + rect.bottom) // 2
            # Emit concise action log with identifying details
            try:
                ctrl_title = target_control.window_text()
            except UIA_ERRORS:
                ctrl_title = ""
            try:
                ctrl_class = getattr(target_control, "class_name", lambda: "")()
            except UIA_ERRORS:
                ctrl_class = ""
            logger.info(
                "[CLICK] %s -> title=%r class=%r rect=(%d,%d,%d,%d) center=(%d,%d)",
                self.name,
                ctrl_title,
                ctrl_class,
                rect.left,
                rect.top,
                rect.right,
                rect.bottom,
                center_x,
                center_y,
            )

            self._move_mouse_smoothly(center_x, center_y)
            target_control.click_input()
            # UIA settlement delay to avoid "event was unable to invoke subscribers" errors
            # during subsequent state dumps or interactions.
            time.sleep(0.5)
        except UIA_ERRORS as e:
            raise RuntimeError(f"Failed to click {target_control}: {e}") from e

    @staticmethod
    def _move_mouse_smoothly(
        target_x: int,
        target_y: int,
    ) -> None:
        """Animate mouse movement for realism and visual tracking.

        Args:
            target_x: X coordinate of the target.
            target_y: Y coordinate of the target.
        """
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        start_x, start_y = pt.x, pt.y

        # Calculate distance
        dist = math.hypot(target_x - start_x, target_y - start_y)

        # Near-instant dynamic duration (e.g. 1 second per 64000 pixels, min 0.01s, max 0.05s)
        duration = max(0.01, min(0.05, dist / 64000.0))
        steps = int(duration * 250)  # Maximum frequency for "instant" feel

        steps = max(steps, 1)
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        start_x, start_y = pt.x, pt.y

        for i in range(1, steps + 1):
            t = i / float(steps)
            dist_x = target_x - start_x
            dist_y = target_y - start_y
            curr_x = int(start_x + (dist_x * t))
            curr_y = int(start_y + (dist_y * t))
            ctypes.windll.user32.SetCursorPos(curr_x, curr_y)
            time.sleep(duration / steps)

    def get_active_dialog(self) -> BaseWrapper | WindowSpecification:
        """Return the WindowSpecification for the foreground dialog.

        Useful for returning a secondary dialog when an action spawns one.
        Waits up to 3 seconds for the foreground window to change from the main window.
        """
        try:
            main_hwnd = self.element.handle
            pid = self.element.process_id()
            # Wait for foreground window to change from main_hwnd
            for _ in range(30):  # 3 seconds total (100ms steps)
                fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
                if fg_hwnd and fg_hwnd != main_hwnd:
                    # Verify it belongs to this process via Win32 (fast check)
                    handles = self._find_window_handles(pid)
                    if fg_hwnd in handles:
                        dialog_app = Application(backend="uia").connect(handle=fg_hwnd)
                        return dialog_app.window(handle=fg_hwnd)
                time.sleep(0.1)
        except UIA_ERRORS:
            pass
        return self.element

    def get_top_level_dialog(self, title: str, timeout: int = 3) -> WindowSpecification:
        """Locate a top-level dialog belonging to this process by exact title.

        Uses Win32 backend for enumeration (immune to Qt shadow windows).

        Args:
            title: Exact window title to find.
            timeout: Seconds to wait before giving up.

        Returns:
            The connected WindowSpecification for the dialog.

        Raises:
            RuntimeError: If the dialog cannot be found within the timeout.
        """
        pid: int | None = None
        with contextlib.suppress(*UIA_ERRORS):
            pid = self.element.process_id()

        end_time = time.time() + float(timeout)
        while time.time() < end_time:
            handles = self._find_window_handles(pid, title=title)
            for h in handles:
                try:
                    dlg_app = Application(backend="uia").connect(handle=h)
                    return dlg_app.window(handle=h)
                except UIA_ERRORS:
                    continue
            time.sleep(0.1)
        raise RuntimeError(f"Top-level dialog titled '{title}' not found within {timeout}s")

    def get_top_level_dialog_by_class(
        self, class_name: str, timeout: int = 3
    ) -> WindowSpecification:
        """Locate a top-level dialog belonging to this process by class name.

        Unlike ``get_top_level_dialog`` which matches on window title, this
        method matches on the window's Qt class name, which is more stable
        for popups that share generic titles like ``"Widget"``.

        Args:
            class_name: Exact Qt class name to find (e.g.
                ``"ControlConfigurationView"``).
            timeout: Seconds to wait before giving up.

        Returns:
            The connected WindowSpecification for the dialog.

        Raises:
            RuntimeError: If the dialog cannot be found within the timeout.
        """
        pid: int | None = None
        with contextlib.suppress(*UIA_ERRORS):
            pid = self.element.process_id()

        end_time = time.time() + float(timeout)
        while time.time() < end_time:
            handles = self._find_window_handles(pid, class_name=class_name)
            for h in handles:
                try:
                    dlg_app = Application(backend="uia").connect(handle=h)
                    return dlg_app.window(handle=h)
                except UIA_ERRORS:
                    continue
            time.sleep(0.1)
        raise RuntimeError(
            f"Top-level dialog with class '{class_name}' not found within {timeout}s"
        )

    def find_top_level_window(
        self,
        class_name: str,
        timeout: float = 3,
    ) -> WindowSpecification | None:
        """Find a top-level OS window belonging to this process by class name.

        This is the preferred entry point for POMs that need to locate a
        popup or dialog that appears as a top-level window rather than a
        child of the main application window.  Individual POMs must **not**
        call ``Desktop(backend="uia")`` directly ΓÇö use this helper instead.

        Args:
            class_name: The Qt/Win32 class name to search for
                (e.g. ``"DriveOverView"``, ``"AboutView"``).
            timeout: Maximum seconds to wait.  Use ``0`` for a single
                non-blocking check.

        Returns:
            A connected ``WindowSpecification`` if found, or ``None``.
        """
        pid: int | None = None
        with contextlib.suppress(*UIA_ERRORS):
            pid = self.element.process_id()

        end_time = time.time() + float(timeout)
        while True:
            handles = self._find_window_handles(pid, class_name=class_name)
            for h in handles:
                try:
                    dlg_app = Application(backend="uia").connect(handle=h)
                    return dlg_app.window(handle=h)
                except UIA_ERRORS:
                    continue
            if time.time() >= end_time:
                return None
            time.sleep(0.1)

    def has_top_level_window(self, class_name: str) -> bool:
        """Check whether a top-level window with *class_name* exists for this process.

        Uses Win32 enumeration (shadow-immune, sub-millisecond).

        Args:
            class_name: The window class name to look for.

        Returns:
            ``True`` if such a window exists, ``False`` otherwise.
        """
        pid: int | None = None
        with contextlib.suppress(*UIA_ERRORS):
            pid = self.element.process_id()
        return len(self._find_window_handles(pid, class_name=class_name)) > 0

    def wait_for[T](
        self, predicate: Callable[[], T], timeout: float = 5.0, interval: float = 0.1
    ) -> T:
        """Wait for a predicate to return a truthy result.

        Args:
            predicate: A callable that returns a truthy value when the condition is met.
            timeout: Maximum seconds to wait.
            interval: Poll interval in seconds.

        Returns:
            The truthy value returned by predicate.

        Raises:
            TimeoutError: If the predicate did not return a truthy value within the timeout.
        """
        end_time = time.time() + float(timeout)
        last_exc = None
        while time.time() < end_time:
            try:
                result = predicate()
                if result:
                    return result
            except Exception as e:  # noqa: BLE001 ΓÇö predicate can throw any caller-defined exception
                last_exc = e
            time.sleep(interval)
        if last_exc:
            raise TimeoutError(f"wait_for timed out after {timeout}s; last error: {last_exc}")
        raise TimeoutError(f"wait_for timed out after {timeout}s")

    @staticmethod
    def _safe_class_name(element: BaseWrapper | WindowSpecification) -> str:
        """Read the class name of a UIA element with resilient fallback.

        Tries ``element_info.class_name`` first (fast, cached), then
        the method ``class_name()`` for resolved wrappers.

        Args:
            element: A pywinauto element (wrapper or specification).

        Returns:
            The class name string, or ``""`` on failure.
        """
        try:
            return str(element.element_info.class_name)
        except UIA_ERRORS:
            pass
        try:
            return str(element.class_name())
        except UIA_ERRORS:
            return ""

    @classmethod
    def _build_tree_from_descendants(
        cls, element: BaseWrapper | WindowSpecification
    ) -> UINode | None:
        """Build a UINode tree using a single descendants() call.

        Uses element runtime IDs for parent-child linking, replacing the old
        recursive children() approach. Captures the full tree without depth limits.

        Returns:
            The root UINode of the tree, or None on failure.
        """
        try:
            all_elements: list[BaseWrapper | WindowSpecification] = [
                element,
                *element.descendants(),
            ]
        except UIA_ERRORS:
            return None

        node_by_rid: dict[tuple[int, ...], UINode] = {}
        root_node: UINode | None = None

        for el in all_elements:
            try:
                control_type, auto_id = "", ""
                cx, cy = 0, 0
                with contextlib.suppress(AttributeError):
                    control_type = el.element_info.control_type or ""
                with contextlib.suppress(AttributeError):
                    auto_id = el.element_info.auto_id
                with contextlib.suppress(*UIA_ERRORS):
                    rect = el.rectangle()
                    cx = (rect.left + rect.right) // 2
                    cy = (rect.top + rect.bottom) // 2
                node = UINode(
                    control_type=ControlType.from_string(control_type),
                    class_name=el.class_name(),
                    title=el.window_text(),
                    auto_id=auto_id,
                    is_visible=el.is_visible(),
                    is_enabled=el.is_enabled(),
                    center_x=cx,
                    center_y=cy,
                    element=el,
                )
                rid = tuple(el.element_info.runtime_id)
                node_by_rid[rid] = node
                parent_info = el.element_info.parent
                if parent_info is None:
                    root_node = node
                else:
                    parent_rid = tuple(parent_info.runtime_id)
                    parent_node = node_by_rid.get(parent_rid)
                    if parent_node is not None:
                        parent_node.children.append(node)
                    elif root_node is None:
                        root_node = node
            except UIA_ERRORS:
                continue

        return root_node

    @classmethod
    def capture_tree_from_win(cls, win: BaseWrapper | WindowSpecification) -> UINode | None:
        """Build the UI tree from an already-resolved pywinauto window wrapper.

        Returns:
            The root UINode of the tree, or None if it fails.
        """
        return cls._build_tree_from_descendants(win)

    @classmethod
    def capture_tree(
        cls, title_re: str, timeout: int = 5, app_instance: Application | None = None
    ) -> UINode | None:
        """Capture the UI tree from a window matching the title regex.

        Returns:
            The root UINode of the tree, or None if it fails.
        """
        try:
            if app_instance is not None:
                app = app_instance
            else:
                app = Application(backend="uia").connect(title_re=title_re, timeout=timeout)

            main_win = app.window(title_re=title_re)

            # Check if a dialog/popup from the same process is currently in the foreground.
            # If so, prefer capturing from that window so the agent explores the dialog first.
            try:
                fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
                pid = main_win.process_id()
                handles = cls._find_window_handles(pid)
                if fg_hwnd in handles and fg_hwnd != main_win.handle:
                    fg_app = Application(backend="uia").connect(handle=fg_hwnd)
                    fg_win = fg_app.window(handle=fg_hwnd)
                    fg_title = fg_win.window_text()
                    if fg_title and fg_title != main_win.window_text():
                        logger.info("[Dialog detected] Capturing from: '%s'", fg_title)
                        return cls._build_tree_from_descendants(fg_win)
            except UIA_ERRORS:
                pass  # Fall through to main window capture

            return cls._build_tree_from_descendants(main_win)
        except UIA_ERRORS as e:
            logger.error("Error connecting or traversing: %s", e, exc_info=True)
            return None

    @classmethod
    def dump_all(cls, main_window: BaseWrapper | WindowSpecification) -> list[dict[str, Any]]:
        """Capture the UI tree of the main window and all top-level windows for this process.

        Args:
            main_window: The main application window element.

        Returns:
            A list of window state dicts (JSON-serializable) representing each
            top-level window belonging to this process. Each entry contains
            metadata and the `UINode` tree converted to dicts.
        """
        results: list[dict[str, Any]] = []

        # Try main window first
        try:
            try:
                main_tree = cls.capture_tree_from_win(main_window)
            except UIA_ERRORS:
                main_tree = None
            main_info = {
                "handle": getattr(main_window, "handle", None),
                "title": getattr(main_window, "window_text", lambda: "")()
                if hasattr(main_window, "window_text")
                else "",
                "class_name": getattr(main_window, "class_name", lambda: "")()
                if hasattr(main_window, "class_name")
                else "",
                "tree": main_tree.to_dict() if main_tree is not None else None,
            }
            results.append(main_info)
        except UIA_ERRORS:
            pass

        # Other top-level windows in the same process (via Win32 for speed)
        try:
            main_pid = main_window.process_id()
            main_handle = getattr(main_window, "handle", None)
            for h in cls._find_window_handles(main_pid):
                if h == main_handle:
                    continue
                try:
                    w_app = Application(backend="uia").connect(handle=h)
                    w = w_app.window(handle=h)
                    tree = cls.capture_tree_from_win(w)
                    results.append({
                        "handle": h,
                        "title": w.window_text(),
                        "class_name": w.class_name(),
                        "tree": tree.to_dict() if tree is not None else None,
                    })
                except UIA_ERRORS:
                    continue
        except UIA_ERRORS:
            pass

        return results

    @classmethod
    def dump_all_str(cls, main_window: BaseWrapper | WindowSpecification) -> str:
        """Capture the UI tree and return it as a human-readable JSON string.

        Args:
            main_window: The main application window element.

        Returns:
            A JSON-formatted string representing all top-level windows.
        """
        data = cls.dump_all(main_window)
        return json.dumps(data, indent=2, default=str)
