"""Native macOS accessibility and PID-addressed keys for the VS Code wake (#2183)."""
from __future__ import annotations

import ctypes as c
import re
import time


class Unavailable(Exception):
    pass


def _api():
    lib = c.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    for name, result, args in (
        ("AXIsProcessTrusted", c.c_bool, []),
        ("AXUIElementCreateApplication", c.c_void_p, [c.c_int]),
        ("AXUIElementCreateSystemWide", c.c_void_p, []),
        ("AXUIElementGetTypeID", c.c_ulong, []),
        ("AXUIElementSetMessagingTimeout", c.c_int, [c.c_void_p, c.c_float]),
        ("AXUIElementCopyAttributeValue", c.c_int, [c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]),
        ("AXUIElementSetAttributeValue", c.c_int, [c.c_void_p, c.c_void_p, c.c_void_p]),
        ("AXUIElementPerformAction", c.c_int, [c.c_void_p, c.c_void_p]),
        ("CFGetTypeID", c.c_ulong, [c.c_void_p]),
        ("CFEqual", c.c_bool, [c.c_void_p, c.c_void_p]),
        ("CFStringGetTypeID", c.c_ulong, []),
        ("CFArrayGetTypeID", c.c_ulong, []),
        ("CFBooleanGetTypeID", c.c_ulong, []),
        ("CFBooleanGetValue", c.c_bool, [c.c_void_p]),
        ("CFStringCreateWithCString", c.c_void_p, [c.c_void_p, c.c_char_p, c.c_uint32]),
        ("CFStringGetLength", c.c_long, [c.c_void_p]),
        ("CFStringGetMaximumSizeForEncoding", c.c_long, [c.c_long, c.c_uint32]),
        ("CFStringGetCString", c.c_bool, [c.c_void_p, c.c_void_p, c.c_long, c.c_uint32]),
        ("CFArrayGetCount", c.c_long, [c.c_void_p]),
        ("CFArrayGetValueAtIndex", c.c_void_p, [c.c_void_p, c.c_long]),
        ("CFRelease", None, [c.c_void_p]),
        ("CGEventSourceGetKeyboardType", c.c_uint32, [c.c_void_p]),
        ("CGEventCreateKeyboardEvent", c.c_void_p, [c.c_void_p, c.c_uint16, c.c_bool]),
        ("CGEventSetFlags", None, [c.c_void_p, c.c_uint64]),
        ("CGEventPostToPid", None, [c.c_int, c.c_void_p]),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    return lib


def preflight(lib=None):
    lib = lib or _api()
    # AXIsProcessTrusted has no prompting option. No Apple event is sent to System Events.
    if not lib.AXIsProcessTrusted():
        raise Unavailable("Accessibility is not granted to the signed 2mw2lt agent; enable it "
                          "in System Settings > Privacy & Security > Accessibility")
    return lib


_UTF8 = 0x08000100


class App:
    def __init__(self, pid: int):
        self.lib = preflight()
        self.held = []
        self.root = self.lib.AXUIElementCreateApplication(pid)
        if not self.root:
            raise Unavailable("the running app has no accessibility element")
        self.held.append(self.root)
        system = self.lib.AXUIElementCreateSystemWide()
        if not system:
            self.close()
            raise Unavailable("the caller's accessibility timeout could not be bounded")
        try:
            # A root-only timeout does not cover descendants or equal AX references.
            if self.lib.AXUIElementSetMessagingTimeout(system, 2.0):
                self.close()
                raise Unavailable("the caller's accessibility timeout could not be bounded")
        finally:
            self.lib.CFRelease(system)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        for ref in reversed(self.held):
            self.lib.CFRelease(ref)
        self.held.clear()

    def string(self, value: str):
        ref = self.lib.CFStringCreateWithCString(None, value.encode(), _UTF8)
        if not ref:
            raise Unavailable("an accessibility attribute could not be named")
        self.held.append(ref)
        return ref

    def get(self, element, name: str):
        value = c.c_void_p()
        code = self.lib.AXUIElementCopyAttributeValue(element, self.string(name), c.byref(value))
        if code:
            return None
        ref = value.value
        self.held.append(ref)
        kind = self.lib.CFGetTypeID(ref)
        if kind == self.lib.CFStringGetTypeID():
            size = self.lib.CFStringGetMaximumSizeForEncoding(self.lib.CFStringGetLength(ref), _UTF8) + 1
            buf = c.create_string_buffer(size)
            return buf.value.decode() if self.lib.CFStringGetCString(ref, buf, size, _UTF8) else None
        if kind == self.lib.CFArrayGetTypeID():
            return [self.lib.CFArrayGetValueAtIndex(ref, i) for i in range(self.lib.CFArrayGetCount(ref))]
        if kind == self.lib.CFBooleanGetTypeID():
            return bool(self.lib.CFBooleanGetValue(ref))
        if kind == self.lib.AXUIElementGetTypeID():
            return ref
        return None

    def same(self, left, right):
        return bool(left and right and self.lib.CFEqual(left, right))

    def action(self, element, name: str):
        code = self.lib.AXUIElementPerformAction(element, self.string(name))
        if code:
            raise Unavailable(f"{name} failed with accessibility status {code}")

    def flag(self, element, name: str, value: bool):
        ref = c.c_void_p.in_dll(self.lib, "kCFBooleanTrue" if value else "kCFBooleanFalse").value
        code = self.lib.AXUIElementSetAttributeValue(element, self.string(name), ref)
        if code:
            raise Unavailable(f"{name} failed with accessibility status {code}")

    def elements(self, window, max_webviews=None, prune_history=False, deadline=None):
        pending = [(window, 0)]
        visited = set()
        while pending:
            if deadline is not None and time.monotonic() >= deadline:
                raise Unavailable("the target tab's accessibility lookup did not settle in time")
            element, webviews = pending.pop()
            if element in visited:
                continue
            visited.add(element)
            if len(visited) > 5000:
                raise Unavailable("the existing window's accessibility tree exceeded the wake's bound")
            role = self.get(element, "AXRole")
            if prune_history and role == "AXGroup" and self.get(element, "AXDescription") == "Claude Code conversation":
                continue
            if role == "AXWebArea":
                webviews += 1
                if max_webviews is not None and webviews > max_webviews:
                    continue
            yield element, role
            pending.extend((child, webviews) for child in (self.get(element, "AXChildren") or []))

    def tabs(self, window, deadline=None):
        for element, role in self.elements(window, max_webviews=1, deadline=deadline):
            if role in ("AXRadioButton", "AXTab", "AXButton"):
                yield element


def _match(app, folder, title, deadline=None):
    matches = []
    for window in app.get(app.root, "AXWindows") or []:
        name = app.get(window, "AXTitle") or ""
        if not folder or not re.search(rf"(?<![\w.-]){re.escape(folder)}(?![\w.-])", name):
            continue
        for tab in app.tabs(window, deadline=deadline):
            if title in (app.get(tab, "AXTitle"), app.get(tab, "AXDescription")):
                matches.append((window, tab))
    if len(matches) != 1:
        raise Unavailable("the running app has no unique existing workspace/tab match")
    return matches[0]


def _owns_input(app, element, window, title, deadline=None):
    if not app.same(app.get(element, "AXWindow"), window):
        return False
    seen, panel = [element], False
    for _ in range(64):
        if deadline is not None and time.monotonic() >= deadline:
            raise Unavailable("the target tab's accessibility lookup did not settle in time")
        element = app.get(element, "AXParent")
        if not element or any(app.same(element, previous) for previous in seen):
            return False
        seen.append(element)
        role = app.get(element, "AXRole")
        if role == "AXWindow":
            return panel and app.same(element, window)
        if role == "AXGroup" and not panel:
            name = app.get(element, "AXDescription")
            if name:
                if name != title:
                    return False
                panel = True
    return False


def _selected(app, tab):
    if not app.get(app.root, "AXFrontmost") or not app.get(tab, "AXSelected"):
        raise Unavailable("the target tab is not selected in the frontmost app")


def _ready(app, window, tab, title, deadline=None):
    _selected(app, tab)
    element = app.get(app.root, "AXFocusedUIElement")
    if (not element or app.get(element, "AXRole") != "AXTextArea"
            or app.get(element, "AXDescription") != "Message input"
            or not app.get(element, "AXFocused")
            or not _owns_input(app, element, window, title, deadline)):
        raise Unavailable("the target tab's message input is not uniquely focused")
    text = app.get(element, "AXValue")
    if not isinstance(text, str):
        raise Unavailable("the target tab's message input text cannot be read")
    if deadline is not None and time.monotonic() >= deadline:
        raise Unavailable("the target tab's accessibility lookup did not settle in time")
    return "" if text == "⌘ Esc to focus or unfocus Claude" else text


# How long a wake waits for Chromium to build the accessibility tree after the wake itself
# requested it (`AXManualAccessibility`): the tree builds lazily, and the first wake of an app
# measured longer than the two seconds this used to allow — the first live #2183 trial refused
# with no windows while the tree was still building, and the one minutes later typed. Bounded,
# still well inside the keyboard wait a wake holds, and paid only once per app.
COLD_TREE_WAIT_S = 15.0


def focus(pid: int, folder: str, title: str):
    with App(pid) as app:
        enabling = not app.get(app.root, "AXManualAccessibility")
        if enabling:
            app.flag(app.root, "AXManualAccessibility", True)
        deadline = time.monotonic() + (COLD_TREE_WAIT_S if enabling else 0.0)
        while True:
            try:
                window, tab = _match(app, folder, title)
                break
            except Unavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        if app.get(window, "AXMinimized"):
            app.flag(window, "AXMinimized", False)
        app.flag(app.root, "AXFrontmost", True)
        app.action(window, "AXRaise")
        app.action(tab, "AXPress")
        deadline = time.monotonic() + 2.0
        acquired = False
        while True:
            try:
                current_window, current_tab = _match(app, folder, title, deadline)
                if not app.same(current_window, window) or not app.same(current_tab, tab):
                    raise Unavailable("the target window or tab changed while acquiring focus")
                _selected(app, tab)
                if acquired:
                    _ready(app, window, tab, title, deadline)
                    return
                inputs = [element for element, role in app.elements(window, prune_history=True, deadline=deadline)
                          if role == "AXTextArea" and app.get(element, "AXDescription") == "Message input"
                          and _owns_input(app, element, window, title, deadline)]
                if len(inputs) != 1:
                    raise Unavailable("the target tab has no unique message input to focus")
            except Unavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
                continue
            _selected(app, tab)
            if time.monotonic() >= deadline:
                raise Unavailable("the target tab's accessibility lookup did not settle in time")
            # Do not retry an unsupported setter or repeatedly steal focus while settling.
            app.flag(inputs[0], "AXFocused", True)
            acquired = True


def ready(pid: int, folder: str, title: str):
    with App(pid) as app:
        deadline = time.monotonic() + 2.0
        window, tab = _match(app, folder, title, deadline)
        return _ready(app, window, tab, title, deadline)


def idle(pid: int, folder: str, title: str):
    """Read the native control composer, not the queue-another-message input of a busy tab."""
    with App(pid) as app:
        deadline = time.monotonic() + 2.0
        window, tab = _match(app, folder, title, deadline)
        _ready(app, window, tab, title, deadline)
        sends = []
        for element, role in app.elements(window, prune_history=True, deadline=deadline):
            if role in ("AXDialog", "AXMenu", "AXList"):
                raise Unavailable("the target window shows a dialog, menu or list, not an idle composer")
            if role != "AXButton":
                continue
            name = app.get(element, "AXDescription")
            if name not in ("Stop", "Send message") or not _owns_input(app, element, window, title, deadline):
                continue
            if name == "Stop":
                raise Unavailable("the target tab shows Stop, not an idle composer")
            sends.append(element)
        if len(sends) != 1:
            raise Unavailable("the target tab has no unique idle Send message control")
        text = _ready(app, window, tab, title, deadline)
        if text and not app.get(sends[0], "AXEnabled"):
            raise Unavailable("the target tab's Send message control is disabled")
        return text


def _keyboard_api():
    lib = c.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    for name, result, args in (
        ("TISCopyCurrentKeyboardLayoutInputSource", c.c_void_p, []),
        ("TISCopyCurrentASCIICapableKeyboardLayoutInputSource", c.c_void_p, []),
        ("TISGetInputSourceProperty", c.c_void_p, [c.c_void_p, c.c_void_p]),
        ("CFDataGetLength", c.c_long, [c.c_void_p]),
        ("CFDataGetBytePtr", c.c_void_p, [c.c_void_p]),
        ("CFRelease", None, [c.c_void_p]),
        ("UCKeyTranslate", c.c_int32, [c.c_void_p, c.c_uint16, c.c_uint16, c.c_uint32,
             c.c_uint32, c.c_uint32, c.POINTER(c.c_uint32), c.c_ulong,
             c.POINTER(c.c_ulong), c.POINTER(c.c_uint16)]),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    return lib, c.c_void_p.in_dll(lib, "kTISPropertyUnicodeKeyLayoutData").value


def command_key(letter: str) -> int:
    lib, prop = _keyboard_api()
    keyboard = _api().CGEventSourceGetKeyboardType(None)
    for current in (lib.TISCopyCurrentKeyboardLayoutInputSource,
                    lib.TISCopyCurrentASCIICapableKeyboardLayoutInputSource):
        source = current()
        if not source:
            continue
        try:
            data = lib.TISGetInputSourceProperty(source, prop)
            if not data or not lib.CFDataGetLength(data):
                continue
            layout = lib.CFDataGetBytePtr(data)
            matches = []
            for code in range(128):
                dead, count = c.c_uint32(), c.c_ulong()
                chars = (c.c_uint16 * 4)()
                status = lib.UCKeyTranslate(layout, code, 0, 1, keyboard, 1, c.byref(dead),
                                           len(chars), c.byref(count), chars)
                if status == 0 and count.value == 1 and chars[0] == ord(letter):
                    matches.append(code)
            if len(matches) != 1:
                raise Unavailable("the active keyboard layout has no unique Command shortcut")
            return matches[0]
        finally:
            lib.CFRelease(source)
    raise Unavailable("the active keyboard layout could not be read")


def keys(pid: int, code: int, flags: int = 0, before_post=None):
    lib = preflight()
    for down in (True, False):
        event = lib.CGEventCreateKeyboardEvent(None, code, down)
        if not event:
            raise Unavailable("a native key event could not be created")
        try:
            lib.CGEventSetFlags(event, flags)
            if down and before_post is not None:
                before_post()
            lib.CGEventPostToPid(pid, event)
        finally:
            lib.CFRelease(event)
