"""Interactive drag-select screen capture.

Replaces the earlier full-screen auto-capture, which the user found
confusing paired with the mascot's screen-sweep animation: the sweep
suggested the mascot was scanning something specific, but the actual
capture was always the whole screen regardless of what was on it. Letting
the user drag exactly the region they mean removes that mismatch, and the
drag itself is the only feedback needed - no separate "busy" animation has
to explain what is being captured.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PIL import Image, ImageGrab

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002
WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSEMOVE = 0x0200
WM_KEYDOWN = 0x0100
WM_PAINT = 0x000F
VK_ESCAPE = 0x1B
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
IDC_CROSS = 32515
R2_NOT = 6

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HANDLE = wintypes.HANDLE

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetDC.restype = HANDLE
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, HANDLE]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
user32.LoadCursorW.restype = HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.SetCursor.argtypes = [HANDLE]
user32.DestroyWindow.argtypes = [wintypes.HWND]
gdi32.SetROP2.argtypes = [HANDLE, ctypes.c_int]
gdi32.Rectangle.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.GetStockObject.restype = HANDLE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
gdi32.SelectObject.restype = HANDLE
gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.CreatePen.restype = HANDLE
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
gdi32.DeleteObject.argtypes = [HANDLE]

NULL_BRUSH = 5  # GetStockObject stock object id

WM_QUIT = 0x0012
PM_REMOVE = 0x0001

user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
]


def _drain_quit_messages() -> int:
    """Remove any pending WM_QUIT from the current thread's message queue.

    Returns how many were discarded, which is what makes the behaviour
    testable without a real capture.

    A WM_QUIT is thread-wide, not window-wide: it survives the window that
    caused it and is delivered to whichever message loop runs next on that
    thread. Since this module's overlay and the companion's question card are
    two successive modal loops on the *same* worker thread, one stray quit
    silently aborts the second one.
    """

    msg = wintypes.MSG()
    discarded = 0
    while user32.PeekMessageW(ctypes.byref(msg), None, WM_QUIT, WM_QUIT, PM_REMOVE):
        discarded += 1
    return discarded


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _RegionSelector:
    """One-shot fullscreen overlay for a single drag-select gesture."""

    def __init__(self) -> None:
        self._hwnd: wintypes.HWND | None = None
        self._dragging = False
        self._start = (0, 0)
        self._current = (0, 0)
        self._last_drawn: tuple[int, int, int, int] | None = None
        self._result: tuple[int, int, int, int] | None = None
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._pen = gdi32.CreatePen(0, 2, 0x00E1CBAB)  # solid, 2px, accent-ish BGR

    def run(self) -> tuple[int, int, int, int] | None:
        """Block until the user finishes (or cancels) a drag; return the rect."""

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "ThreeLoopRegionSelector"
        wndclass = _WNDCLASS()
        wndclass.style = 0
        wndclass.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wndclass.hInstance = hinstance
        wndclass.hCursor = None  # cursor is set explicitly to a crosshair below
        wndclass.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wndclass))

        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            class_name, "3loop-select", WS_POPUP,
            x, y, w, h, None, None, hinstance, None,
        )
        self._hwnd = hwnd
        # Uniform per-window alpha (not per-pixel UpdateLayeredWindow): a
        # simple dim overlay is all this needs, and it lets ordinary GDI
        # drawing (the marquee rectangle) work directly on the window's DC.
        user32.SetLayeredWindowAttributes(hwnd, 0, 90, LWA_ALPHA)
        cross = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_CROSS), wintypes.LPCWSTR))
        user32.SetCursor(cross)
        user32.ShowWindow(hwnd, 5)
        user32.SetForegroundWindow(hwnd)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.DestroyWindow(hwnd)
        gdi32.DeleteObject(self._pen)
        # Belt and braces: leave the calling thread's queue clean whatever
        # happened above. This selector is opened from a worker thread that
        # goes on to run another modal loop (the question card), and a single
        # leftover WM_QUIT is enough to make that loop exit before showing
        # anything. Draining is safe because this thread owns no other window
        # by now - the overlay was the only one and it is already destroyed.
        _drain_quit_messages()
        if self._result is None:
            return None
        # Translate back to virtual-screen (multi-monitor) coordinates.
        left, top, right, bottom = self._result
        return (left + x, top + y, right + x, bottom + y)

    def _draw_rect(self, rect: tuple[int, int, int, int]) -> None:
        dc = user32.GetDC(self._hwnd)
        gdi32.SetROP2(dc, R2_NOT)  # XOR draw: drawing the same rect again erases it
        old_pen = gdi32.SelectObject(dc, self._pen)
        old_brush = gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_BRUSH))
        gdi32.Rectangle(dc, *rect)
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)
        user32.ReleaseDC(self._hwnd, dc)

    def _normalized(self) -> tuple[int, int, int, int]:
        x0, y0 = self._start
        x1, y1 = self._current
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def _wndproc(self, hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_LBUTTONDOWN:
            cursor = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._start = (cursor.x - rect.left, cursor.y - rect.top)
            self._current = self._start
            self._dragging = True
            return 0
        if msg == WM_MOUSEMOVE and self._dragging:
            cursor = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._current = (cursor.x - rect.left, cursor.y - rect.top)
            if self._last_drawn is not None:
                self._draw_rect(self._last_drawn)  # erase previous frame
            new_rect = self._normalized()
            self._draw_rect(new_rect)
            self._last_drawn = new_rect
            return 0
        if msg == WM_LBUTTONUP and self._dragging:
            self._dragging = False
            rect = self._normalized()
            # A near-zero drag is almost certainly an accidental click, not
            # an intentional 1px selection - treat it as a cancel.
            if rect[2] - rect[0] > 4 and rect[3] - rect[1] > 4:
                self._result = rect
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_RBUTTONUP or (msg == WM_KEYDOWN and wparam == VK_ESCAPE):
            self._result = None
            user32.PostQuitMessage(0)
            return 0
        if msg == WM_DESTROY:
            # Deliberately NOT PostQuitMessage here. ``run`` only calls
            # DestroyWindow *after* its own message loop has already ended, so
            # a quit posted from this branch lands in the thread's queue with
            # nobody left to consume it. That stray WM_QUIT then killed the
            # very next modal loop opened on the same thread: after a capture,
            # the OCR worker calls ``prompt_window.ask_question``, whose
            # GetMessageW returned 0 immediately, so the question card never
            # appeared and the flow silently behaved as if the user had
            # cancelled - "I take the screenshot and nothing happens".
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def select_region_and_capture() -> Image.Image | None:
    """Let the user drag a rectangle on screen; return that region as an image.

    Blocks until the drag completes. Returns ``None`` if the user cancels
    (Esc, right-click, or a drag too small to be intentional) - no capture
    happens in that case.
    """

    rect = _RegionSelector().run()
    if rect is None:
        return None
    return ImageGrab.grab(bbox=rect)
