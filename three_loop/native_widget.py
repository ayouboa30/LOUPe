"""Pure Win32 floating desktop mascot with real per-pixel transparency.

WebView2 (used for the main window) turned out not to support genuine
transparency reliably on this system: three different tricks tried against
a pywebview window (layered color-key, DWM frame extension, WinForms
TransparencyKey) all left a solid rectangle behind the sprite, because a
hosted browser control renders through its own composition surface that
ignores those tricks. This module sidesteps the problem entirely by not
using a browser control at all: it paints the sprite with Win32's
``UpdateLayeredWindow`` API directly onto a 32-bit ARGB bitmap, which is
the standard, reliable mechanism native "desktop pet" apps use.

The artwork itself is the same hand-drawn PNG frame set used by the web
chat's avatar (see web/assets/mascot_*.png and MASCOT_ASPECT in
web/app.js) - real alpha transparency baked into the files, no procedural
approximation.

Hovering the mascot fades in two action icons to its right: a mic (one-shot
voice question) and a magnifying glass. The glass lets the user drag-select
a region of the screen; that region is read with OCR, a web search is fired
from what it found, and the model explains the capture using both. Both
actions run against the local engine on a background thread and finish with
a Windows tray toast, so neither blocks nor steals focus from what the user
is doing.

An earlier version auto-captured the whole screen and had the mascot sweep
across it while waiting - the sweep didn't correspond to what was actually
being captured (always the whole screen, regardless of the sweep's path),
which read as an animation bug rather than a useful cue. Letting the user
drag the exact region themselves removes that mismatch: the drag itself is
the feedback, so the mascot just raises its glass and holds it while the
capture is read, searched and explained.
"""

from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw

from . import notify
from .assistant_actions import (
    build_screen_reading_prompt,
    build_screen_search_prompt,
    listen_and_transcribe,
    ocr_image,
    run_prompt_in_background,
    search_from_screen_text,
)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSEMOVE = 0x0200
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
TPM_RIGHTBUTTON = 0x0002
ID_CLOSE_MASCOT = 1001
BI_RGB = 0
DIB_RGB_COLORS = 0

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

HANDLE = wintypes.HANDLE

