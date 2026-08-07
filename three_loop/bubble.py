"""Speech bubble for the desktop companion: a click-through-free, focus-free card.

The mascot needs somewhere to put a sentence and a few links - "here is what
I found while you were working" - without interrupting what the user is doing.
Windows' tray balloons (see notify.py) can carry a line of text but nothing
clickable and nothing styled, so this is a small layered window of its own.

Three properties matter and each one drives a specific flag below:

* **It must never steal focus.** Typing into another window has to keep
  working while the bubble is up, so the window is ``WS_EX_NOACTIVATE`` and is
  only ever shown with ``SW_SHOWNOACTIVATE`` / ``SWP_NOACTIVATE``.
* **It must not appear in Alt-Tab or the taskbar**, hence
  ``WS_EX_TOOLWINDOW``, the same treatment the mascot window gets.
* **It must be shaped and soft-edged**, so it is drawn as a 32-bit ARGB
  bitmap pushed through ``UpdateLayeredWindow`` - the same technique
  native_widget.py uses for the sprite. That is what allows the rounded
  corners, the drop shadow and the pointer tail to blend with whatever is
  behind them instead of sitting on a grey rectangle.

Windows are thread-affine: messages are delivered to the thread that created
the window. The bubble is therefore created and shown on the mascot's UI
thread, and background workers ask for it indirectly (see the
``WM_BUBBLE_REQUEST`` marshalling in native_widget.py) rather than calling
``show`` from their own thread, which would leave the window with no message
pump and make it unclickable.
"""

from __future__ import annotations

import ctypes
import os
import threading
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
HWND_TOPMOST = -1
WM_DESTROY = 0x0002
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202
WM_TIMER = 0x0113
WM_MOUSELEAVE = 0x02A3
BI_RGB = 0
DIB_RGB_COLORS = 0
IDC_HAND = 32649
IDC_ARROW = 32512

_AUTO_HIDE_TIMER = 1

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HANDLE = wintypes.HANDLE

