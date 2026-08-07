"""Hand-drawn question prompt for the desktop companion.

The mascot has to ask for a sentence before it can act: "what do you want to
know about this capture?" after an OCR selection, or "what should I search
for?" behind the magnifying glass. That used to be a Tkinter dialog, and the
user's verdict was blunt: Windows' own windows are ugly. So this module draws
the prompt itself, with the exact same recipe as bubble.py - a ``WS_POPUP`` /
``WS_EX_LAYERED`` window whose whole surface is a 32-bit ARGB bitmap pushed
through ``UpdateLayeredWindow``, painted with PIL, rounded corners, blurred
shadow, and the palette shared with the speech bubble. The two windows are
meant to look like siblings, so the colours, radii, paddings and fonts all
come from bubble.py rather than being redeclared here.

Three consequences of that choice drive the code below.

* **No native child controls.** A layered window's pixels come exclusively
  from the bitmap handed to ``UpdateLayeredWindow``; a child ``EDIT`` control
  paints through the normal WM_PAINT path and simply never appears on that
  surface. It is the same wall WebView2 hit with transparency (documented at
  the top of native_widget.py). The text field is therefore drawn by hand:
  an internal string buffer, a caret index, and key handling in the window
  procedure.
* **The window is created on the calling thread.** Win32 windows are
  thread-affine: messages are only ever delivered to the thread that created
  the window. ``ask_question`` is called from the companion's background
  worker, so it creates the window *there* and runs its own
  ``GetMessageW`` / ``TranslateMessage`` / ``DispatchMessageW`` loop until the
  user answers. Creating it on one thread and pumping it from another would
  leave the card visible but deaf to keyboard and mouse. The call is
  blocking by design and must not be made from the mascot's own UI thread,
  which already owns a message loop.
* **It must take focus**, unlike the bubble (``WS_EX_NOACTIVATE``), because
  the user is about to type into it: the window is activable and asks for the
  foreground plus keyboard focus right after creation. It keeps
  ``WS_EX_TOOLWINDOW`` (out of Alt-Tab and the taskbar) and
  ``WS_EX_TOPMOST``.

Failure policy: if the window cannot be created, ``ask_question`` does *not*
raise. It returns ``PromptResult("", checkbox_default)``, i.e. "no question
asked, defaults kept", so the OCR flow that called it falls back to a general
analysis of the capture instead of throwing away a screenshot the user just
took. Cancellation is a different answer and returns ``None``.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .bubble import (
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_HOVER,
    COLOR_MUTED,
    COLOR_SEPARATOR,
    COLOR_SHADOW,
    COLOR_TEXT,
    COLOR_TITLE,
    load_ui_font,
    premultiplied_bgra,
    wrap_text,
)

# The two structure layouts are dictated by Win32, and ctypes function
# prototypes are shared process-wide: reusing bubble's declarations keeps
# ``UpdateLayeredWindow``'s argtypes pointing at the very same struct type
# both modules pass, which is the pattern bubble.py and native_widget.py
# already follow (each re-asserts the argtypes right before its own call).
from .bubble import _BITMAPINFOHEADER, _BLENDFUNCTION, _WNDCLASS  # noqa: PLC2701

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
BI_RGB = 0
DIB_RGB_COLORS = 0
SW_SHOW = 5
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_KEYDOWN = 0x0100
WM_CHAR = 0x0102
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202

VK_BACK = 0x08
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_RIGHT = 0x27
VK_DELETE = 0x2E
VK_V = 0x56

WM_QUIT = 0x0012
PM_REMOVE = 0x0001

CF_UNICODETEXT = 13
IDC_ARROW = 32512
IDC_IBEAM = 32513
IDC_HAND = 32649
ERROR_CLASS_ALREADY_EXISTS = 1410

#: Windows' own caret cadence, so the field blinks like every other field.
CARET_BLINK_MS = 530
_CARET_TIMER = 1

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
HANDLE = wintypes.HANDLE

# Same reasoning as bubble.py / native_widget.py: anything crossing a handle
# or a pointer-sized value needs explicit argtypes, otherwise ctypes assumes
# 32-bit ints and raises "int too long to convert" as soon as a handle uses
# the high bits on 64-bit Windows. Values below are deliberately identical to
# bubble.py's for the calls both modules share.
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [
    wintypes.HWND, HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetFocus.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT,
]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.GetDC.restype = HANDLE
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, HANDLE]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.LoadCursorW.restype = HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.SetCursor.restype = HANDLE
user32.SetCursor.argtypes = [HANDLE]
user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
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
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [HANDLE]
kernel32.GlobalUnlock.argtypes = [HANDLE]

# ------------------------------------------------------------------ styling
#
# Sizes are bubble.py's, nudged for a form rather than a notification: a
# little wider (a question needs room) and a little more padding, same radius
# family so the two cards read as one design.

CARD_WIDTH = 380
SHADOW_MARGIN = 14
PADDING = 18
RADIUS = 16
HEADER_HEIGHT = 22
LINE_HEIGHT = 16
#: A pathological description must not grow a card taller than the screen.
MAX_DESCRIPTION_LINES = 8
FIELD_HEIGHT = 36
FIELD_RADIUS = 10
FIELD_INSET = 10
CHECKBOX_SIZE = 16
CHECKBOX_ROW_HEIGHT = 18
BUTTON_HEIGHT = 32
BUTTON_RADIUS = 9
BUTTON_GAP = 8
CARET_WIDTH = 2

COLOR_FIELD = (255, 255, 255, 255)
COLOR_BUTTON_SOFT = (229, 229, 234, 255)
COLOR_BUTTON_SOFT_HOVER = (214, 214, 222, 255)
COLOR_BUTTON_TEXT = (255, 255, 255, 255)

#: Hit-test zone names, also used as hover keys.
ZONE_FIELD = "field"
ZONE_CHECKBOX = "checkbox"
ZONE_CONFIRM = "confirm"
ZONE_CANCEL = "cancel"
ZONE_CLOSE = "close"
_CLICKABLE = (ZONE_CHECKBOX, ZONE_CONFIRM, ZONE_CANCEL, ZONE_CLOSE)

_MEASURE = ImageDraw.Draw(Image.new("RGBA", (1, 1)))


@lru_cache(maxsize=16)
def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """Memoised ``load_ui_font``: the caret redraws the card twice a second."""

    return load_ui_font(size, bold=bold)


def _line_height(font: ImageFont.ImageFont, fallback: int) -> int:
    try:
        box = font.getbbox("Ag")
        return max(1, int(box[3] - box[1]))
    except Exception:  # pragma: no cover - only the bitmap fallback font
        return fallback


@dataclass
class PromptResult:
    """What the user answered: the typed text and the checkbox state."""

    text: str
    checkbox: bool


# ---------------------------------------------------------------- rendering


def _field_scroll(text: str, caret: int, font: ImageFont.ImageFont, inner_width: int) -> int:
    """Horizontal pixel offset that keeps the caret inside the field.

    Derived from the caret position on every render instead of being carried
    as state, so "the caret is always visible" is true by construction rather
    than by bookkeeping.
    """

    total = _MEASURE.textlength(text, font=font)
    if total <= inner_width:
        return 0
    caret_x = _MEASURE.textlength(text[:caret], font=font)
    scroll = 0.0
    if caret_x > inner_width - CARET_WIDTH - 2:
        scroll = caret_x - inner_width + CARET_WIDTH + 2
    scroll = min(scroll, total - inner_width + CARET_WIDTH + 2)
    return int(max(0.0, scroll))


def _caret_from_x(text: str, font: ImageFont.ImageFont, offset_x: int) -> int:
    """Index of the character boundary closest to ``offset_x`` inside the text."""

    best_index, best_delta = 0, abs(offset_x)
    for index in range(1, len(text) + 1):
        delta = abs(_MEASURE.textlength(text[:index], font=font) - offset_x)
        if delta < best_delta:
            best_index, best_delta = index, delta
    return best_index


def _rounded(draw: ImageDraw.ImageDraw, box, radius, **kwargs) -> None:
    """rounded_rectangle guard: PIL raises when a box has negative extent."""

    if box[2] > box[0] and box[3] > box[1]:
        draw.rounded_rectangle(box, radius, **kwargs)


def _render_card(
    *,
    title: str,
    description: str = "",
    text: str = "",
    caret: int | None = None,
    hover: str | None = None,
    checkbox: bool = False,
    placeholder: str = "",
    checkbox_label: str | None = None,
    confirm_label: str = "Continuer",
    cancel_label: str = "Annuler",
    caret_visible: bool = True,
    zones: dict[str, tuple[int, int, int, int]] | None = None,
) -> Image.Image:
    """Paint the whole prompt into an RGBA image, shadow margin included.

    Pure function of the state it is given - buffer, caret, hover zone,
    checkbox - which is what makes it testable without ever opening a window.
    When ``zones`` is passed it is filled with the hit-test rectangles, in the
    same coordinates as the returned image (and therefore as the window's
    client area, since the bitmap covers it 1:1).
    """

    title_font = _font(14, bold=True)
    body_font = _font(12)
    field_font = _font(13)
    label_font = _font(11)
    button_font = _font(12, bold=True)

    content_width = CARD_WIDTH - 2 * PADDING
    caret = len(text) if caret is None else max(0, min(int(caret), len(text)))
    description_lines = wrap_text(_MEASURE, description, body_font, content_width - 4)
    if len(description_lines) > MAX_DESCRIPTION_LINES:
        description_lines = description_lines[:MAX_DESCRIPTION_LINES]
        description_lines[-1] = description_lines[-1] + " ..."

    # Height first: a layered window has no scroll area, so the card must be
    # exactly as tall as its content (same approach as bubble.py).
    height = PADDING + HEADER_HEIGHT
    if description_lines:
        height += 4 + len(description_lines) * LINE_HEIGHT
    height += 12 + FIELD_HEIGHT
    if checkbox_label:
        height += 12 + CHECKBOX_ROW_HEIGHT
    height += 16 + BUTTON_HEIGHT + PADDING

    canvas = Image.new("RGBA", (CARD_WIDTH + 2 * SHADOW_MARGIN, height + 2 * SHADOW_MARGIN), (0, 0, 0, 0))
    card = (SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN + CARD_WIDTH, SHADOW_MARGIN + height)

    # Soft shadow: blurred silhouette of the card, nudged down.
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (card[0], card[1] + 3, card[2], card[3] + 3), RADIUS, fill=COLOR_SHADOW
    )
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(6)))

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(card, RADIUS, fill=COLOR_CARD, outline=COLOR_BORDER, width=1)

    x = card[0] + PADDING
    right = card[2] - PADDING
    y = card[1] + PADDING

    # Header: accent dot + bold title, exactly like the bubble's. The title
    # is kept to one line so it never runs under the close cross.
    draw.ellipse((x, y + 4, x + 8, y + 12), fill=COLOR_ACCENT)
    title_lines = wrap_text(_MEASURE, title, title_font, content_width - 40)
    header = title_lines[0] if title_lines else ""
    if len(title_lines) > 1:
        header += " ..."
    draw.text((x + 14, y), header, font=title_font, fill=COLOR_TITLE)
    y += HEADER_HEIGHT

    if description_lines:
        y += 4
        for line in description_lines:
            draw.text((x, y), line, font=body_font, fill=COLOR_TEXT)
            y += LINE_HEIGHT

    # ---- text field, drawn by hand (no native EDIT child, see module docstring)
    y += 12
    field = (x, y, right, y + FIELD_HEIGHT)
    # The field is always the focused element, so it always wears the accent
    # ring; a soft halo outside it stands in for the platform focus glow.
    _rounded(draw, (field[0] - 2, field[1] - 2, field[2] + 2, field[3] + 2), FIELD_RADIUS + 2, fill=COLOR_ACCENT_SOFT)
    _rounded(draw, field, FIELD_RADIUS, fill=COLOR_FIELD, outline=COLOR_ACCENT, width=1)

    inner_left = field[0] + FIELD_INSET
    inner_width = max(1, (field[2] - FIELD_INSET) - inner_left)
    strip_height = FIELD_HEIGHT - 4
    text_height = _line_height(field_font, 13)
    text_y = max(0, (strip_height - text_height) // 2)

    # Clipping: PIL has no clip region, and a scrolled string would otherwise
    # bleed over the card's rounded edge. Draw the visible strip on its own
    # transparent tile and composite it inside the field.
    strip = Image.new("RGBA", (inner_width, strip_height), (0, 0, 0, 0))
    strip_draw = ImageDraw.Draw(strip)
    scroll = _field_scroll(text, caret, field_font, inner_width)
    if text:
        strip_draw.text((-scroll, text_y), text, font=field_font, fill=COLOR_TITLE)
    elif placeholder:
        strip_draw.text((0, text_y), placeholder, font=field_font, fill=COLOR_MUTED)
    if caret_visible:
        caret_x = int(_MEASURE.textlength(text[:caret], font=field_font)) - scroll
        caret_x = max(0, min(caret_x, inner_width - CARET_WIDTH))
        strip_draw.rectangle(
            (caret_x, text_y - 2, caret_x + CARET_WIDTH - 1, text_y + text_height + 2), fill=COLOR_ACCENT
        )
    canvas.alpha_composite(strip, (inner_left, field[1] + 2))
    y = field[3]

    # ---- checkbox, also hand-drawn
    check_zone: tuple[int, int, int, int] | None = None
    if checkbox_label:
        y += 12
        box = (x, y, x + CHECKBOX_SIZE, y + CHECKBOX_SIZE)
        hovered = hover == ZONE_CHECKBOX
        if checkbox:
            _rounded(draw, box, 4, fill=COLOR_ACCENT, outline=COLOR_ACCENT, width=1)
            # The tick itself: two strokes, so it scales with CHECKBOX_SIZE.
            draw.line(
                (
                    box[0] + 4, box[1] + CHECKBOX_SIZE // 2,
                    box[0] + CHECKBOX_SIZE // 2 - 1, box[3] - 5,
                ),
                fill=COLOR_BUTTON_TEXT,
                width=2,
            )
            draw.line(
                (box[0] + CHECKBOX_SIZE // 2 - 1, box[3] - 5, box[2] - 4, box[1] + 4),
                fill=COLOR_BUTTON_TEXT,
                width=2,
            )
        else:
            _rounded(
                draw, box, 4,
                fill=COLOR_HOVER if hovered else COLOR_FIELD,
                outline=COLOR_ACCENT if hovered else COLOR_BORDER,
                width=1,
            )
        draw.text(
            (box[2] + 9, y + 1),
            checkbox_label,
            font=label_font,
            fill=COLOR_TEXT if (checkbox or hovered) else COLOR_MUTED,
        )
        label_width = int(_MEASURE.textlength(checkbox_label, font=label_font))
        check_zone = (box[0] - 4, y - 3, min(right, box[2] + 13 + label_width), y + CHECKBOX_ROW_HEIGHT + 1)
        y += CHECKBOX_ROW_HEIGHT

    # ---- buttons: primary on the left, cancel on the right (Windows order)
    y += 16
    cancel_width = max(88, int(_MEASURE.textlength(cancel_label, font=button_font)) + 28)
    confirm_width = max(96, int(_MEASURE.textlength(confirm_label, font=button_font)) + 30)
    cancel_box = (right - cancel_width, y, right, y + BUTTON_HEIGHT)
    confirm_box = (
        max(x, cancel_box[0] - BUTTON_GAP - confirm_width), y,
        cancel_box[0] - BUTTON_GAP, y + BUTTON_HEIGHT,
    )

    _rounded(
        draw, cancel_box, BUTTON_RADIUS,
        fill=COLOR_BUTTON_SOFT_HOVER if hover == ZONE_CANCEL else COLOR_BUTTON_SOFT,
        outline=COLOR_SEPARATOR,
        width=1,
    )
    _rounded(
        draw, confirm_box, BUTTON_RADIUS,
        fill=COLOR_ACCENT if hover == ZONE_CONFIRM else COLOR_TITLE,
    )
    _draw_centered(draw, cancel_box, cancel_label, button_font, COLOR_TITLE)
    _draw_centered(draw, confirm_box, confirm_label, button_font, COLOR_BUTTON_TEXT)

    # ---- close cross, top-right, same geometry as the bubble's
    close_x = card[2] - PADDING - 10
    close_y = card[1] + PADDING + 2
    close_zone = (close_x - 6, close_y - 6, close_x + 15, close_y + 15)
    if hover == ZONE_CLOSE:
        draw.ellipse(close_zone, fill=COLOR_HOVER)
    ink = COLOR_TITLE if hover == ZONE_CLOSE else COLOR_MUTED
    draw.line((close_x, close_y, close_x + 9, close_y + 9), fill=ink, width=2)
    draw.line((close_x + 9, close_y, close_x, close_y + 9), fill=ink, width=2)

    if zones is not None:
        zones.clear()
        zones[ZONE_FIELD] = field
        zones[ZONE_CONFIRM] = confirm_box
        zones[ZONE_CANCEL] = cancel_box
        zones[ZONE_CLOSE] = close_zone
        if check_zone is not None:
            zones[ZONE_CHECKBOX] = check_zone
    return canvas


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    """Centre ``label`` in ``box`` without PIL anchors.

    ``anchor=`` is unsupported by the bitmap fallback font load_ui_font
    degrades to, so the offsets are measured instead.
    """

    width = _MEASURE.textlength(label, font=font)
    height = _line_height(font, 12)
    draw.text(
        (box[0] + (box[2] - box[0] - width) / 2, box[1] + (box[3] - box[1] - height) / 2 - 1),
        label,
        font=font,
        fill=fill,
    )


def _clipboard_text() -> str:
    """Read CF_UNICODETEXT, flattened to one line.

    The field is single-line, so newlines and tabs collapse into spaces
    rather than being pasted as invisible control characters.
    """

    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            raw = ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    return " ".join(raw.split())


# ------------------------------------------------------------------- window
#
# One window class for the whole process, registered once, with a
# module-level trampoline that routes messages to the instance owning the
# hwnd. Registering a fresh class per call would be worse than useless: the
# class outlives the call and keeps a raw pointer to its WNDPROC, so a
# second prompt would jump into a callback the garbage collector had already
# freed. Keying on hwnd also makes concurrent prompts on different threads
# safe.

_CLASS_NAME = "ThreeLoopPromptWindow"
_class_lock = threading.Lock()
_class_registered = False
_windows: dict[int, "_PromptWindow"] = {}
_windows_lock = threading.Lock()


def _dispatch(hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
    with _windows_lock:
        window = _windows.get(int(hwnd or 0))
    if window is None:
        # Messages sent during CreateWindowExW arrive before the hwnd is
        # mapped; none of them need our handling.
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return window.handle_message(hwnd, msg, wparam, lparam)


#: Module-level strong reference: the window class stores a raw function
#: pointer, so this trampoline must live as long as the process.
_WNDPROC_REF = WNDPROC(_dispatch)


def _lparam_point(lparam: int) -> tuple[int, int]:
    """Split a packed WM_MOUSE* lParam into signed client coordinates."""

    x = lparam & 0xFFFF
    y = (lparam >> 16) & 0xFFFF
    return (x - 0x10000 if x > 0x7FFF else x, y - 0x10000 if y > 0x7FFF else y)


def _drain_quit_messages() -> int:
    """Discard any pending WM_QUIT on this thread; return how many were removed.

    Returning the count is what makes this verifiable without a real window.
    """

    msg = wintypes.MSG()
    discarded = 0
    while user32.PeekMessageW(ctypes.byref(msg), None, WM_QUIT, WM_QUIT, PM_REMOVE):
        discarded += 1
    return discarded


def _ensure_class() -> None:
    global _class_registered
    with _class_lock:
        if _class_registered:
            return
        wndclass = _WNDCLASS()
        wndclass.style = 0
        wndclass.lpfnWndProc = ctypes.cast(_WNDPROC_REF, ctypes.c_void_p)
        wndclass.hInstance = kernel32.GetModuleHandleW(None)
        wndclass.hCursor = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_ARROW), wintypes.LPCWSTR))
        wndclass.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            error = kernel32.GetLastError()
            if error != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(error)
        _class_registered = True


class _PromptWindow:
    """State and message handling for one blocking prompt."""

    def __init__(
        self,
        *,
        title: str,
        description: str,
        placeholder: str,
        checkbox_label: str | None,
        checkbox_default: bool,
        confirm_label: str,
        cancel_label: str,
    ) -> None:
        self._title = title
        self._description = description
        self._placeholder = placeholder
        self._checkbox_label = checkbox_label
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label

        self._buffer = ""
        self._caret = 0
        self._checkbox = bool(checkbox_default)
        self._hover: str | None = None
        self._caret_visible = True
        self._zones: dict[str, tuple[int, int, int, int]] = {}

        self._hwnd: int | None = None
        self._origin = (0, 0)
        self._size = (0, 0)
        self._closed = False
        self._result: PromptResult | None = None
        self._error: BaseException | None = None

        self._arrow = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_ARROW), wintypes.LPCWSTR))
        self._hand = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_HAND), wintypes.LPCWSTR))
        self._beam = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_IBEAM), wintypes.LPCWSTR))

    # -- lifecycle ------------------------------------------------------

    def run(self) -> PromptResult | None:
        """Create, show and pump the window until the user answers.

        Raises if the window could not be created or if the message handling
        blew up; ``ask_question`` turns that into the documented fallback.
        """

        self._create()
        try:
            self._pump()
        finally:
            self._teardown()
        if self._error is not None:
            raise self._error
        return self._result

    def _create(self) -> None:
        _ensure_class()
        # A WM_QUIT is queued per *thread*, not per window, and outlives the
        # loop that caused it. This card is opened from the companion's worker
        # thread, right after that thread ran another modal loop (the capture
        # overlay), so a quit left behind there would make our own pump exit
        # before the user ever saw the card - which read as "the OCR does
        # nothing". The source of that stray quit is fixed in
        # screen_capture.py; draining here keeps the card immune to any other
        # modal loop that might run on this thread in the future.
        _drain_quit_messages()
        image = self._render()
        width, height = image.size
        x, y = self._centered(width, height)

        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            _CLASS_NAME,
            "3loop-prompt",
            WS_POPUP,
            x,
            y,
            width,
            height,
            None,
            None,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not hwnd:
            raise ctypes.WinError(kernel32.GetLastError())
        self._hwnd = int(hwnd)
        self._origin = (x, y)
        self._size = (width, height)
        with _windows_lock:
            _windows[self._hwnd] = self

        self._push(image)
        user32.ShowWindow(self._hwnd, SW_SHOW)
        user32.SetWindowPos(self._hwnd, HANDLE(HWND_TOPMOST), x, y, width, height, SWP_SHOWWINDOW)
        self._take_focus()
        user32.SetTimer(self._hwnd, _CARET_TIMER, CARET_BLINK_MS, None)

    def _take_focus(self) -> None:
        """Ask for the foreground, then for the caret.

        Windows delivers keystrokes to the focused window *of the foreground
        thread*, so both calls are needed - and SetForegroundWindow can be
        refused when our process is not the one the user last interacted
        with, hence the single retry after raising the window.
        """

        user32.SetForegroundWindow(self._hwnd)
        if int(user32.GetForegroundWindow() or 0) != self._hwnd:
            user32.BringWindowToTop(self._hwnd)
            user32.SetForegroundWindow(self._hwnd)
        user32.SetFocus(self._hwnd)

    def _pump(self) -> None:
        """Run this thread's own message loop until the card is done.

        ``GetMessageW`` is called with a NULL hwnd filter on purpose: filtering
        on our window would hide thread messages and anything posted by the
        timer teardown. The loop ends on the ``_closed`` flag rather than on
        ``PostQuitMessage`` so a WM_QUIT never escapes into another loop
        running on this thread.
        """

        msg = wintypes.MSG()
        while not self._closed:
            status = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if status <= 0:  # 0 = WM_QUIT, -1 = error
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _teardown(self) -> None:
        hwnd, self._hwnd = self._hwnd, None
        if hwnd is None:
            return
        with _windows_lock:
            _windows.pop(hwnd, None)
        user32.KillTimer(hwnd, _CARET_TIMER)
        user32.DestroyWindow(hwnd)

    def _finish(self, result: PromptResult | None) -> None:
        self._result = result
        self._closed = True
        hwnd = self._hwnd
        if hwnd is not None:
            user32.KillTimer(hwnd, _CARET_TIMER)
            user32.DestroyWindow(hwnd)  # sends WM_DESTROY synchronously

    @staticmethod
    def _centered(width: int, height: int) -> tuple[int, int]:
        """Centre on the virtual screen, clamped so nothing hangs off it."""

        screen_x = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        screen_y = user32.GetSystemMetrics(77)
        screen_w = user32.GetSystemMetrics(78) or width
        screen_h = user32.GetSystemMetrics(79) or height
        x = screen_x + (screen_w - width) // 2
        y = screen_y + (screen_h - height) // 3  # slightly above centre
        x = max(screen_x, min(x, screen_x + max(0, screen_w - width)))
        y = max(screen_y, min(y, screen_y + max(0, screen_h - height)))
        return x, y

    # -- painting -------------------------------------------------------

    def _render(self) -> Image.Image:
        return _render_card(
            title=self._title,
            description=self._description,
            text=self._buffer,
            caret=self._caret,
            hover=self._hover,
            checkbox=self._checkbox,
            placeholder=self._placeholder,
            checkbox_label=self._checkbox_label,
            confirm_label=self._confirm_label,
            cancel_label=self._cancel_label,
            caret_visible=self._caret_visible,
            zones=self._zones,
        )

    def _redraw(self) -> None:
        if self._hwnd is None or self._closed:
            return
        self._push(self._render())

    def _push(self, image: Image.Image) -> None:
        """Hand the RGBA card to Windows as a premultiplied ARGB surface.

        The DIB plumbing mirrors bubble.py's; the pixel conversion itself is
        the shared ``premultiplied_bgra``.
        """

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
        dst_point = wintypes.POINT(*self._origin)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        # Re-asserted here, as in bubble.py and native_widget.py: each module
        # passes its own _BLENDFUNCTION type and the prototype is shared.
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

    # -- editing --------------------------------------------------------

    def _insert(self, chunk: str) -> None:
        if not chunk:
            return
        self._buffer = self._buffer[: self._caret] + chunk + self._buffer[self._caret :]
        self._caret += len(chunk)
        self._caret_visible = True
        self._redraw()

    def _move_caret(self, caret: int) -> None:
        caret = max(0, min(caret, len(self._buffer)))
        if caret == self._caret and self._caret_visible:
            return
        self._caret = caret
        self._caret_visible = True
        self._redraw()

    def _backspace(self) -> None:
        if self._caret == 0:
            return
        self._buffer = self._buffer[: self._caret - 1] + self._buffer[self._caret :]
        self._caret -= 1
        self._caret_visible = True
        self._redraw()

    def _delete(self) -> None:
        if self._caret >= len(self._buffer):
            return
        self._buffer = self._buffer[: self._caret] + self._buffer[self._caret + 1 :]
        self._caret_visible = True
        self._redraw()

    def _toggle_checkbox(self) -> None:
        self._checkbox = not self._checkbox
        self._redraw()

    # -- input ----------------------------------------------------------

    def _hit(self, x: int, y: int) -> str | None:
        for name, (x0, y0, x1, y1) in self._zones.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return name
        return None

    def handle_message(self, hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
        """Window procedure body, guarded so no exception crosses ctypes.

        A Python exception raised inside a ctypes callback is printed and
        swallowed, which would leave the card alive but unresponsive; capturing
        it here lets ``run`` re-raise and the caller fall back cleanly.
        """

        try:
            return self._handle(hwnd, msg, wparam, lparam)
        except BaseException as exc:  # noqa: BLE001 - see docstring
            self._error = exc
            self._result = None
            self._closed = True
            return 0

    def _handle(self, hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_MOUSEMOVE:
            x, y = _lparam_point(lparam)
            zone = self._hit(x, y)
            user32.SetCursor(
                self._hand if zone in _CLICKABLE else self._beam if zone == ZONE_FIELD else self._arrow
            )
            # Only repaint when the hovered zone actually changes: the card is
            # a full bitmap upload, redrawing it per mouse pixel would burn CPU
            # for nothing.
            if zone != self._hover:
                self._hover = zone
                self._redraw()
            return 0

        if msg == WM_LBUTTONUP:
            x, y = _lparam_point(lparam)
            zone = self._hit(x, y)
            if zone == ZONE_CONFIRM:
                self._finish(PromptResult(self._buffer.strip(), self._checkbox))
            elif zone in (ZONE_CANCEL, ZONE_CLOSE):
                self._finish(None)
            elif zone == ZONE_CHECKBOX:
                self._toggle_checkbox()
            elif zone == ZONE_FIELD:
                # Put the caret where the user clicked, scroll included.
                field = self._zones[ZONE_FIELD]
                inner_left = field[0] + FIELD_INSET
                inner_width = max(1, (field[2] - FIELD_INSET) - inner_left)
                font = _font(13)
                scroll = _field_scroll(self._buffer, self._caret, font, inner_width)
                self._move_caret(_caret_from_x(self._buffer, font, x - inner_left + scroll))
            return 0

        if msg == WM_CHAR:
            char = int(wparam)
            # Control characters (Enter, Escape, Ctrl+V's 0x16, ...) are
            # handled in WM_KEYDOWN; only printable input reaches the buffer.
            if char >= 32 and char != 127:
                self._insert(chr(char))
            return 0

        if msg == WM_KEYDOWN:
            key = int(wparam)
            ctrl = bool(user32.GetKeyState(VK_CONTROL) & 0x8000)
            if ctrl and key == VK_V:
                self._insert(_clipboard_text())
                return 0
            if key == VK_BACK:
                self._backspace()
            elif key == VK_DELETE:
                self._delete()
            elif key == VK_LEFT:
                self._move_caret(self._caret - 1)
            elif key == VK_RIGHT:
                self._move_caret(self._caret + 1)
            elif key == VK_HOME:
                self._move_caret(0)
            elif key == VK_END:
                self._move_caret(len(self._buffer))
            elif key == VK_RETURN:
                self._finish(PromptResult(self._buffer.strip(), self._checkbox))
            elif key == VK_ESCAPE:
                self._finish(None)
            return 0

        if msg == WM_TIMER and int(wparam) == _CARET_TIMER:
            self._caret_visible = not self._caret_visible
            self._redraw()
            return 0

        if msg == WM_CLOSE:  # Alt+F4 and friends: same as cancelling
            self._finish(None)
            return 0

        if msg == WM_DESTROY:
            self._closed = True
            hwnd_int = self._hwnd
            self._hwnd = None
            if hwnd_int is not None:
                with _windows_lock:
                    _windows.pop(hwnd_int, None)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def ask_question(
    *,
    title: str,
    description: str,
    placeholder: str = "",
    checkbox_label: str | None = None,
    checkbox_default: bool = False,
    confirm_label: str = "Continuer",
    cancel_label: str = "Annuler",
) -> PromptResult | None:
    """Ask the user one question in a card drawn by the app itself.

    Blocking: it returns when the user answers, cancels or closes the window.
    Call it from a background worker - it creates the window on the calling
    thread and runs that thread's message loop, so calling it from the
    mascot's UI thread would nest a second loop inside the first.

    Returns ``None`` when the user cancels (Cancel button, Escape, close
    cross) and ``PromptResult(text.strip(), checkbox)`` otherwise. If the
    window cannot be created or shown at all, it returns
    ``PromptResult("", checkbox_default)`` instead of raising: the callers are
    OCR and web-search flows that already hold a capture or a request, and
    losing that to a windowing error would be worse than continuing with a
    general analysis.
    """

    window = _PromptWindow(
        title=title,
        description=description,
        placeholder=placeholder,
        checkbox_label=checkbox_label,
        checkbox_default=checkbox_default,
        confirm_label=confirm_label,
        cancel_label=cancel_label,
    )
    try:
        return window.run()
    except Exception:
        return PromptResult("", bool(checkbox_default))