# Every Win32 call below crosses a handle or pointer-sized value; without
# explicit argtypes ctypes assumes plain 32-bit ints for anything it
# wasn't told about, which raises "int too long to convert" the moment a
# handle happens to need the high bits on 64-bit Windows.
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetDC.restype = HANDLE
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, HANDLE]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCapture.restype = HANDLE
user32.SetCapture.argtypes = [wintypes.HWND]
user32.SetWindowPos.argtypes = [wintypes.HWND, HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.LoadCursorW.restype = HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.CreatePopupMenu.restype = HANDLE
user32.AppendMenuW.argtypes = [HANDLE, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [
    HANDLE, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [HANDLE]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
gdi32.CreateCompatibleDC.restype = HANDLE
gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.SelectObject.restype = HANDLE
gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.DeleteDC.argtypes = [HANDLE]
gdi32.CreateDIBSection.restype = HANDLE
gdi32.CreateDIBSection.argtypes = [
    HANDLE, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p), HANDLE, wintypes.DWORD,
]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class _WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# ---------------------------------------------------------------- artwork
#
# Frames 5-9 of the hand-drawn set: 5 is idle (no magnifying glass), 9 is
# fully raised. Loaded once as premultiplied BGRA byte buffers so redraws
# are just a memcpy, not a re-decode.

_SCALE = 0.48  # shrink the ~261x275 source art further: tighter CPU/GPU perf
_ICON_SIZE = 32
_ICON_GAP = 8


def _assets_dir() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / "web" / "assets"


def _load_frames() -> list[tuple[bytes, int, int]]:
    """Decode mascot_05..09.png into premultiplied BGRA buffers, once."""

    frames = []
    for i in range(5, 10):
        path = _assets_dir() / f"mascot_{i:02d}.png"
        img = Image.open(path).convert("RGBA")
        w, h = int(img.width * _SCALE), int(img.height * _SCALE)
        img = img.resize((w, h), Image.LANCZOS)
        r, g, b, a = img.split()
        rgb = np.dstack([np.array(b), np.array(g), np.array(r)]).astype(np.float64)
        alpha = np.array(a).astype(np.float64)
        premultiplied = (rgb * (alpha[..., None] / 255.0)).clip(0, 255).astype(np.uint8)
        bgra = np.dstack([premultiplied, alpha.astype(np.uint8)])
        frames.append((bgra.tobytes(), w, h))
    return frames


def _build_icon(kind: str, size: int = _ICON_SIZE) -> np.ndarray:
    """Draw a small round mic/magnifying-glass glyph, straight (non-premultiplied) RGBA."""

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([1, 1, size - 2, size - 2], fill=(28, 19, 48, 235), outline=(150, 110, 255, 255), width=2)
    ink = (241, 238, 251, 255)
    if kind == "mic":
        draw.rounded_rectangle(
            [size * 0.40, size * 0.16, size * 0.60, size * 0.54], radius=size * 0.10, fill=ink
        )
        draw.arc([size * 0.26, size * 0.30, size * 0.74, size * 0.66], start=0, end=180, fill=ink, width=3)
        draw.line([size / 2, size * 0.66, size / 2, size * 0.80], fill=ink, width=3)
    else:
        draw.ellipse([size * 0.20, size * 0.20, size * 0.58, size * 0.58], outline=ink, width=4)
        draw.line([size * 0.56, size * 0.56, size * 0.80, size * 0.80], fill=ink, width=4)
    return np.array(img)


class NativeWidget:
    """A tiny always-on-top, draggable, transparent desktop companion."""

    def __init__(
        self,
        on_click: Callable[[], None],
        *,
        x: int = 60,
        y: int = 60,
        port: int | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._on_click = on_click
        self._on_close = on_close
        self._port = port
        self._x = x
        self._y = y
        self._hwnd: wintypes.HWND | None = None
        self._dragging = False
        self._hovering = False
        self._busy = False
        self._drag_start_cursor = (0, 0)
        self._drag_start_window = (0, 0)
        self._moved = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._start_time = time.time()
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._frames = _load_frames()
        self._glass_progress = 0.0  # 0 = idle frame, len(frames)-1 = fully raised
        self._icon_opacity = 0.0
        self._flip_horizontally = False  # rotate toward mouse
        # True while an OCR/search/mic request is in flight: keeps the
        # magnifying glass raised so the mascot visibly reads as busy. The
        # region-select drag itself is the main feedback for what is being
        # captured; this only covers the OCR+search+model time after that.
        self._busy_glass_raised = False

        sprite_w = max(f[1] for f in self._frames)
        sprite_h = max(f[2] for f in self._frames)
        self._sprite_w = sprite_w
        self._sprite_h = sprite_h
        self._canvas_w = sprite_w + _ICON_GAP + _ICON_SIZE
        self._canvas_h = max(sprite_h, 2 * _ICON_SIZE + _ICON_GAP)

        icon_x = sprite_w + _ICON_GAP
        icons_h = 2 * _ICON_SIZE + _ICON_GAP
        icons_y0 = (self._canvas_h - icons_h) // 2
        self._icon_rects = {
            "mic": (icon_x, icons_y0, icon_x + _ICON_SIZE, icons_y0 + _ICON_SIZE),
            "ocr": (icon_x, icons_y0 + _ICON_SIZE + _ICON_GAP, icon_x + _ICON_SIZE, icons_y0 + icons_h),
        }
        self._icon_images = {"mic": _build_icon("mic"), "ocr": _build_icon("ocr")}

    def start(self) -> None:
        self._thread.start()

    # -- Win32 plumbing -------------------------------------------------

    def _run(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "ThreeLoopNativeWidget"

        wndclass = _WNDCLASS()
        wndclass.style = 0
        wndclass.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wndclass.hInstance = hinstance
        idc_arrow = ctypes.cast(ctypes.c_void_p(32512), wintypes.LPCWSTR)  # MAKEINTRESOURCE(IDC_ARROW)
        wndclass.hCursor = user32.LoadCursorW(None, idc_arrow)
        wndclass.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wndclass))

        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            class_name,
            "3loop-mascot",
            WS_POPUP,
            self._x,
            self._y,
            self._canvas_w + 4,
            self._canvas_h + 4,
            None,
            None,
            hinstance,
            None,
        )
        self._hwnd = hwnd
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetTimer(hwnd, 1, 33, None)
        self._redraw()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _update_hover_state(self) -> None:
        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        rect = wintypes.RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        self._hovering = rect.left <= cursor.x <= rect.right and rect.top <= cursor.y <= rect.bottom

        # Orient toward mouse: flip if mouse is to the left of sprite center
        center_x = rect.left + self._sprite_w // 2
        self._flip_horizontally = cursor.x < center_x

    def _redraw(self) -> None:
        if self._hwnd is None:
            return
        self._update_hover_state()

        # Ease the magnifying glass (and the action icons' fade-in) over
        # ~0.5s instead of snapping, mirroring the web avatar's CSS
        # steps() hover animation. While a request is in flight the glass
        # stays fully raised regardless of where the pointer is.
        last_frame = len(self._frames) - 1
        target = last_frame if (self._hovering or self._busy_glass_raised) else 0.0
        step = last_frame / 15.0
        if self._glass_progress < target:
            self._glass_progress = min(target, self._glass_progress + step)
        elif self._glass_progress > target:
            self._glass_progress = max(target, self._glass_progress - step)
        frame_index = round(self._glass_progress)
        self._icon_opacity = self._glass_progress / last_frame

        elapsed = time.time() - self._start_time
        bob = round(math.sin(elapsed * 2.4) * 3.5)

        pixels, width, height = self._frames[frame_index]
        if self._flip_horizontally:
            pixels = self._flip_pixels_horizontal(pixels, width, height)
        self._blit(pixels, width, height, bob)

    @staticmethod
    def _flip_pixels_horizontal(pixels: bytes, width: int, height: int) -> bytes:
        """Mirror pixel data left-to-right."""
        buf = bytearray(pixels)
        row_bytes = width * 4
        for row in range(height):
            row_start = row * row_bytes
            row_end = row_start + row_bytes
            row_data = buf[row_start:row_end]
            flipped = bytearray()
            for col in range(width - 1, -1, -1):
                flipped.extend(row_data[col * 4 : col * 4 + 4])
            buf[row_start:row_end] = flipped
        return bytes(buf)

    def _blit(self, pixels: bytes, width: int, height: int, bob_offset: int) -> None:
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        # Canvas is padded to the largest frame so a smaller idle frame
        # doesn't jump position; the sprite stays anchored bottom-left of
        # its own sub-area, with the icon column to its right.
        canvas_w, canvas_h = self._canvas_w, self._canvas_h
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = canvas_w
        header.biHeight = -canvas_h  # negative = top-down DIB
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        bits_ptr = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
        ctypes.memset(bits_ptr, 0, canvas_w * canvas_h * 4)
        buf = (ctypes.c_ubyte * (canvas_w * canvas_h * 4)).from_address(bits_ptr.value)

        ox = (self._sprite_w - width) // 2
        oy = (canvas_h - height) - bob_offset
        row_bytes = width * 4
        for row in range(height):
            dst_y = oy + row
            if not (0 <= dst_y < canvas_h):
                continue
            dst_start = (dst_y * canvas_w + ox) * 4
            src_start = row * row_bytes
            buf[dst_start : dst_start + row_bytes] = pixels[src_start : src_start + row_bytes]

        if self._icon_opacity > 0.01:
            for name, (x0, y0, _x1, _y1) in self._icon_rects.items():
                self._composite_icon(buf, canvas_w, canvas_h, self._icon_images[name], x0, y0, self._icon_opacity)

        old_bitmap = gdi32.SelectObject(mem_dc, bitmap)

        size = wintypes.SIZE(canvas_w, canvas_h)
        src_point = wintypes.POINT(0, 0)
        win_rect = wintypes.RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(win_rect))
        dst_point = wintypes.POINT(win_rect.left, win_rect.top)

        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
            wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow(
            self._hwnd, None, ctypes.byref(dst_point), ctypes.byref(size),
            mem_dc, ctypes.byref(src_point), 0, ctypes.byref(blend), ULW_ALPHA,
        )

        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    @staticmethod
    def _composite_icon(
        buf: ctypes.Array, canvas_w: int, canvas_h: int, icon_rgba: np.ndarray, x: int, y: int, opacity: float
    ) -> None:
        h, w = icon_rgba.shape[:2]
        r = icon_rgba[..., 0].astype(np.float64)
        g = icon_rgba[..., 1].astype(np.float64)
        b = icon_rgba[..., 2].astype(np.float64)
        a = icon_rgba[..., 3].astype(np.float64) * opacity
        scale = a / 255.0
        bgra = np.dstack(
            [
                (b * scale).clip(0, 255).astype(np.uint8),
                (g * scale).clip(0, 255).astype(np.uint8),
                (r * scale).clip(0, 255).astype(np.uint8),
                a.clip(0, 255).astype(np.uint8),
            ]
        )
        data = bgra.tobytes()
        row_bytes = w * 4
        for row in range(h):
            dst_y = y + row
            if not (0 <= dst_y < canvas_h):
                continue
            dst_start = (dst_y * canvas_w + x) * 4
            src_start = row * row_bytes
            buf[dst_start : dst_start + row_bytes] = data[src_start : src_start + row_bytes]

    @staticmethod
    def _point_in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
        x0, y0, x1, y1 = rect
        return x0 <= x <= x1 and y0 <= y <= y1

    # -- background actions ----------------------------------------------

    def _notify(self, message: str) -> None:
        if self._hwnd is not None:
            notify.show_toast(self._hwnd, "3loop", message[:250])

    def _finish_prompt(self, answer: str, success: bool) -> None:
        self._notify(answer if success else f"Erreur: {answer}")
        self._busy = False

    def _start_mic_flow(self) -> None:
        if self._busy or self._port is None:
            return
        self._busy = True

        def worker() -> None:
            try:
                text = listen_and_transcribe()
            except Exception as exc:
                self._notify(f"Erreur micro: {exc}")
                self._busy = False
                return
            if not text:
                self._busy = False
                return
            run_prompt_in_background(text, port=self._port, on_done=self._finish_prompt)

        threading.Thread(target=worker, daemon=True).start()

    def _start_ocr_flow(self) -> None:
        """Let the user drag-select a region, read it, search the web from
        it, and have the model explain it using both.

        No text dialog is opened. An earlier version had one, which is where
        the flow died in practice: the dialog could land behind other
        windows, and cancelling it left the mascot busy with nothing to show
        for it. The drag-select gesture (mouse down, drag, release) is itself
        the input - unambiguous, and it never leaves an orphaned window.
        """

        if self._busy or self._port is None:
            return
        self._busy = True

        def worker() -> None:
            from .screen_capture import select_region_and_capture

            try:
                image = select_region_and_capture()
            except Exception as exc:
                self._notify(f"Erreur de capture: {exc}")
                self._busy = False
                return
            if image is None:
                self._busy = False  # user cancelled (Esc / right-click / no drag)
                return

            self._busy_glass_raised = True
            try:
                self._run_ocr_search_and_explain(image)
            except Exception as exc:
                self._busy_glass_raised = False
                self._notify(f"Erreur OCR: {exc}")
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _run_ocr_search_and_explain(self, image: Image.Image) -> None:
        ocr_text = ocr_image(image)

        try:
            import asyncio

            results = asyncio.run(search_from_screen_text(ocr_text))
        except Exception:
            results = []  # a broken search must not sink an otherwise-fine OCR

        prompt = build_screen_search_prompt(ocr_text, results) if results else None
        if prompt is None:
            prompt = build_screen_reading_prompt(ocr_text)
        if prompt is None:
            self._busy_glass_raised = False
            self._notify("Aucun texte lisible n'a ete trouve dans la selection.")
            self._busy = False
            return
        run_prompt_in_background(prompt, port=self._port, on_done=self._finish_ocr)

    def _finish_ocr(self, answer: str, success: bool) -> None:
        self._busy_glass_raised = False
        self._finish_prompt(answer, success)

    def _show_context_menu(self, hwnd: wintypes.HWND) -> None:
        """A real right-click menu with one item: "Fermer la mascotte".

        Destroying the window on any right-click (the previous behaviour)
        risked closing it by accident with no way back short of relaunching
        the app. A menu makes it a deliberate choice.
        """

        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, ID_CLOSE_MASCOT, "Fermer la mascotte")
        # Recommended Win32 pattern for a popup menu owned by a window that
        # is not the foreground window: without this the menu can fail to
        # dismiss itself when the user clicks elsewhere.
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, cursor.x, cursor.y, 0, hwnd, None)
        user32.DestroyMenu(menu)

    def _wndproc(self, hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TIMER:
            self._redraw()
            return 0
        if msg == WM_LBUTTONDOWN:
            self._dragging = True
            self._moved = False
            cursor = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            self._drag_start_cursor = (cursor.x, cursor.y)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._drag_start_window = (rect.left, rect.top)
            user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE and self._dragging:
            cursor = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor))
            dx = cursor.x - self._drag_start_cursor[0]
            dy = cursor.y - self._drag_start_cursor[1]
            if abs(dx) > 3 or abs(dy) > 3:
                self._moved = True
            new_x = self._drag_start_window[0] + dx
            new_y = self._drag_start_window[1] + dy
            user32.SetWindowPos(hwnd, None, new_x, new_y, 0, 0, 0x0001 | 0x0004)  # NOSIZE|NOZORDER
            return 0
        if msg == WM_LBUTTONUP:
            self._dragging = False
            user32.ReleaseCapture()
            if not self._moved:
                cursor = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(cursor))
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                local_x = cursor.x - rect.left
                local_y = cursor.y - rect.top
                if self._hovering and self._point_in_rect(local_x, local_y, self._icon_rects["mic"]):
                    self._start_mic_flow()
                elif self._hovering and self._point_in_rect(local_x, local_y, self._icon_rects["ocr"]):
                    self._start_ocr_flow()
                else:
                    try:
                        self._on_click()
                    except Exception:
                        pass
            return 0
        if msg == WM_RBUTTONUP:
            self._show_context_menu(hwnd)
            return 0
        if msg == WM_COMMAND:
            if (wparam & 0xFFFF) == ID_CLOSE_MASCOT:
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            # Right-click ("fermer la mascotte") is the only way to destroy
            # this window, and with the main app window now hidden instead
            # of closed on the X button, it is also the only remaining way
            # to quit the whole application - so the caller gets a callback
            # here to tear the rest of the process down.
            if self._on_close is not None:
                try:
                    self._on_close()
                except Exception:
                    pass
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