# Same reasoning as native_widget.py: without explicit argtypes ctypes assumes
# 32-bit ints and any handle needing the high bits raises "int too long to
# convert" on 64-bit Windows.
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.GetDC.restype = HANDLE
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, HANDLE]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.LoadCursorW.restype = HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.SetCursor.argtypes = [HANDLE]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
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
        ("hCursor", HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# ------------------------------------------------------------------ styling
#
# Light card to match the app's own surfaces: near-white fill, hairline
# border, minimal shadow. The only saturated colour is the accent used for
# links and the header dot.

CARD_WIDTH = 336
SHADOW_MARGIN = 12
PADDING = 14
RADIUS = 14
TAIL = 9

COLOR_CARD = (255, 255, 255, 250)
COLOR_BORDER = (206, 206, 214, 255)
COLOR_TITLE = (29, 29, 31, 255)
COLOR_TEXT = (72, 72, 78, 255)
COLOR_MUTED = (138, 138, 148, 255)
COLOR_ACCENT = (109, 63, 212, 255)
COLOR_ACCENT_SOFT = (238, 232, 255, 255)
COLOR_HOVER = (245, 242, 255, 255)
COLOR_SEPARATOR = (233, 233, 238, 255)
COLOR_SHADOW = (26, 16, 48, 46)


def load_ui_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """Load a UI font, degrading to PIL's bitmap font rather than failing.

    A frozen build still runs on Windows, so Segoe UI is effectively always
    present; the fallbacks exist so a stripped container or a future non-
    Windows port renders *something* instead of raising.
    """

    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = ("segoeuib.ttf", "tahomabd.ttf") if bold else ("segoeui.ttf", "tahoma.ttf")
    for name in candidates:
        try:
            return ImageFont.truetype(str(fonts_dir / name), size)
        except OSError:
            continue
    return ImageFont.load_default()


@dataclass
class BubbleLink:
    """One clickable row: a headline, its source, and where it goes."""

    title: str
    url: str
    domain: str = ""


@dataclass
class BubbleContent:
    title: str = "3loop"
    lines: tuple[str, ...] = ()
    links: tuple[BubbleLink, ...] = ()
    #: Seconds before the bubble hides itself; ``None`` keeps it until the
    #: user acts on it (used for "click me to see the result").
    timeout_s: float | None = 45.0
    #: Called when the card itself - not a link - is clicked.
    on_click: object | None = None
    footer: str = ""
    _rows: list[tuple[int, int, int, int, str]] = field(default_factory=list)


class Bubble:
    """A single reusable speech-bubble window owned by one UI thread."""

    def __init__(self) -> None:
        self._hwnd: wintypes.HWND | None = None
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._content: BubbleContent | None = None
        self._hover: int = -1
        self._image: Image.Image | None = None
        self._visible = False
        self._class_registered = False
        self._hand = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_HAND), wintypes.LPCWSTR))
        self._arrow = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_ARROW), wintypes.LPCWSTR))

    # -- public API -----------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    def show(self, anchor: tuple[int, int, int, int], content: BubbleContent) -> None:
        """Render ``content`` and place it beside ``anchor`` (the mascot rect).

        Must be called on the thread that owns this bubble's window.
        """

        self._content = content
        self._hover = -1
        image = self._render(content)
        self._image = image
        hwnd = self._ensure_window()

        x, y = self._placement(anchor, image.size)
        user32.SetWindowPos(hwnd, HANDLE(HWND_TOPMOST), x, y, image.width, image.height, SWP_NOACTIVATE)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self._push_bitmap(image, (x, y))
        self._visible = True

        user32.KillTimer(hwnd, _AUTO_HIDE_TIMER)
        if content.timeout_s:
            user32.SetTimer(hwnd, _AUTO_HIDE_TIMER, int(content.timeout_s * 1000), None)

    def hide(self) -> None:
        if self._hwnd is None:
            return
        user32.KillTimer(self._hwnd, _AUTO_HIDE_TIMER)
        user32.ShowWindow(self._hwnd, SW_HIDE)
        self._visible = False

    def destroy(self) -> None:
        if self._hwnd is not None:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
            self._visible = False

    # -- window plumbing ------------------------------------------------

    def _ensure_window(self) -> wintypes.HWND:
        if self._hwnd is not None:
            return self._hwnd
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "ThreeLoopBubble"
        if not self._class_registered:
            wndclass = _WNDCLASS()
            wndclass.style = 0
            wndclass.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            wndclass.hInstance = hinstance
            wndclass.hCursor = self._arrow
            wndclass.lpszClassName = class_name
            user32.RegisterClassW(ctypes.byref(wndclass))
            self._class_registered = True
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            class_name,
            "3loop-bubble",
            WS_POPUP,
            0,
            0,
            CARD_WIDTH,
            160,
            None,
            None,
            hinstance,
            None,
        )
        return self._hwnd

    @staticmethod
    def _placement(anchor: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int]:
        """Sit to the right of the mascot, flipping left when it would run off.

        Clamped to the virtual screen so a companion parked at the edge still
        gets a fully visible bubble.
        """

        left, top, right, bottom = anchor
        width, height = size
        screen_x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        screen_y = user32.GetSystemMetrics(77)
        screen_w = user32.GetSystemMetrics(78)
        screen_h = user32.GetSystemMetrics(79)

        x = right - SHADOW_MARGIN + 4
        if x + width > screen_x + screen_w:
            x = left - width + SHADOW_MARGIN - 4
        y = top + (bottom - top) // 2 - height // 2
        x = max(screen_x, min(x, screen_x + screen_w - width))
        y = max(screen_y, min(y, screen_y + screen_h - height))
        return x, y

    def _push_bitmap(self, image: Image.Image, position: tuple[int, int]) -> None:
        """Hand the RGBA card to Windows as a premultiplied ARGB layered surface."""

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        width, height = image.size

        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # negative = top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        bits_ptr = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            mem_dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0
        )
        payload = premultiplied_bgra(image)
        ctypes.memmove(bits_ptr, payload, len(payload))
        old_bitmap = gdi32.SelectObject(mem_dc, bitmap)

        size = wintypes.SIZE(width, height)
        src_point = wintypes.POINT(0, 0)
        dst_point = wintypes.POINT(*position)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
            wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
            ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow(
            self._hwnd, None, ctypes.byref(dst_point), ctypes.byref(size),
            mem_dc, ctypes.byref(src_point), 0, ctypes.byref(blend), ULW_ALPHA,
        )

        gdi32.SelectObject(mem_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)

    # -- rendering ------------------------------------------------------

    def _render(self, content: BubbleContent) -> Image.Image:
        title_font = load_ui_font(13, bold=True)
        body_font = load_ui_font(12)
        link_font = load_ui_font(12, bold=True)
        small_font = load_ui_font(11)

        text_width = CARD_WIDTH - 2 * PADDING
        measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

        wrapped_lines: list[str] = []
        for line in content.lines:
            wrapped_lines.extend(wrap_text(measure, line, body_font, text_width))

        wrapped_links: list[tuple[list[str], BubbleLink]] = []
        for link in content.links:
            wrapped_links.append((wrap_text(measure, link.title, link_font, text_width - 22)[:2], link))

        # Height is measured before drawing: the card has to be exactly as tall
        # as its content, since a layered window has no scroll area.
        height = PADDING + 20  # header row
        if wrapped_lines:
            height += 4 + len(wrapped_lines) * 16
        if wrapped_links:
            height += 8
            for titles, _link in wrapped_links:
                height += 6 + len(titles) * 15 + 14
        if content.footer:
            height += 8 + 14
        height += PADDING

        canvas = Image.new("RGBA", (CARD_WIDTH + 2 * SHADOW_MARGIN, height + 2 * SHADOW_MARGIN), (0, 0, 0, 0))
        card_box = (SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN + CARD_WIDTH, SHADOW_MARGIN + height)

        # Soft shadow: a blurred copy of the card silhouette, nudged downward.
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (card_box[0], card_box[1] + 3, card_box[2], card_box[3] + 3), RADIUS, fill=COLOR_SHADOW
        )
        canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(5)))

        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(card_box, RADIUS, fill=COLOR_CARD, outline=COLOR_BORDER, width=1)

        # Pointer tail toward the mascot, on the left edge.
        tail_y = card_box[1] + min(height - 24, max(24, height // 2))
        draw.polygon(
            [
                (card_box[0] + 1, tail_y - TAIL),
                (card_box[0] - TAIL + 1, tail_y),
                (card_box[0] + 1, tail_y + TAIL),
            ],
            fill=COLOR_CARD,
            outline=COLOR_BORDER,
        )
        draw.line([(card_box[0] + 1, tail_y - TAIL), (card_box[0] + 1, tail_y + TAIL)], fill=COLOR_CARD)

        x = card_box[0] + PADDING
        y = card_box[1] + PADDING

        draw.ellipse((x, y + 4, x + 8, y + 12), fill=COLOR_ACCENT)
        draw.text((x + 14, y), content.title, font=title_font, fill=COLOR_TITLE)
        y += 20

        if wrapped_lines:
            y += 4
            for line in wrapped_lines:
                draw.text((x, y), line, font=body_font, fill=COLOR_TEXT)
                y += 16

        rows: list[tuple[int, int, int, int, str]] = []
        if wrapped_links:
            y += 8
            for index, (titles, link) in enumerate(wrapped_links):
                row_top = y
                row_height = 6 + len(titles) * 15 + 14
                if index == self._hover:
                    draw.rounded_rectangle(
                        (x - 6, row_top - 2, card_box[2] - PADDING + 6, row_top + row_height - 6),
                        8,
                        fill=COLOR_HOVER,
                    )
                # Globe glyph: a ring with a meridian, drawn rather than
                # shipped as an icon file so it scales with the font size.
                gx, gy = x + 1, row_top + 3
                draw.ellipse((gx, gy, gx + 11, gy + 11), outline=COLOR_ACCENT, width=1)
                draw.line((gx, gy + 5, gx + 11, gy + 5), fill=COLOR_ACCENT)
                draw.arc((gx + 3, gy, gx + 8, gy + 11), 90, 270, fill=COLOR_ACCENT)
                draw.arc((gx + 3, gy, gx + 8, gy + 11), 270, 90, fill=COLOR_ACCENT)

                text_y = row_top
                for title_line in titles:
                    draw.text((x + 18, text_y), title_line, font=link_font, fill=COLOR_ACCENT)
                    text_y += 15
                draw.text((x + 18, text_y + 1), link.domain or link.url, font=small_font, fill=COLOR_MUTED)
                y = row_top + row_height
                if index < len(wrapped_links) - 1:
                    draw.line((x, y - 7, card_box[2] - PADDING, y - 7), fill=COLOR_SEPARATOR)
                rows.append((x - 6, row_top - 2, card_box[2] - PADDING + 6, row_top + row_height - 6, link.url))

        if content.footer:
            y += 8
            draw.text((x, y), content.footer, font=small_font, fill=COLOR_MUTED)

        # Close affordance, top-right.
        close_x = card_box[2] - PADDING - 10
        close_y = card_box[1] + PADDING + 2
        draw.line((close_x, close_y, close_x + 9, close_y + 9), fill=COLOR_MUTED, width=2)
        draw.line((close_x + 9, close_y, close_x, close_y + 9), fill=COLOR_MUTED, width=2)
        content._rows = rows
        self._close_rect = (close_x - 6, close_y - 6, close_x + 15, close_y + 15)
        return canvas

    def _rerender(self) -> None:
        """Redraw in place, e.g. after the hovered row changed."""

        if self._content is None or self._hwnd is None or not self._visible:
            return
        image = self._render(self._content)
        self._image = image
        rect = wintypes.RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        self._push_bitmap(image, (rect.left, rect.top))

    # -- input ----------------------------------------------------------

    def _hit_row(self, x: int, y: int) -> int:
        if self._content is None:
            return -1
        for index, (x0, y0, x1, y1, _url) in enumerate(self._content._rows):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return index
        return -1

    def _wndproc(self, hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_MOUSEMOVE:
            x, y = _lparam_point(lparam)
            hover = self._hit_row(x, y)
            user32.SetCursor(self._hand if hover >= 0 else self._arrow)
            if hover != self._hover:
                self._hover = hover
                self._rerender()
            return 0
        if msg == WM_LBUTTONUP:
            x, y = _lparam_point(lparam)
            close = getattr(self, "_close_rect", None)
            if close and close[0] <= x <= close[2] and close[1] <= y <= close[3]:
                self.hide()
                return 0
            index = self._hit_row(x, y)
            if index >= 0 and self._content is not None:
                url = self._content._rows[index][4]
                if url:
                    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
                self.hide()
                return 0
            if self._content is not None and callable(self._content.on_click):
                callback = self._content.on_click
                self.hide()
                try:
                    callback()
                except Exception:
                    pass
            return 0
        if msg == WM_TIMER and wparam == _AUTO_HIDE_TIMER:
            self.hide()
            return 0
        if msg == WM_DESTROY:
            self._hwnd = None
            self._visible = False
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _lparam_point(lparam: int) -> tuple[int, int]:
    """Split a packed WM_MOUSE* lParam into signed client coordinates."""

    x = lparam & 0xFFFF
    y = (lparam >> 16) & 0xFFFF
    return (x - 0x10000 if x > 0x7FFF else x, y - 0x10000 if y > 0x7FFF else y)


def premultiplied_bgra(image: Image.Image) -> bytes:
    """Convert straight RGBA to the premultiplied BGRA UpdateLayeredWindow wants.

    Public because every layered window in the app needs exactly this
    conversion (see prompt_window.py); duplicating it would risk the two
    surfaces drifting apart.
    """

    import numpy as np

    rgba = np.asarray(image.convert("RGBA"), dtype=np.float64)
    alpha = rgba[..., 3:4] / 255.0
    rgb = (rgba[..., :3] * alpha).clip(0, 255).astype(np.uint8)
    bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], rgba[..., 3].astype(np.uint8)])
    return bgra.tobytes()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """Greedy word wrap against measured pixel width.

    Shared with prompt_window.py so both cards break lines identically.
    """

    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
