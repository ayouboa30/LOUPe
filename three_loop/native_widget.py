"""Pure Win32 floating desktop companion with real per-pixel transparency.

WebView2 (used for the main window) turned out not to support genuine
transparency reliably on this system: three different tricks tried against a
pywebview window (layered color-key, DWM frame extension, WinForms
TransparencyKey) all left a solid rectangle behind the sprite, because a
hosted browser control renders through its own composition surface that
ignores those tricks. This module sidesteps the problem entirely by not using
a browser control at all: it paints the sprite with Win32's
``UpdateLayeredWindow`` API directly onto a 32-bit ARGB bitmap, which is the
standard, reliable mechanism native "desktop pet" apps use.

The character
-------------
The art is generated pixel art living in ``web/assets``: the kawaii 8-bit
researcher (lab coat, magnifier) is the character the app ships, and the older
slime set is kept as a fallback so a half-regenerated asset folder still
starts (see ``_CHARACTER_SETS`` / ``_resolve_character_set``). Two sheets are
read per character - the plain one and its "watch" twin, worn while
research-assistant mode is on - never the ``*_strip.png`` variants, which bake
the eyes in for the web avatar's CSS sprite.

Nothing about the grid is hard-coded: ``logical_size``, ``frame_count`` and the
clip bounds all come from the sheet's JSON, and the upscale factor is derived
from ``logical_size`` against a constant on-screen sprite size
(``_SPRITE_TARGET_PX``). That is what lets the character move from a 32x32 grid
at x4 to a 64x64 grid at x2 with no change here.

Three rendering rules come out of the art being *pixel* art:

* **The upscale factor is an integer and the filter is nearest-neighbour**
  (``_SPRITE_TARGET_PX // logical_size``: x4 for a 32x32 cell, x2 for a 64x64
  one, both landing on 128px). A fractional factor makes some source pixels two
  screen pixels wide and their neighbours one, which is exactly what makes
  upscaled pixel art look broken; a smooth filter (bilinear, Lanczos) turns the
  1px outline into grey mush. The previous hand-drawn set was resized by 0.48
  with Lanczos - correct for that art, fatal for this one.
* **The eyes are drawn here, at runtime**, on the frames whose metadata says
  ``eyes`` is not ``null``. That is what lets the character follow the cursor
  and blink without shipping one sheet per look direction. On the vanish/return
  frames the eyes are already in the sheet (they have to shrink with the
  imploding body), and the metadata marks them ``null`` so this module draws
  nothing there.
* **Every frame is stored premultiplied BGRA in a numpy array** - the DIB's own
  memory layout - and a redraw is numpy slice assignments plus a single
  ``ctypes.memmove``. ``_redraw`` runs every 33 ms; a per-pixel or per-row
  Python loop at that rate is what made the old mouse-facing mirror expensive
  enough to be worth deleting (the character looks at the cursor with its eyes
  instead).

Where it may sit
----------------
The window is clamped to the *work area* of the monitor it is on - the desktop
minus the taskbar - at first placement, on every drag step, and whenever the
display layout or the work area changes (``WM_DISPLAYCHANGE``,
``WM_SETTINGCHANGE``). Two details matter:

* The work area comes from ``MonitorFromWindow`` + ``GetMonitorInfoW``'s
  ``rcWork``, not from ``SystemParametersInfo(SPI_GETWORKAREA)``, which only
  ever describes the *primary* monitor and therefore puts the floor in the
  wrong place on a multi-screen desk.
* The clamp is on the character's **feet**, not on the bottom of the canvas:
  the sprite carries a ground shadow and some breathing room underneath, so
  parking the canvas bottom on the taskbar would leave a visible gap. The
  sheet's JSON publishes ``ground_y`` (the ground line, in logical pixels) and
  ``_feet_slack`` turns it into the number of canvas pixels that may legally
  hang below the work area.

The window also re-asserts its topmost rank about once a second (see
``_TOPMOST_REASSERT_SECONDS``): ``WS_EX_TOPMOST`` alone loses the fight against
another topmost window, a full-screen app or a desktop switch.

Behaviour
---------
Hovering fades in a column of four pixel-art icons: mic (one-shot voice
question), scan (drag-select a region, OCR it, search from it, explain it),
globe (background web research) and flask (research-assistant mode). The
character hops on its own every few seconds, when the pointer arrives, and when
a background action lands, so it reads as alive rather than as a static PNG.

The globe is the "black hole" flow: it asks for the question, plays ``vanish``,
hides its own window while the local engine researches, then reappears with
``return`` and a bubble saying "click me to see the result". The result is held
until the user clicks the body, which opens a second bubble of clickable
articles. The window must never stay hidden - it is the app's only entry point
once the main window is closed - so a failed or slow search brings it back
anyway (see ``_RESEARCH_TIMEOUT_SECONDS``).

The flask toggles research-assistant mode: a ``ScreenWatcher`` reads the screen
every few minutes and offers articles about what is on it, and the character
wears its "watch" variant while it does. The bubble is hidden around each
capture, because otherwise OCR reads the *previous* suggestion off the screen
and the search starts feeding on its own output.

How it speaks
-------------
Every status message is a **speech bubble from the character** (``bubble.py``),
never a tray balloon: the balloon is generic Windows chrome, off-brand, and
invisible altogether when system notifications are muted. ``notify.show_toast``
survives as the single fallback for when the bubble cannot be drawn at all -
typically a message emitted before the window exists. ``_say`` is the one door
to all of it, and it sorts messages into two levels: *step* chatter ("I'm
reading your capture") is disposable, *result* messages (an answer, an error, a
state change) always take the floor. See ``_say`` for why steps are dropped
rather than queued.

Questions are asked with ``prompt_window.ask_question``, a card this app draws
itself; the platform's stock dialog toolkit was dropped because the user's
verdict on it was blunt, and because it could not match the bubble's look.

Threading
---------
Win32 windows are thread-affine: messages go to the thread that created the
window. Background workers therefore never touch the bubble directly - they
drop a payload in a lock-protected queue and post ``WM_BUBBLE_REQUEST`` to the
widget's window, and ``_wndproc`` does the actual work on the UI thread.
Calling ``Bubble.show`` from a worker produces a bubble that appears but never
responds to a click, because its window has no message pump on that thread.
``prompt_window.ask_question`` is the mirror image: it is blocking and creates
*its own* window, so it must be called from a worker and never from
``_wndproc``.
"""

from __future__ import annotations

import ctypes
import json
import random
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, ImageDraw

from . import notify
from .assistant_actions import (
    build_screen_reading_prompt,
    build_screen_search_prompt,
    capture_screen,
    listen_and_transcribe,
    load_assistant_settings,
    ocr_image,
    run_prompt_in_background,
    run_research_in_background,
    save_assistant_settings,
    search_from_screen_text,
    search_from_text,
)
from .bubble import Bubble, BubbleContent, BubbleLink
from .eye_tracker import get_eye_tracker
from .prompt_window import ask_question
from .screen_watcher import INTERVAL_CHOICES_MINUTES, ScreenWatcher, WatchResult

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
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
WM_DESTROY = 0x0002
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_MOUSEMOVE = 0x0200
WM_TIMER = 0x0113
WM_COMMAND = 0x0111
#: Sent when the resolution / monitor layout changes, and when a system-wide
#: setting does - the taskbar moving, resizing or switching to auto-hide
#: arrives as WM_SETTINGCHANGE with SPI_SETWORKAREA. Both invalidate the floor
#: the character is standing on, hence the re-clamp in ``_wndproc``.
WM_DISPLAYCHANGE = 0x007E
WM_SETTINGCHANGE = 0x001A
WM_APP = 0x8000

#: ``SetWindowPos`` flags. NOACTIVATE is not optional anywhere it appears
#: below: activating this window would pull focus out of whatever the user is
#: typing into, and the topmost re-assertion runs once a second.
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1

#: ``MonitorFromWindow`` / ``MonitorFromPoint``: never fail, fall back to the
#: monitor closest to the window or point. The alternative flags can return
#: NULL, which would leave the clamp with no work area to aim at.
MONITOR_DEFAULTTONEAREST = 0x00000002

#: Virtual-screen metrics, used only if the monitor query fails outright.
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

#: Posted (or sent) by background workers to make the UI thread act on a
#: payload they queued - showing a bubble, hiding it before a capture, playing
#: a clip. See the module docstring: doing any of that from the worker itself
#: leaves a window with no message pump behind it. WM_APP+2 is taken by
#: notify.WM_TRAYICON, hence +1 here.
WM_BUBBLE_REQUEST = WM_APP + 1

TPM_RIGHTBUTTON = 0x0002
MF_STRING = 0x0000
MF_UNCHECKED = 0x0000
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0800

#: Context-menu command ids. ``ID_CLOSE_MASCOT`` is part of this module's
#: public surface (the lifecycle test drives WM_COMMAND with it); the rest are
#: distinct so an unknown id can be told apart and ignored.
ID_CLOSE_MASCOT = 1001
ID_TOGGLE_ASSISTANT = 1002
ID_READ_SCREEN_NOW = 1003
#: One id per entry of ``INTERVAL_CHOICES_MINUTES``, offset from this base.
ID_INTERVAL_BASE = 1100

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
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
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
user32.ShowWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.LoadCursorW.restype = HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
# HMONITOR is a handle: without an explicit restype ctypes truncates it to a
# 32-bit int and GetMonitorInfoW is then handed a bogus monitor.
user32.MonitorFromWindow.restype = HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromPoint.restype = HANDLE
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]  # POINT by value
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [HANDLE, ctypes.c_void_p]
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


class _MONITORINFO(ctypes.Structure):
    """``GetMonitorInfoW`` output. ``rcWork`` is the taskbar-free rectangle.

    ``rcMonitor`` is the whole screen and ``rcWork`` is what is left once the
    taskbar and any other appbar has taken its strip. The companion is clamped
    to ``rcWork`` so it can never be dragged under the taskbar and stranded
    there - and per-monitor, which is the whole reason this structure is used
    instead of ``SystemParametersInfo(SPI_GETWORKAREA)``.
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
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

#: On-screen side of the sprite, in physical pixels. The *upscale factor* is
#: derived from it and from the sheet's own ``logical_size``
#: (``_SPRITE_TARGET_PX // logical_size``), so the character keeps the same size
#: on the desktop whether it is authored on a 32x32 grid (x4) or a 64x64 one
#: (x2). Writing the factor down instead would mean editing it - and the eye
#: maths, and the geometry - every time the art changes grid.
_SPRITE_TARGET_PX = 128

#: The variants of a character. ``plain`` is the normal look; ``watch`` is worn
#: while research-assistant mode reads the screen (the researcher shows a
#: thought bubble with a flask; the older slime wore a hat).
_VARIANT_PLAIN = "plain"
_VARIANT_WATCH = "watch"

#: Character sets in preference order: the first one whose files are all
#: present wins. The 8-bit researcher is the character the app ships; the slime
#: stays behind it so the widget still starts while the researcher sheets are
#: being (re)generated, instead of dying at import with a missing-file error.
#: The ``*_strip.png`` sheets are deliberately absent from both: their eyes are
#: baked in for the web avatar, and this widget draws its own.
#:
#: ``name`` doubles as the theme id published by the interface
#: (``/api/v1/theme``), which is what lets the companion on the desktop change
#: character with the page. MATh and CODy ship no dedicated ``watch`` sheet
#: yet; the loader falls back to their plain one rather than refusing them, so
#: research-assistant mode stays available for every character.
_CHARACTER_SETS: tuple[dict[str, Any], ...] = (
    {
        "name": "researcher",
        "metadata": "pixel_researcher.json",
        "sheets": {
            _VARIANT_PLAIN: "pixel_researcher_base.png",
            _VARIANT_WATCH: "pixel_researcher_watch_base.png",
        },
    },
    {
        "name": "pixelbit",
        "metadata": "pixel_pixelbit.json",
        "sheets": {
            _VARIANT_PLAIN: "pixel_pixelbit_base.png",
            _VARIANT_WATCH: "pixel_pixelbit_watch_base.png",
        },
    },
    {
        "name": "cody",
        "metadata": "pixel_cody.json",
        "sheets": {
            _VARIANT_PLAIN: "pixel_cody_base.png",
            _VARIANT_WATCH: "pixel_cody_watch_base.png",
        },
    },
    {
        "name": "slime",
        "metadata": "pixel_slime.json",
        "sheets": {
            _VARIANT_PLAIN: "pixel_slime_base.png",
            _VARIANT_WATCH: "pixel_slime_base_hat.png",
        },
    },
)

#: How often the companion asks the local server which character is on screen.
#: Slow on purpose: this is a cosmetic preference, not a control channel, and
#: the poll must stay invisible next to the 33ms redraw timer.
_THEME_POLL_SECONDS = 1.5

_CLIP_IDLE = "idle"
_CLIP_HOP = "hop"
_CLIP_VANISH = "vanish"
_CLIP_RETURN = "return"

#: Spontaneous hop cadence at rest, in seconds. Explicitly asked for: the
#: bounce has to be recurring, not just a reaction to a click.
_HOP_INTERVAL = (5.0, 9.0)
#: Blink cadence and how long the eyes stay shut, in seconds.
_BLINK_INTERVAL = (3.0, 6.0)
_BLINK_DURATION = 0.12

#: Cursor distance, in screen pixels, at which the eye deflection saturates.
#: Screen-space, so it is independent of the logical grid. The deflection
#: *limits* are not constants: they are derived from the eye size published by
#: the sheet (see ``_look_limits``), because "two logical pixels" means half an
#: eye on a 32x32 grid and a quarter of one on a 64x64 grid.
_LOOK_SATURATION_PX = 220

#: Redraws needed for the icon column to fade fully in or out (~0.5s at 33ms).
_ICON_FADE_STEPS = 15.0

_ICON_LOGICAL = 16
_ICON_SCALE = 2
_ICON_SIZE = _ICON_LOGICAL * _ICON_SCALE
_ICON_GAP = 8
_ICON_ORDER = ("mic", "ocr", "web", _VARIANT_WATCH)

# Icon colours mirror the character's palette (outline / light / pale / mid) so
# the controls read as part of the same character rather than as system chrome.
_ICON_PAD = (34, 22, 62, 236)
_ICON_BORDER = (167, 139, 250, 255)
_ICON_INK = (240, 236, 255, 255)
#: Active state for the watch icon, so "the assistant is running" is visible
#: without opening the menu: the pill inverts to accent-on-dark-ink.
_ICON_PAD_ACTIVE = (131, 87, 241, 244)
_ICON_BORDER_ACTIVE = (231, 224, 255, 255)
_ICON_INK_ACTIVE = (26, 16, 48, 255)

#: 16x16 logical glyphs, '#' = ink. Authored as pixel maps rather than with
#: vector primitives because at this size a single misplaced pixel is the
#: difference between a legible globe and a smudge - the same reason the
#: character itself is authored on a 32x32 grid.
_GLYPHS: dict[str, tuple[str, ...]] = {
    # Microphone: capsule, cradle, stem, base.
    "mic": (
        "................",
        "................",
        "................",
        "......####......",
        ".....######.....",
        ".....######.....",
        ".....######.....",
        ".....######.....",
        "......####......",
        "....#......#....",
        "....#......#....",
        ".....#....#.....",
        "......####......",
        ".......##.......",
        "....########....",
        "................",
    ),
    # Magnifying glass: the screen-reading (OCR) scan.
    "ocr": (
        "................",
        "................",
        ".....####.......",
        "....#....#......",
        "...#......#.....",
        "...#......#.....",
        "...#......#.....",
        "....#....#......",
        ".....####.......",
        "........##......",
        ".........##.....",
        "..........##....",
        "...........##...",
        "................",
        "................",
        "................",
    ),
    # Globe: ring + meridian + parallel, the standard "search the internet"
    # glyph, and the one the user asked for by name.
    "web": (
        "................",
        "................",
        "......####......",
        "....###..###....",
        "...#..#..#..#...",
        "...#.#....#.#...",
        "..#..#....#..#..",
        "..############..",
        "..#..#....#..#..",
        "..#..#....#..#..",
        "...#.#....#.#...",
        "...#..#..#..#...",
        "....###..###....",
        "......####......",
        "................",
        "................",
    ),
    # Conical flask: research-assistant mode, i.e. the character watching the
    # screen. It mirrors the flask the "watch" sheet floats in the character's
    # thought bubble, so the icon and the sprite say the same thing. (It
    # replaces the hat the slime used to wear for this mode; a flask also reads
    # differently enough from the OCR magnifier not to be mistaken for it.)
    _VARIANT_WATCH: (
        "................",
        "................",
        "................",
        ".....######.....",
        "......#..#......",
        "......#..#......",
        "......#..#......",
        ".....#....#.....",
        "....#......#....",
        "...#........#...",
        "...#.######.#...",
        "...#.######.#...",
        "...##########...",
        "................",
        "................",
        "................",
    ),
}

#: Exact sentence the companion says when it comes back with a result. Quoted
#: verbatim from the request, so it lives in one place.
_RESULT_READY_SENTENCE = "Clique sur moi pour voir le résultat"

#: How long the companion waits for a research run before coming back empty
#: handed. It must never stay invisible: while the main window is closed, the
#: mascot is the only way back into the app. Matches the HTTP timeout used by
#: ``run_research_in_background``; whichever fires first wins (see
#: ``_research_resolved``).
_RESEARCH_TIMEOUT_SECONDS = 180.0

#: How often the window re-claims the top of the z-order, in seconds.
#: ``WS_EX_TOPMOST`` is set at creation but does not stick: another topmost
#: window, a full-screen app or a virtual-desktop switch pushes the companion
#: behind them for good. One second is slow enough to be free (roughly one
#: ``SetWindowPos`` every 30 redraws at 33 ms) and fast enough that the user
#: never notices the dip.
_TOPMOST_REASSERT_SECONDS = 1.0

#: Two levels of speech, and the reason ``_say`` needs a priority at all:
#:
#: * ``_SAY_STEP`` is progress chatter ("I'm reading your capture"). It is
#:   disposable - the state it describes is already over by the time anyone
#:   could read a queued copy of it.
#: * ``_SAY_RESULT`` is an outcome: an answer, an error, a mode change. It is
#:   the only thing the user asked for, so it always takes the floor.
#:
#: A step message is *dropped* rather than shown when a result is already on
#: screen, because a bubble is a single window: showing the step would replace
#: the result mid-sentence, and the result bubble is sometimes the only handle
#: on a payload (see ``_RESULT_READY_SENTENCE``). Dropping it loses nothing;
#: queueing it would show stale news later, and stacking bubbles would need a
#: second window and a layout manager for a line of text.
_SAY_STEP = 0
_SAY_RESULT = 1

#: How often the mascot asks the eye tracker whether the gaze is stuck. The
#: redraw timer runs at ~30 fps, and polling a lock-protected dataclass that
#: often would be pure waste for a signal that changes on the order of
#: seconds.
_GAZE_CHECK_SECONDS = 1.0

#: Quiet period after the user dismisses a help offer. Matches the intent
#: stated for the idle-based proposal: refusing once must buy real silence,
#: not a fresh prompt as soon as the gaze settles again.
_GAZE_OFFER_COOLDOWN_SECONDS = 20 * 60.0

#: Model answers land in a bubble, and a bubble has no scroll area: its height
#: is measured from its content (see ``bubble.py``), so an unbounded answer
#: would draw a card taller than the screen. Long answers are cut here and the
#: full text stays in the main window's conversation.
_ANSWER_EXCERPT_CHARS = 420

# Queued UI actions, drained by ``_wndproc`` on WM_BUBBLE_REQUEST.
_UI_SHOW_BUBBLE = 1
_UI_HIDE_BUBBLE = 2
_UI_PLAY_VANISH = 3
_UI_RESEARCH_DONE = 4


def _assets_dir() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / "web" / "assets"


def _character_is_usable(character: dict[str, Any]) -> bool:
    """Whether a character can be drawn: its JSON and its plain sheet exist.

    The ``watch`` sheet is explicitly *not* required. It is an optional second
    look, and demanding it would silently disqualify a character whose art is
    otherwise complete - which is how MATh and CODy would have been skipped.
    """

    assets = _assets_dir()
    sheets = dict(character["sheets"])
    required = [character["metadata"], sheets[_VARIANT_PLAIN]]
    return all((assets / name).is_file() for name in required)


def _resolve_character_set(preferred: str | None = None) -> dict[str, Any]:
    """Pick a character by name, else the first usable one on disk.

    ``preferred`` is the theme id published by the interface. An unknown or
    incomplete name falls back to the normal order instead of failing: the
    companion must keep running whatever the page asked for.

    The researcher comes first and the slime last (see ``_CHARACTER_SETS``):
    the widget must keep starting while the new sheets are being generated, and
    a missing PNG would otherwise raise inside ``__init__`` - which is also
    where the lifecycle tests construct the widget with no window at all.

    When nothing is complete, the *preferred* set is returned anyway so the
    failure that follows names the character the app actually wants, instead of
    a fallback nobody asked for.
    """

    wanted = str(preferred or "").strip().lower()
    if wanted:
        for character in _CHARACTER_SETS:
            if character["name"] == wanted and _character_is_usable(character):
                return character
    for character in _CHARACTER_SETS:
        if _character_is_usable(character):
            return character
    return _CHARACTER_SETS[0]


def _load_sprite_metadata(filename: str) -> dict[str, Any]:
    """Read a character's JSON: clips, per-frame eye anchors, ground line, palette.

    The generator writes it from the same constants it draws with, so the
    anchors here cannot drift away from the artwork. Everything grid-dependent
    (``logical_size``, ``frame_count``, clip bounds, ``ground_y``) is taken from
    this file rather than assumed, which is what makes a change of grid a
    no-op here.
    """

    return json.loads((_assets_dir() / filename).read_text(encoding="utf-8"))


def _premultiplied_bgra(image: Image.Image) -> np.ndarray:
    """Straight RGBA image -> premultiplied BGRA array, the DIB's own layout.

    ``UpdateLayeredWindow`` with ``AC_SRC_ALPHA`` expects colour channels
    already multiplied by alpha, in BGRA order, so doing it once at load time
    turns each redraw into a plain copy.
    """

    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint16)
    alpha = rgba[..., 3]
    # Integer maths with rounding: floats here would cost a second full-size
    # buffer per frame for a result that has to be uint8 anyway.
    premultiplied = ((rgba[..., :3] * alpha[..., None] + 127) // 255).astype(np.uint8)
    return np.dstack(
        [premultiplied[..., 2], premultiplied[..., 1], premultiplied[..., 0], alpha.astype(np.uint8)]
    )


def _pixel_scale(logical: int) -> int:
    """Integer upscale factor for a ``logical``x``logical`` cell.

    Derived, never written down: a 32px grid gives x4 and a 64px grid gives x2,
    both landing on ``_SPRITE_TARGET_PX`` on screen. Floor division keeps it an
    integer even for a grid that does not divide the target - the sprite then
    comes out slightly smaller rather than blurred, which is the right trade
    for pixel art.
    """

    return max(1, _SPRITE_TARGET_PX // max(1, int(logical)))


def _load_frame_strip(path: Path, *, frame_count: int, logical: int, scale: int) -> list[np.ndarray]:
    """Slice a horizontal sheet into premultiplied BGRA frames, upscaled x``scale``.

    ``Image.NEAREST`` with an integer factor is not a preference here: any
    smooth filter blurs the 1px outline into a halo, and a fractional factor
    makes neighbouring source pixels different sizes on screen. Both destroy
    the art the generator carefully aligned to its logical grid.

    ``frame_count`` and ``logical`` come from the sheet's JSON, and the sheet's
    own dimensions are checked against them: a sheet and a metadata file that
    disagree would otherwise slice frames at the wrong offsets and show half a
    character.
    """

    sheet = Image.open(path).convert("RGBA")
    expected = (logical * frame_count, logical)
    if sheet.size != expected:
        raise RuntimeError(f"{path.name}: attendu {expected}, trouve {sheet.size}")
    side = logical * scale
    frames: list[np.ndarray] = []
    for index in range(frame_count):
        cell = sheet.crop((index * logical, 0, (index + 1) * logical, logical))
        frames.append(_premultiplied_bgra(cell.resize((side, side), Image.NEAREST)))
    return frames


def _opaque_bgra(rgba: Sequence[int]) -> np.ndarray:
    """Palette entry -> a single BGRA pixel, ready to assign into the canvas.

    Only used for the eye and its glint, which are fully opaque: premultiplying
    an opaque colour is the identity, so no conversion is needed.
    """

    r, g, b, a = (int(value) for value in rgba)
    return np.array([b, g, r, a], dtype=np.uint8)


def _build_icon(kind: str, *, active: bool = False) -> Image.Image:
    """Draw one action icon as pixel art: 16x16 logical, then x2 NEAREST.

    Same discipline as the character (integer factor, nearest neighbour), so
    the icon column and the character share one pixel grid. A rounded dark pill
    with a 1px accent border keeps the glyph readable over any wallpaper.
    """

    glyph = _GLYPHS[kind]
    tile = Image.new("RGBA", (_ICON_LOGICAL, _ICON_LOGICAL), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle(
        (0, 0, _ICON_LOGICAL - 1, _ICON_LOGICAL - 1),
        radius=5,
        fill=_ICON_PAD_ACTIVE if active else _ICON_PAD,
        outline=_ICON_BORDER_ACTIVE if active else _ICON_BORDER,
        width=1,
    )
    pixels = tile.load()
    ink = _ICON_INK_ACTIVE if active else _ICON_INK
    for y, row in enumerate(glyph):
        for x, cell in enumerate(row):
            if cell == "#":
                pixels[x, y] = ink
    return tile.resize((_ICON_SIZE, _ICON_SIZE), Image.NEAREST)


def canvas_to_image(canvas: np.ndarray) -> Image.Image:
    """Premultiplied BGRA canvas -> straight RGBA image.

    The inverse of ``_premultiplied_bgra``, kept public because it is the only
    way to look at what ``_compose_canvas`` produced without a window on
    screen - which is how the sprite, the eyes and every clip are checked.
    """

    bgra = np.asarray(canvas, dtype=np.float64)
    alpha = bgra[..., 3:4]
    scale = np.where(alpha > 0, 255.0 / np.maximum(alpha, 1e-6), 0.0)
    straight = (bgra[..., :3] * scale).clip(0, 255).astype(np.uint8)
    rgba = np.dstack([straight[..., 2], straight[..., 1], straight[..., 0], alpha[..., 0].astype(np.uint8)])
    return Image.fromarray(rgba, "RGBA")


def _look_offset(delta_px: int, limit: int, saturation: int = _LOOK_SATURATION_PX) -> int:
    """Screen-space cursor offset -> bounded eye offset in logical pixels."""

    ratio = max(-1.0, min(1.0, delta_px / float(saturation)))
    return int(round(ratio * limit))


def _domain_of(url: str) -> str:
    """Host of a URL, without ``www.``, for a bubble link's source line."""

    try:
        host = urlsplit(str(url or "")).netloc
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _work_area(hwnd: wintypes.HWND | None, probe: tuple[int, int]) -> tuple[int, int, int, int]:
    """Work area (left, top, right, bottom) of the monitor concerned, taskbar excluded.

    "The monitor concerned" is the one the window is on
    (``MonitorFromWindow``), or - when there is no window yet, at first
    placement or in a headless check - the one nearest ``probe``, the position
    being asked about (``MonitorFromPoint``). Both use
    ``MONITOR_DEFAULTTONEAREST`` so they cannot return NULL and leave the caller
    with no rectangle to clamp against.

    ``SystemParametersInfo(SPI_GETWORKAREA)`` would be one call instead of two,
    and wrong: it only ever reports the *primary* monitor's work area, so a
    companion living on a second screen would be clamped against a taskbar that
    is not there.

    The virtual-screen fallback at the end is for the case where the monitor
    query itself fails: it includes the taskbar, which is worse, but a floor in
    the wrong place still beats no floor at all.
    """

    monitor = None
    if hwnd:
        monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not monitor:
        monitor = user32.MonitorFromPoint(
            wintypes.POINT(int(probe[0]), int(probe[1])), MONITOR_DEFAULTTONEAREST
        )
    if monitor:
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            return (int(work.left), int(work.top), int(work.right), int(work.bottom))

    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    return (
        int(left),
        int(top),
        int(left + user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(top + user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


#: Spawn margin from the work-area edge: comfortably clear of the ~128px
#: sprite so it lands fully on screen rather than clipped at the corner.
_DEFAULT_SPAWN_MARGIN = 170


def _default_spawn_position() -> tuple[int, int]:
    """Bottom-right corner of the primary monitor's work area.

    Previously a fixed (60, 60) - top-left - which is exactly where this
    app's own sidebar lives, so the companion spawned directly on top of
    the app's own UI on every launch. Bottom-right keeps it clear of both
    the sidebar (left edge) and the taskbar (excluded from the work area)
    while remaining freely draggable anywhere afterwards.
    """

    left, top, right, bottom = _work_area(None, (0, 0))
    return (
        max(left, right - _DEFAULT_SPAWN_MARGIN),
        max(top, bottom - _DEFAULT_SPAWN_MARGIN),
    )


@dataclass(frozen=True)
class _Clip:
    """One animation clip, straight out of the sheet's metadata."""

    name: str
    start: int
    count: int
    frame_ms: int
    loop: bool

    @property
    def duration(self) -> float:
        return self.count * self.frame_ms / 1000.0


class NativeWidget:
    """A tiny always-on-top, draggable, transparent desktop companion."""

    def __init__(
        self,
        on_click: Callable[[], None],
        *,
        x: int | None = None,
        y: int | None = None,
        port: int | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._on_click = on_click
        self._on_close = on_close
        self._port = port
        if x is None or y is None:
            default_x, default_y = _default_spawn_position()
            x = default_x if x is None else x
            y = default_y if y is None else y
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
        self._wndproc_ref = WNDPROC(self._wndproc)

        # -- artwork. Loaded here, in __init__, deliberately: the widget must be
        # constructible with no window and no message loop (that is what the
        # lifecycle test relies on), so nothing about the sprite may depend on
        # Win32 state. It all goes through _apply_character because the same
        # work runs again when the interface switches character.
        self._apply_character(_resolve_character_set())
        self._icon_opacity = 0.0
        #: Character asked for by the page but not yet swapped in. Written by
        #: the polling thread, consumed on the UI thread: reloading sheets under
        #: a redraw would tear the frame being composited.
        self._pending_character: str | None = None
        self._theme_poll_stop = threading.Event()
        self._theme_thread: threading.Thread | None = None
        self._init_session_state()

    def _apply_character(self, character: dict[str, Any]) -> None:
        """Load one character's sheets, clips, palette and geometry.

        Called from ``__init__`` and again on a theme change. Every derived
        value is recomputed from the sheet's own JSON, so a character authored
        on another grid stays correct without touching this code.
        """

        self._character = character
        self._meta = _load_sprite_metadata(str(self._character["metadata"]))
        self._logical = int(self._meta["logical_size"])
        # Derived, not written down: 64 -> x2, 32 -> x4, both 128px on screen.
        self._scale = _pixel_scale(self._logical)
        self._frame_meta: list[dict[str, Any]] = list(self._meta["frames"])
        frame_count = int(self._meta["frame_count"])
        if len(self._frame_meta) != frame_count:
            # The per-frame table is indexed by clip offsets straight from the
            # same file; a mismatch means an IndexError mid-animation instead of
            # a clear error at startup.
            raise RuntimeError(
                f"{self._character['metadata']}: frame_count={frame_count} mais "
                f"{len(self._frame_meta)} frame(s) decrites"
            )
        # A sheet that is not on disk is skipped rather than fatal, and the
        # missing variant then aliases the plain one. That keeps
        # research-assistant mode working for a character that only ships one
        # look, instead of raising several seconds into a mode change.
        sheets = dict(self._character["sheets"])
        self._frames = {
            variant: _load_frame_strip(
                _assets_dir() / filename,
                frame_count=frame_count,
                logical=self._logical,
                scale=self._scale,
            )
            for variant, filename in sheets.items()
            if (_assets_dir() / filename).is_file()
        }
        if _VARIANT_PLAIN not in self._frames:
            raise RuntimeError(
                f"{self._character['name']}: planche '{sheets[_VARIANT_PLAIN]}' introuvable"
            )
        for variant in sheets:
            self._frames.setdefault(variant, self._frames[_VARIANT_PLAIN])
        self._clips = {
            name: _Clip(
                name=name,
                start=int(spec["start"]),
                count=int(spec["count"]),
                frame_ms=int(spec["frame_ms"]),
                loop=bool(spec["loop"]),
            )
            for name, spec in self._meta["clips"].items()
        }
        for clip in self._clips.values():
            # Clip bounds are read, never assumed - and checked, because a clip
            # running past the end of the sheet is an IndexError several seconds
            # into an animation rather than at startup.
            if clip.start < 0 or clip.count < 1 or clip.start + clip.count > frame_count:
                raise RuntimeError(
                    f"{self._character['metadata']}: clip '{clip.name}' sort de la bande "
                    f"({clip.start}+{clip.count} > {frame_count})"
                )
        palette = self._meta["palette"]
        self._eye_bgra = _opaque_bgra(palette["eye"])
        self._glint_bgra = _opaque_bgra(palette["glint"])
        self._look_max = self._look_limits()

        # -- geometry: the canvas holds the sprite and the icon column side by
        # side. The sheet has transparent rows below ``ground_y``; reserve the
        # same slack below the controls so their last button cannot sink into
        # the taskbar while the character's feet remain exactly on its edge.
        self._sprite_w = self._sprite_h = self._logical * self._scale
        icons_h = len(_ICON_ORDER) * _ICON_SIZE + (len(_ICON_ORDER) - 1) * _ICON_GAP
        self._canvas_w = self._sprite_w + _ICON_GAP + _ICON_SIZE
        ground = self._meta.get("ground_y")
        sprite_ground_slack = (
            0
            if ground is None
            else max(
                0,
                min(
                    self._sprite_h,
                    self._sprite_h - int(round(float(ground) * self._scale)),
                ),
            )
        )
        self._canvas_h = max(self._sprite_h, icons_h + sprite_ground_slack)
        # Sprite anchored at the bottom of its column so the ground line stays
        # put whatever the icon column does.
        self._sprite_origin = (0, self._canvas_h - self._sprite_h)
        self._feet_slack = self._compute_feet_slack()

        icon_x = self._sprite_w + _ICON_GAP
        controls_floor = self._canvas_h - self._feet_slack
        icons_y0 = max(0, (controls_floor - icons_h) // 2)
        self._icon_rects = {
            name: (
                icon_x,
                icons_y0 + index * (_ICON_SIZE + _ICON_GAP),
                icon_x + _ICON_SIZE,
                icons_y0 + index * (_ICON_SIZE + _ICON_GAP) + _ICON_SIZE,
            )
            for index, name in enumerate(_ICON_ORDER)
        }
        self._icon_images = {name: _premultiplied_bgra(_build_icon(name)) for name in _ICON_ORDER}
        self._icon_images_active = {
            _VARIANT_WATCH: _premultiplied_bgra(_build_icon(_VARIANT_WATCH, active=True))
        }

    def _init_session_state(self) -> None:
        """Animation clock, research mode and speech-bubble plumbing.

        Deliberately separate from ``_apply_character``: swapping character
        mid-session must keep the running animation, the assistant mode and the
        bubble exactly as they were, and only replace the artwork.
        """

        # -- animation state machine, driven by time.monotonic (a wall-clock
        # step backwards must not freeze or fast-forward the character).
        now = time.monotonic()
        self._clip_name = _CLIP_IDLE
        self._clip_frame = 0
        self._clip_started_at = now
        self._clip_finished = False
        self._hop_requested = False
        self._next_hop_at = now + random.uniform(*_HOP_INTERVAL)
        self._next_blink_at = now + random.uniform(*_BLINK_INTERVAL)
        self._look = (0, 0)
        #: Next topmost re-assertion. Kept on the same monotonic clock as the
        #: animation, and *not* done every frame: see ``_reassert_topmost``.
        self._next_topmost_at = now + _TOPMOST_REASSERT_SECONDS

        # -- research-assistant mode. Off unless the user turned it on before:
        # it reads the screen, so it stays an explicit choice (see
        # load_assistant_settings).
        settings = load_assistant_settings()
        self._assistant_enabled = bool(settings["enabled"])
        self._interval_minutes = int(settings["interval_minutes"])
        self._watcher: ScreenWatcher | None = None
        self._capturing = False

        # -- speech bubble and the marshalling slot feeding it. The bubble's
        # window is created lazily on whichever thread first shows it, which is
        # why every path below goes through the queue.
        self._bubble = Bubble()
        self._lock = threading.Lock()
        self._ui_queue: list[tuple[int, Any]] = []
        self._pending_result: list[dict[str, str]] | None = None
        self._research_question = ""
        self._research_resolved = True
        self._research_timer: threading.Timer | None = None
        self._return_payload: tuple[list[dict[str, str]], bool] | None = None
        self._window_hidden = False
        #: True while the bubble currently on screen carries an outcome rather
        #: than progress chatter. Read on the UI thread only, where the bubble
        #: lives, so it cannot disagree with what is actually displayed.
        self._bubble_is_result = False

        # -- gaze-blocked help offer. The eye tracker is a singleton living in
        # this same process (three_loop/eye_tracker.py), so the mascot reads
        # its state directly rather than through the HTTP API. Nothing here
        # starts the tracker or touches the camera: if the user never turned
        # tracking on, `event_seq` simply never moves and this stays inert.
        self._last_gaze_event = 0
        self._next_gaze_check_at = now + _GAZE_CHECK_SECONDS
        #: Refusing an offer must buy real quiet, otherwise a user who is
        #: concentrating on one spot gets nagged every few seconds by the very
        #: feature meant to help them.
        self._gaze_offer_muted_until = 0.0
        #: True while a help offer is on screen waiting to be accepted, so a
        #: click on the character opens the question card instead of doing
        #: what a body click normally does (bring the main window up).
        self._gaze_offer_pending = False

    def start(self) -> None:
        self._thread.start()
        # Only worth polling when there is a server to ask. Without a port the
        # companion simply keeps the character it resolved at startup.
        if self._port is not None:
            self._theme_thread = threading.Thread(target=self._poll_theme, daemon=True)
            self._theme_thread.start()

    # -- character following the interface -------------------------------

    def _poll_theme(self) -> None:
        """Ask the local server which character the interface is showing.

        A background thread rather than the message loop: a stalled socket must
        never freeze the animation. Only the resulting *name* is handed over,
        and the reload itself happens on the UI thread.
        """

        url = f"http://127.0.0.1:{self._port}/api/v1/theme"
        while not self._theme_poll_stop.wait(_THEME_POLL_SECONDS):
            try:
                with urllib.request.urlopen(url, timeout=1.5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                # The page may not have loaded yet, or the server may be gone.
                # Neither is worth reporting: the character stays as it is.
                continue
            name = str(payload.get("theme") or "").strip().lower()
            if name and name != str(self._character["name"]):
                self._pending_character = name

    def _consume_pending_character(self) -> None:
        """Swap the artwork on the UI thread, if the page asked for another.

        Runs just before a redraw so the sheets can never be replaced while a
        frame is being composited. Position and animation state are preserved:
        only the drawing changes, and a character whose art is missing is
        ignored rather than crashing the companion.
        """

        name, self._pending_character = self._pending_character, None
        if not name or name == str(self._character["name"]):
            return
        character = _resolve_character_set(name)
        if str(character["name"]) != name:
            # Unknown or incomplete: keep what is on screen and stop retrying.
            return
        try:
            self._apply_character(character)
        except (OSError, ValueError, KeyError, RuntimeError):
            return
        hwnd = self._hwnd
        if hwnd:
            # The canvas is derived from the sheet, so a character on another
            # grid would need a different window size. Same size today; resized
            # anyway so that stays true without another edit here.
            user32.SetWindowPos(
                hwnd,
                None,
                self._x,
                self._y,
                self._canvas_w + 4,
                self._canvas_h + 4,
                SWP_NOZORDER | SWP_NOACTIVATE,
            )

    # -- derived geometry ------------------------------------------------

    def _look_limits(self) -> tuple[int, int]:
        """Maximum eye deflection, in logical pixels, derived from the eye size.

        Half an eye horizontally and a quarter vertically: enough to read as a
        glance, not enough to push the pupil out of its socket. Derived from the
        biggest eye the sheet declares (frames whose ``eyes`` is ``null`` carry a
        placeholder size, so they are skipped) rather than hard-coded, because a
        fixed "2 logical pixels" is half an eye on a 32x32 grid and a quarter of
        one on a 64x64 grid.
        """

        sizes = [
            (int(frame["eye_size"][0]), int(frame["eye_size"][1]))
            for frame in self._frame_meta
            if frame.get("eyes") and frame.get("eye_size")
        ]
        if not sizes:
            return (1, 1)
        return (max(1, max(w for w, _h in sizes) // 2), max(1, max(h for _w, h in sizes) // 4))

    def _compute_feet_slack(self) -> int:
        """Canvas pixels between the character's feet and the canvas bottom.

        This is what the work-area clamp actually uses: the sprite cell carries
        a ground shadow and some empty rows below the feet, and the canvas is
        taller still when the icon column is (the sprite is bottom-anchored, so
        that surplus is above, not below). Clamping the canvas bottom to the
        taskbar would therefore park the character a dozen pixels too high, with
        a visible gap underneath.

        ``ground_y`` is the ground line in *logical* pixels, published by the
        generator from the same constant it draws the shadow with. When a sheet
        does not publish one - the older slime metadata has no ``ground_y`` - the
        slack is zero, i.e. the clamp falls back to the bottom of the canvas.
        That is the safe direction: the character stays fully visible, just not
        flush with the taskbar.
        """

        ground = self._meta.get("ground_y")
        if ground is None:
            return 0
        _origin_x, origin_y = self._sprite_origin
        feet_on_canvas = origin_y + int(round(float(ground) * self._scale))
        return max(0, min(self._canvas_h, self._canvas_h - feet_on_canvas))

    def _feet_y(self, window_y: int) -> int:
        """Screen y of the character's feet for a window placed at ``window_y``."""

        return window_y + self._canvas_h - self._feet_slack

    def _clamp_to_work_area(
        self, x: int, y: int, *, hwnd: wintypes.HWND | None = None
    ) -> tuple[int, int]:
        """Nearest position that keeps the whole widget inside the work area.

        The floor is the work area's bottom edge *under the character's feet*
        (see ``_compute_feet_slack``), so the sprite ends up standing on the
        taskbar instead of sinking behind it - which is how a companion dragged
        too low used to become unreachable.

        Left, right and top are clamped too: nothing about a companion parked
        half off-screen is useful, and the icon column lives on the right of the
        canvas, so clamping the canvas width is what keeps the icons clickable.

        The size used is the canvas, not the rectangle passed to
        ``CreateWindowExW``: ``UpdateLayeredWindow`` resizes the window to the
        surface it is handed, so the canvas is the window's real size once the
        first redraw has run.

        Degenerate work areas (smaller than the widget) fall back to the top-left
        corner rather than producing a negative range, because ``min`` is applied
        before ``max``.
        """

        left, top, right, bottom = _work_area(self._hwnd if hwnd is None else hwnd, (int(x), int(y)))
        max_x = right - self._canvas_w
        # y + canvas_h - feet_slack <= bottom  <=>  y <= bottom - canvas_h + slack
        max_y = bottom - self._canvas_h + self._feet_slack
        return (max(left, min(int(x), max_x)), max(top, min(int(y), max_y)))

    def _reclamp_position(self, hwnd: wintypes.HWND) -> None:
        """Pull the window back inside the work area after a layout change.

        Called on ``WM_DISPLAYCHANGE`` and ``WM_SETTINGCHANGE``: a resolution
        drop, a monitor unplugged or a taskbar that just got taller moves the
        floor, and a character that was correctly placed a second ago is now
        behind it. ``SetWindowPos`` is skipped when nothing moved, so the common
        case (settings changes have many other causes) costs two Win32 calls.
        """

        if not hwnd:
            return
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        x, y = self._clamp_to_work_area(rect.left, rect.top, hwnd=hwnd)
        if (x, y) == (rect.left, rect.top):
            return
        self._x, self._y = x, y
        user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)

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

        # First placement is clamped too, before the window exists: the caller's
        # coordinates come from a config file or a default, and neither knows
        # about this machine's screens or where its taskbar is.
        self._x, self._y = self._clamp_to_work_area(self._x, self._y)

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
        # Now that there is a window, ``MonitorFromWindow`` can be asked which
        # monitor it actually landed on - authoritative where the pre-creation
        # ``MonitorFromPoint`` was only a guess about the requested corner.
        self._reclamp_position(hwnd)
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetTimer(hwnd, 1, 33, None)
        self._redraw()

        # Restore a mode the user had left on. Done here rather than in
        # __init__ so nothing starts reading the screen for a widget that was
        # merely constructed.
        if self._assistant_enabled:
            self._start_watcher(announce=False)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    # -- pointer, eyes, animation ---------------------------------------

    def _update_pointer_state(self) -> None:
        """Refresh the hover flag and where the eyes should be looking.

        Replaces the old "flip the sprite toward the mouse" trick: the character
        is near enough symmetrical that mirroring it was invisible, and it cost a
        Python per-pixel loop 30 times a second. Moving the eyes is both cheaper
        and actually readable.
        """

        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        rect = wintypes.RECT()
        user32.GetWindowRect(self._hwnd, ctypes.byref(rect))

        was_hovering = self._hovering
        self._hovering = rect.left <= cursor.x <= rect.right and rect.top <= cursor.y <= rect.bottom
        if self._hovering and not was_hovering:
            self._hop_requested = True  # greet the pointer

        origin_x, origin_y = self._sprite_origin
        centre_x = rect.left + origin_x + self._sprite_w // 2
        centre_y = rect.top + origin_y + self._sprite_h // 2
        look_max_x, look_max_y = self._look_max
        self._look = (
            _look_offset(cursor.x - centre_x, look_max_x),
            _look_offset(cursor.y - centre_y, look_max_y),
        )

    def _advance_icon_fade(self) -> None:
        """Ease the icon column in and out over ~0.5s instead of snapping.

        It also stays up while a request is in flight, so "I am working on it"
        has a visible state that does not depend on where the pointer is.
        """

        target = 1.0 if (self._hovering or self._busy) else 0.0
        step = 1.0 / _ICON_FADE_STEPS
        if self._icon_opacity < target:
            self._icon_opacity = min(target, self._icon_opacity + step)
        elif self._icon_opacity > target:
            self._icon_opacity = max(target, self._icon_opacity - step)

    def _start_clip(self, name: str, *, now: float | None = None) -> None:
        """Begin a clip from its first frame. Clips are never stacked."""

        started = time.monotonic() if now is None else now
        self._clip_name = name
        self._clip_frame = 0
        self._clip_started_at = started
        self._clip_finished = False
        if name == _CLIP_HOP:
            # Measured from the end of the hop, so the pause between two
            # bounces is the interval that was asked for.
            self._next_hop_at = started + self._clips[name].duration + random.uniform(*_HOP_INTERVAL)

    def _advance_animation(self, now: float) -> None:
        """Pick the current frame from the clock, and fire end-of-clip work.

        Looping clips wrap; one-shot clips stop on their last frame and call
        ``_on_clip_finished`` exactly once, which is what chains vanish ->
        hidden -> return -> bubble.
        """

        clip = self._clips[self._clip_name]
        # Rounded to the millisecond before dividing: float subtraction of two
        # monotonic readings lands a hair under the exact boundary (0.15s reads
        # as 0.14999999999997726), which would drop the first frame of every
        # step and make a clip end one frame late.
        elapsed_ms = int(round((now - self._clip_started_at) * 1000.0))
        step = max(0, elapsed_ms // clip.frame_ms)
        if clip.loop:
            self._clip_frame = step % clip.count
            return
        if step < clip.count:
            self._clip_frame = step
            return
        self._clip_frame = clip.count - 1  # hold the last frame
        if self._clip_finished:
            return
        self._clip_finished = True
        self._on_clip_finished(clip.name)

    def _on_clip_finished(self, name: str) -> None:
        if name == _CLIP_HOP:
            self._start_clip(_CLIP_IDLE)
        elif name == _CLIP_VANISH:
            self._swallow_and_search()
        elif name == _CLIP_RETURN:
            self._start_clip(_CLIP_IDLE)
            self._announce_research()

    def _maybe_offer_gaze_help(self, now: float) -> None:
        """Offer help when the eye tracker reports a stuck gaze. UI thread.

        This is what the feature promised - "when the gaze stays in one place,
        LOUPe offers help" - and what was missing: the tracker computed the
        blocked state and the web page reacted to it, but only by focusing its
        own text field. If the user is stuck reading something else, that page
        is not even on screen, so nothing was ever offered.

        Deliberately edge-triggered on ``event_seq`` rather than on the state:
        ``blocked`` stays true for as long as the gaze holds, so reacting to
        the state would re-offer on every frame. The tracker bumps the counter
        once per blocked episode.
        """

        if now < self._next_gaze_check_at:
            return
        self._next_gaze_check_at = now + _GAZE_CHECK_SECONDS
        # Never talk over an ongoing task, a result waiting to be read, or a
        # user who just refused an offer.
        if self._busy or self._bubble_is_result or now < self._gaze_offer_muted_until:
            return
        try:
            status = get_eye_tracker().status()
        except Exception:
            return  # a status read must never be able to break the render loop
        event_seq = int(status.get("event_seq") or 0)
        if event_seq <= self._last_gaze_event or not status.get("help_requested"):
            return
        self._last_gaze_event = event_seq
        self._gaze_offer_pending = True
        self._show_bubble(
            BubbleContent(
                title="Besoin d'un coup de main ?",
                lines=(
                    "Ton regard reste au même endroit depuis un moment.",
                    "Clique-moi et dis-moi ce qui bloque.",
                ),
                # No timeout: an offer that vanishes on its own is one the user
                # cannot accept. It goes away when clicked, or when the next
                # bubble replaces it.
                timeout_s=None,
                footer="Clique pour poser ta question · ignore pour continuer",
            ),
            is_result=True,
        )

    def _accept_gaze_help(self) -> bool:
        """Body click while a help offer is up: ask what is blocking. UI thread.

        Returns True when the click was consumed by the offer, so the normal
        body-click behaviour (raise the main window) does not also fire.
        """

        if not self._gaze_offer_pending:
            return False
        self._gaze_offer_pending = False
        # Accepting once is enough of an answer for a while either way: the
        # user is now being helped, and should not be asked again mid-task.
        self._gaze_offer_muted_until = time.monotonic() + _GAZE_OFFER_COOLDOWN_SECONDS
        if self._busy or self._port is None:
            return True
        self._busy = True
        threading.Thread(
            target=self._gaze_help_worker, daemon=True, name="3loop-widget-gaze-help"
        ).start()
        return True

    def _gaze_help_worker(self) -> None:
        """Ask the question, then hand it to the engine like the mic flow does.

        ``ask_question`` blocks and pumps its own message loop, so it must not
        run on the widget's UI thread - the same reason the research flow uses
        a worker.
        """

        try:
            options = ask_question(
                title="Qu'est-ce qui bloque ?",
                description=(
                    "Dis-moi ce sur quoi tu bloques, je m'en occupe. "
                    "Laisse vide pour annuler."
                ),
                placeholder="ex: je ne comprends pas cette erreur",
            )
        except Exception:
            options = None
        question = options.text.strip() if options is not None else ""
        if not question:
            self._busy = False  # cancelled: the offer simply goes away
            return
        run_prompt_in_background(
            question,
            port=self._port,
            on_done=self._finish_prompt,
            on_started=lambda: self._say(
                "C'est noté", f"Je réfléchis à « {question[:60]} ».", timeout_s=6.0
            ),
        )

    def _maybe_hop(self, now: float) -> None:
        """Bounce on a timer, or on request - but only from a settled idle.

        Guarding on ``idle`` is what keeps a hop from cutting another animation
        in half or from being queued twice.
        """

        if self._clip_name != _CLIP_IDLE:
            return
        if self._hop_requested:
            self._hop_requested = False
            self._start_clip(_CLIP_HOP, now=now)
            return
        if now >= self._next_hop_at:
            self._start_clip(_CLIP_HOP, now=now)

    def _blinking(self, now: float) -> bool:
        """True during a blink; schedules the next one when this one ends."""

        if now < self._next_blink_at:
            return False
        if now < self._next_blink_at + _BLINK_DURATION:
            return True
        self._next_blink_at = now + random.uniform(*_BLINK_INTERVAL)
        return False

    @property
    def _variant(self) -> str:
        """Which sheet to draw from: the watch variant marks assistant mode."""

        return _VARIANT_WATCH if self._assistant_enabled else _VARIANT_PLAIN

    def _frame_index(self) -> int:
        clip = self._clips[self._clip_name]
        return clip.start + self._clip_frame

    # -- rendering ------------------------------------------------------

    def _compose_canvas(
        self,
        frame_index: int,
        *,
        variant: str = "plain",
        icon_opacity: float = 0.0,
        look: tuple[int, int] = (0, 0),
        blinking: bool = False,
        watch_active: bool = False,
    ) -> np.ndarray:
        """Build the whole premultiplied BGRA canvas with numpy only.

        Pure function of its arguments and of the loaded artwork - no Win32, no
        window - which is what makes every clip and both variants checkable
        offscreen.
        """

        canvas = np.zeros((self._canvas_h, self._canvas_w, 4), dtype=np.uint8)
        origin_x, origin_y = self._sprite_origin
        canvas[origin_y : origin_y + self._sprite_h, origin_x : origin_x + self._sprite_w] = self._frames[
            variant
        ][frame_index]
        self._paint_eyes(canvas, frame_index, look=look, blinking=blinking)

        if icon_opacity > 0.01:
            for name, (x0, y0, _x1, _y1) in self._icon_rects.items():
                icon = self._icon_images[name]
                if watch_active and name in self._icon_images_active:
                    icon = self._icon_images_active[name]
                if icon_opacity < 0.999:
                    # Scaling all four premultiplied channels by the same factor
                    # is exactly a per-pixel alpha multiply - the identity
                    # colour * (alpha * o) == (colour * alpha) * o - so the fade
                    # is one numpy multiply instead of an unpack/repack.
                    icon = (icon.astype(np.float32) * icon_opacity).astype(np.uint8)
                canvas[y0 : y0 + _ICON_SIZE, x0 : x0 + _ICON_SIZE] = icon
        return canvas

    def _paint_eyes(
        self, canvas: np.ndarray, frame_index: int, *, look: tuple[int, int], blinking: bool
    ) -> None:
        """Stamp both eyes onto ``canvas`` for the frames that need them.

        ``eyes: null`` in the metadata means the sheet already has them (the
        vanish/return frames, where the eyes shrink with the body): drawing
        anything there would double them up. Otherwise the anchors are the
        top-left corner of each eye in logical pixels, and everything below
        works in logical units multiplied by the derived upscale factor.
        """

        meta = self._frame_meta[frame_index]
        anchors = meta.get("eyes")
        if not anchors:
            return
        width, height = (int(value) for value in meta["eye_size"])
        scale = self._scale
        origin_x, origin_y = self._sprite_origin
        look_x, look_y = look
        for anchor in anchors:
            # Clamped to the logical grid so a look offset can never push a
            # slice outside the sprite's own area of the canvas.
            ax = max(0, min(int(anchor[0]) + look_x, self._logical - width))
            ay = max(0, min(int(anchor[1]) + look_y, self._logical - height))
            px = origin_x + ax * scale
            py = origin_y + ay * scale

            if blinking:
                # A single logical row: the closed lid. Anything thicker reads
                # as a squint held too long at 120ms.
                bar = py + (height // 2) * scale
                canvas[bar : bar + scale, px : px + width * scale] = self._eye_bgra
                continue

            if width >= 3 and height >= 3:
                # Corners left untouched (not cleared) so the body shows
                # through and the eye reads as rounded - the same trick the
                # generator uses, and the reason this is three slices instead
                # of one rectangle.
                canvas[py : py + scale, px + scale : px + (width - 1) * scale] = self._eye_bgra
                canvas[py + scale : py + (height - 1) * scale, px : px + width * scale] = self._eye_bgra
                canvas[
                    py + (height - 1) * scale : py + height * scale,
                    px + scale : px + (width - 1) * scale,
                ] = self._eye_bgra
            else:
                canvas[py : py + height * scale, px : px + width * scale] = self._eye_bgra

            if width >= 3 and height >= 4:
                # Two logical pixels of specular glint, upper-left, same place
                # the generator puts it.
                canvas[py + scale : py + 2 * scale, px + scale : px + 3 * scale] = self._glint_bgra

    def _reassert_topmost(self, now: float) -> None:
        """Re-claim the top of the z-order, about once a second.

        ``WS_EX_TOPMOST`` is set at creation but is not a lease: another topmost
        window, a full-screen app, a UAC prompt or a virtual-desktop switch all
        push the companion down, and it then stays down. Re-asserting is the only
        reliable fix.

        Two things make this cheap and safe:

        * **Not every frame.** ``_redraw`` runs every 33 ms; this runs on its own
          ``_TOPMOST_REASSERT_SECONDS`` clock, so roughly one ``SetWindowPos`` per
          30 redraws.
        * **``SWP_NOACTIVATE`` is mandatory.** Without it, every re-assertion
          would activate this window and pull the caret out of whatever the user
          is typing into - once a second, forever. ``SWP_NOMOVE | SWP_NOSIZE``
          keep it to a pure z-order change, which also means it cannot fight the
          drag or the work-area clamp.

        Nothing is re-asserted while the window is hidden (the research
        "black hole"): ``_redraw`` returns before this is reached, and the guard
        below covers the case where the flag flips mid-frame.
        """

        if now < self._next_topmost_at:
            return
        self._next_topmost_at = now + _TOPMOST_REASSERT_SECONDS
        hwnd = self._hwnd
        if hwnd is None or self._window_hidden:
            return
        user32.SetWindowPos(
            hwnd, HANDLE(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
        )

    def _redraw(self) -> None:
        if self._hwnd is None or self._window_hidden:
            return
        now = time.monotonic()
        self._reassert_topmost(now)
        self._update_pointer_state()
        self._advance_icon_fade()
        self._advance_animation(now)
        if self._window_hidden:
            return  # the vanish clip just ended and took the window with it
        self._maybe_offer_gaze_help(now)
        self._maybe_hop(now)
        canvas = self._compose_canvas(
            self._frame_index(),
            variant=self._variant,
            icon_opacity=self._icon_opacity,
            look=self._look,
            blinking=self._blinking(now),
            watch_active=self._assistant_enabled,
        )
        self._blit(canvas)

    def _blit(self, canvas: np.ndarray) -> None:
        """Hand the canvas to Windows as a premultiplied ARGB layered surface."""

        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)

        canvas_w, canvas_h = self._canvas_w, self._canvas_h
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = canvas_w
        header.biHeight = -canvas_h  # negative = top-down DIB, same order as numpy
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB

        bits_ptr = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(mem_dc, ctypes.byref(header), DIB_RGB_COLORS, ctypes.byref(bits_ptr), None, 0)
        # One memmove for the whole surface: the canvas is already contiguous,
        # top-down, premultiplied BGRA - byte-for-byte what the DIB expects.
        payload = canvas.tobytes()
        ctypes.memmove(bits_ptr, payload, len(payload))

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
    def _point_in_rect(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
        x0, y0, x1, y1 = rect
        return x0 <= x <= x1 and y0 <= y <= y1

    # -- UI-thread marshalling ------------------------------------------

    def _queue_ui(self, action: int, payload: Any = None) -> None:
        with self._lock:
            self._ui_queue.append((action, payload))

    def _post_ui(self, action: int, payload: Any = None) -> None:
        """Ask the UI thread to run ``action`` later (fire and forget)."""

        self._queue_ui(action, payload)
        hwnd = self._hwnd
        if hwnd is not None:
            user32.PostMessageW(hwnd, WM_BUBBLE_REQUEST, 0, 0)

    def _send_ui(self, action: int, payload: Any = None) -> None:
        """Run ``action`` on the UI thread and wait for it.

        Used where the caller has to know the work is done before continuing -
        hiding the bubble immediately before a screen capture. ``SendMessageW``
        blocks this thread until the UI thread has handled it; the UI thread
        never waits on a worker, so there is no cycle to deadlock on.
        """

        self._queue_ui(action, payload)
        hwnd = self._hwnd
        if hwnd is None:
            self._drain_ui()  # no window yet: do it here, still correct
            return
        user32.SendMessageW(hwnd, WM_BUBBLE_REQUEST, 0, 0)

    def _drain_ui(self) -> None:
        while True:
            with self._lock:
                if not self._ui_queue:
                    return
                action, payload = self._ui_queue.pop(0)
            try:
                self._handle_ui(action, payload)
            except Exception:
                pass  # a failed bubble must never take the message loop down

    def _handle_ui(self, action: int, payload: Any) -> None:
        if action == _UI_SHOW_BUBBLE:
            content, is_result = payload
            self._show_bubble(content, is_result=is_result)
        elif action == _UI_HIDE_BUBBLE:
            self._bubble.hide()
            self._bubble_is_result = False
        elif action == _UI_PLAY_VANISH:
            self._research_question = str(payload)
            self._bubble.hide()
            self._start_clip(_CLIP_VANISH)
        elif action == _UI_RESEARCH_DONE:
            self._research_returned(*payload)

    def _show_bubble(self, content: BubbleContent, *, is_result: bool = False) -> None:
        """Put the bubble beside the character. UI thread only.

        Shown from a worker it would appear but never react to a click: the
        window would have no message pump on that thread.

        ``is_result`` marks an outcome (an answer, an error, a mode change) as
        opposed to progress chatter. The priority test lives here rather than in
        ``_say`` because the answer depends on what is on screen *now*, and the
        bubble's state belongs to this thread.
        """

        hwnd = self._hwnd
        if hwnd is None or self._window_hidden:
            # No window to anchor to, or the character is off in the research
            # "black hole" - it cannot speak while it is not there.
            return
        if self._capturing:
            # A card appearing mid-capture would be read straight back by the
            # OCR pass in flight. Dropping it is better than poisoning it - and
            # a tray balloon would be in the screenshot too, so no fallback here.
            return
        if not is_result and self._result_on_screen():
            return  # never talk over something the user is still reading
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        try:
            self._bubble.show((rect.left, rect.top, rect.right, rect.bottom), content)
            self._bubble_is_result = bool(is_result)
        except Exception:
            # Reached from inside the window procedure: a failed card must not
            # escape into ctypes and leave the message loop half-unwound. This is
            # the one case where the Windows balloon is still used.
            self._fallback_toast(content)

    def _result_on_screen(self) -> bool:
        """True while an outcome is still worth the user's attention.

        Either a result bubble is up, or a research payload is waiting for the
        click the bubble asked for - in which case that bubble is the only handle
        on it and must not be replaced by "I'm looking at your screen".
        """

        if self._has_pending_result():
            return True
        return self._bubble_is_result and self._bubble.visible

    # -- background actions ----------------------------------------------

    def _say(
        self,
        title: str,
        *lines: str,
        timeout_s: float | None = 6.0,
        priority: int = _SAY_STEP,
        footer: str = "",
    ) -> None:
        """Have the character say something, from any thread.

        This is the only status channel: every "I'm listening", "I'm reading your
        capture", "here is your answer" goes through a speech bubble drawn by
        ``bubble.py``, in the app's own style. Windows tray balloons were what
        this replaced - generic chrome, off-brand, and silent altogether when the
        user has notifications switched off.

        Callers pass a short timeout for step messages and a longer one for
        outcomes; ``timeout_s=None`` is reserved for a bubble that waits for a
        click (see ``_RESULT_READY_SENTENCE``), because a card with no timeout
        and nothing to act on never goes away.

        Thread affinity is not negotiable: ``Bubble.show`` must run on the thread
        that owns the widget's window, so this only ever *queues* and posts
        ``WM_BUBBLE_REQUEST``. Called from the UI thread itself the post is still
        correct - the queue is drained on the next message.
        """

        content = BubbleContent(
            title=title,
            lines=tuple(line for line in lines if line),
            timeout_s=timeout_s,
            footer=footer,
        )
        if self._hwnd is None:
            # No window yet, so no bubble and no marshalling target: the tray
            # balloon is the only channel left. It may itself be refused (the
            # shell wants an owner window), and that is acceptable - what is lost
            # is a line of status, never state.
            self._fallback_toast(content)
            return
        self._post_ui(_UI_SHOW_BUBBLE, (content, priority == _SAY_RESULT))

    def _fallback_toast(self, content: BubbleContent) -> None:
        """Last resort when the bubble cannot be drawn: a Windows tray balloon.

        Deliberately the only remaining use of ``notify.show_toast`` in this
        module. The balloon carries no styling and nothing clickable, so it is a
        safety net rather than a channel: reached when the window does not exist
        yet, or when rendering the card raised.
        """

        message = " ".join(content.lines) or content.title
        try:
            notify.show_toast(self._hwnd or 0, "LOUPe", message[:250])
        except Exception:
            pass  # a failed notification must never break the flow it reports on

    def _finish_prompt(self, answer: str, success: bool) -> None:
        """A backend answer landed (worker thread): the character reads it out."""

        text = str(answer or "")
        excerpt = text[:_ANSWER_EXCERPT_CHARS] + ("..." if len(text) > _ANSWER_EXCERPT_CHARS else "")
        if success:
            self._say(
                "Voilà ce que j'ai trouvé",
                excerpt,
                timeout_s=90.0,
                priority=_SAY_RESULT,
                footer="La réponse complète est dans la fenêtre LOUPe",
            )
        else:
            self._say(
                "Je me suis cassé les dents",
                f"Ça n'a pas abouti : {excerpt}",
                timeout_s=30.0,
                priority=_SAY_RESULT,
            )
        self._busy = False
        if success:
            self._hop_requested = True  # a little bounce on a job done

    def _start_mic_flow(self) -> None:
        if self._busy or self._port is None:
            return
        self._busy = True
        self._say("Je t'écoute", "Parle maintenant, j'enregistre.", timeout_s=8.0)

        def worker() -> None:
            try:
                text = listen_and_transcribe()
            except Exception as exc:
                self._say(
                    "Mon micro coince",
                    f"Je n'ai pas réussi à t'écouter : {exc}",
                    timeout_s=25.0,
                    priority=_SAY_RESULT,
                )
                self._busy = False
                return
            if not text:
                self._say(
                    "Je n'ai rien entendu",
                    "Reclique sur le micro et parle-moi un peu plus fort.",
                    timeout_s=12.0,
                    priority=_SAY_RESULT,
                )
                self._busy = False
                return
            run_prompt_in_background(
                text, port=self._port, on_done=self._finish_prompt,
                on_started=lambda: self._say(
                    "C'est noté", f"Je réfléchis à « {text[:60]} ».", timeout_s=6.0
                ),
            )

        threading.Thread(target=worker, daemon=True, name="3loop-widget-mic").start()

    # -- research flow: the "black hole" --------------------------------

    def _start_research_flow(self) -> None:
        """Globe icon: ask for a question, then go get swallowed.

        The question is asked from a worker because ``ask_question`` is
        blocking and pumps its own window; called from ``_wndproc`` it would
        nest a second message loop inside this thread's own.
        """

        if self._busy or self._port is None:
            return
        self._busy = True
        threading.Thread(target=self._research_worker, daemon=True, name="3loop-widget-research").start()

    def _research_worker(self) -> None:
        try:
            options = ask_question(
                title="Recherche web LOUPe",
                description=(
                    "Quelle question veux-tu me faire chercher ? Je disparais le "
                    "temps de fouiller le web, puis je reviens avec les articles."
                ),
                placeholder="ex: comparatif des petits modeles locaux",
            )
        except Exception:
            # ask_question already absorbs windowing failures; this only guards
            # the thread itself, because dying here would leave _busy stuck on.
            options = None
        question = options.text.strip() if options is not None else ""
        if options is None or not question:
            self._busy = False  # cancelled: nothing happens at all
            return
        self._post_ui(_UI_PLAY_VANISH, question)

    def _swallow_and_search(self) -> None:
        """End of the vanish clip: hide the window and start the research.

        Called on the UI thread from ``_on_clip_finished``, so the window is
        only hidden once the character has finished being pulled in - hiding it
        earlier would cut the animation the user asked for.

        The backend is not chosen here: ``/api/research`` already forces the
        local Qwen 1.5B model server-side, so imposing one here would only be a
        second place to keep in sync.
        """

        hwnd = self._hwnd
        self._bubble.hide()
        if hwnd is not None:
            user32.ShowWindow(hwnd, SW_HIDE)
        self._window_hidden = True

        with self._lock:
            self._research_resolved = False
        run_research_in_background(
            self._research_question, port=self._port, on_done=self._finish_research
        )
        timer = threading.Timer(_RESEARCH_TIMEOUT_SECONDS, self._research_timed_out)
        timer.daemon = True
        self._research_timer = timer
        timer.start()

    def _claim_research(self) -> bool:
        """First of (answer, timeout) to get here wins; the other is ignored."""

        with self._lock:
            if self._research_resolved:
                return False
            self._research_resolved = True
            return True

    def _finish_research(self, sources: list[dict[str, str]], success: bool) -> None:
        if not self._claim_research():
            return
        self._post_ui(_UI_RESEARCH_DONE, (list(sources or []), bool(success)))

    def _research_timed_out(self) -> None:
        """Safety net: come back even when the search never answers.

        While the main window is closed the mascot is the app's only entry
        point, so staying invisible would strand the user with no UI at all.
        """

        if not self._claim_research():
            return
        self._post_ui(_UI_RESEARCH_DONE, ([], False))

    def _research_returned(self, sources: list[dict[str, str]], success: bool) -> None:
        """Show the window again and play the return clip (UI thread)."""

        if self._research_timer is not None:
            self._research_timer.cancel()
            self._research_timer = None
        self._busy = False
        self._return_payload = (list(sources or []), bool(success))
        hwnd = self._hwnd
        if hwnd is not None:
            # NOACTIVATE: reappearing must not steal focus from whatever the
            # user typed into while the search ran.
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        self._window_hidden = False
        self._start_clip(_CLIP_RETURN)

    def _announce_research(self) -> None:
        """End of the return clip: say something about what came back."""

        payload, self._return_payload = self._return_payload, None
        if payload is None:
            return
        sources, success = payload
        if success and sources:
            with self._lock:
                self._pending_result = sources
            self._hop_requested = True
            self._show_bubble(
                BubbleContent(
                    title="Recherche terminée",
                    lines=(_RESULT_READY_SENTENCE,),
                    timeout_s=None,  # persists: it is the only handle on the result
                    on_click=self._reveal_pending_result,
                    footer=f"{len(sources)} source(s) en attente",
                ),
                is_result=True,
            )
            return
        with self._lock:
            self._pending_result = None
        self._show_bubble(
            BubbleContent(
                title="Recherche interrompue",
                lines=(
                    "Je reviens les mains vides : la recherche a échoué ou a "
                    "pris trop de temps. Vérifie la connexion et renvoie-moi.",
                ),
                timeout_s=30.0,
            ),
            is_result=True,
        )

    def _reveal_pending_result(self) -> bool:
        """Open the result bubble, if a result is waiting. UI thread only.

        Reachable two ways on purpose - clicking the body, or clicking the
        first bubble - because "click me" invites both.
        """

        with self._lock:
            sources = self._pending_result
            self._pending_result = None
        if not sources:
            return False
        links = tuple(
            BubbleLink(
                title=str(source.get("title") or source.get("url") or "source"),
                url=str(source.get("url") or ""),
                domain=str(source.get("domain") or _domain_of(source.get("url", ""))),
            )
            for source in sources[:4]
            if source.get("url")
        )
        question = self._research_question.strip()
        lines = (f'Ce que j\'ai trouvé pour « {question[:70]} » :',) if question else ("Ce que j'ai trouvé :",)
        self._show_bubble(
            BubbleContent(
                title="Résultats de recherche",
                lines=lines,
                links=links,
                timeout_s=120.0,
                footer="Clique une piste pour l'ouvrir",
            ),
            is_result=True,
        )
        return True

    def _has_pending_result(self) -> bool:
        with self._lock:
            return bool(self._pending_result)

    # -- research-assistant mode (the watch variant) ---------------------

    def _ensure_watcher(self) -> ScreenWatcher:
        """The one watcher instance, so both cadences share a memory.

        Creating one per pass would reset its "already seen" state and offer
        the same articles again on the next read.
        """

        if self._watcher is None:
            self._watcher = ScreenWatcher(
                capture=capture_screen,
                ocr=ocr_image,
                search=search_from_text,
                on_result=self._on_watch_result,
                before_capture=self._before_capture,
                after_capture=self._after_capture,
                interval_seconds=self._interval_minutes * 60,
            )
        return self._watcher

    def _before_capture(self) -> None:
        """Hide the bubble before the screen is read. Runs on the watcher thread.

        This is the trap this hook exists for: the bubble is an always-on-top
        window, so it is *in* the screenshot. Leave it up and OCR reads the
        previous suggestion's own headlines, the search runs on them, and the
        assistant starts feeding on its own output - the same three articles,
        drifting further from what the user is actually doing every pass.

        ``_send_ui`` rather than ``_post_ui``: the capture happens on the next
        line, so the bubble has to be gone *now*, not eventually.
        """

        self._capturing = True
        self._send_ui(_UI_HIDE_BUBBLE)

    def _after_capture(self) -> None:
        self._capturing = False

    def _on_watch_result(self, result: WatchResult) -> None:
        """A fresh reading came back with leads (watcher thread)."""

        links = tuple(
            BubbleLink(
                title=str(getattr(item, "title", "") or getattr(item, "url", "")),
                url=str(getattr(item, "url", "")),
                domain=_domain_of(getattr(item, "url", "")),
            )
            for item in result.results[:4]
            if getattr(item, "url", "")
        )
        if not links:
            return
        self._hop_requested = True
        self._post_ui(
            _UI_SHOW_BUBBLE,
            (
                BubbleContent(
                    title="J'ai lu ton écran",
                    lines=("Ces pistes ont l'air d'aller avec ce que tu regardes :",),
                    links=links,
                    timeout_s=45.0,
                    footer=f"Assistant de recherche - toutes les {self._interval_minutes} min",
                ),
                True,  # clickable leads: an outcome, not chatter
            ),
        )

    def _start_watcher(self, *, announce: bool = True) -> None:
        self._assistant_enabled = True  # also swaps the sprite to the watch sheet
        watcher = self._ensure_watcher()
        watcher.set_interval(self._interval_minutes * 60)
        watcher.start()
        if announce:
            self._say(
                "Mode veille activé",
                f"Je jette un œil à ton écran toutes les {self._interval_minutes} minutes "
                f"et je te souffle des pistes.",
                timeout_s=10.0,
                priority=_SAY_RESULT,
            )

    def _stop_watcher(self, *, announce: bool = True) -> None:
        self._assistant_enabled = False
        if self._watcher is not None:
            self._watcher.stop()
        if announce:
            self._say(
                "Mode veille coupé",
                "Je ne lis plus ton écran, je reste juste dans un coin.",
                timeout_s=8.0,
                priority=_SAY_RESULT,
            )

    def _toggle_assistant_mode(self) -> None:
        if self._assistant_enabled:
            self._stop_watcher()
        else:
            self._start_watcher()
        save_assistant_settings(
            enabled=self._assistant_enabled, interval_minutes=self._interval_minutes
        )

    def _set_watch_interval(self, minutes: int) -> None:
        self._interval_minutes = int(minutes)
        self._ensure_watcher().set_interval(self._interval_minutes * 60)
        save_assistant_settings(
            enabled=self._assistant_enabled, interval_minutes=self._interval_minutes
        )
        self._say(
            "Nouvelle cadence",
            f"Je regarderai ton écran toutes les {self._interval_minutes} minutes.",
            timeout_s=8.0,
            priority=_SAY_RESULT,
        )

    def _read_screen_now(self) -> None:
        """Menu item: read the screen immediately.

        With the mode running this only cuts the current wait short. With it
        off, the user still asked for one reading, so a single pass is run on a
        worker thread - OCR plus a search is far too slow for the UI thread.
        """

        watcher = self._ensure_watcher()
        if watcher.running:
            watcher.trigger_now()
            return
        if self._busy:
            return
        self._busy = True

        def worker() -> None:
            try:
                if watcher.run_once() is None:
                    self._say(
                        "Rien de neuf",
                        "J'ai lu ton écran, rien de neuf à te signaler.",
                        timeout_s=12.0,
                        priority=_SAY_RESULT,
                    )
            except Exception as exc:
                self._say(
                    "Lecture impossible",
                    f"Je n'ai pas réussi à lire ton écran : {exc}",
                    timeout_s=25.0,
                    priority=_SAY_RESULT,
                )
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True, name="3loop-widget-screen").start()

    # -- screen capture + OCR flow ---------------------------------------

    def _start_ocr_flow(self) -> None:
        """Let the user drag-select a region, read it, search the web from
        it, and have the model explain it using both.

        The drag-select gesture (mouse down, drag, release) is itself the
        input, so nothing has to be explained before the capture; the question
        is asked afterwards, once there is something to ask about.
        """

        if self._busy or self._port is None:
            return
        self._busy = True

        def worker() -> None:
            from .screen_capture import select_region_and_capture

            try:
                image = select_region_and_capture()
            except Exception as exc:
                self._say(
                    "Capture manquée",
                    f"Je n'ai pas pu prendre la zone : {exc}",
                    timeout_s=25.0,
                    priority=_SAY_RESULT,
                )
                self._busy = False
                return
            if image is None:
                self._busy = False  # user cancelled (Esc / right-click / no drag)
                return

            try:
                self._run_ocr_search_and_explain(image)
            except Exception as exc:
                self._say(
                    "Lecture impossible",
                    f"Je me suis perdu en déchiffrant ta capture : {exc}",
                    timeout_s=25.0,
                    priority=_SAY_RESULT,
                )
                self._busy = False

        threading.Thread(target=worker, daemon=True, name="3loop-widget-ocr").start()

    def _run_ocr_search_and_explain(self, image: Image.Image) -> None:
        self._say("Je lis ta capture", "Je déchiffre le texte, deux secondes.", timeout_s=6.0)
        ocr_text = ocr_image(image)

        if not ocr_text.strip():
            self._say(
                "Rien à lire",
                "Je n'ai trouvé aucun texte lisible dans cette zone.",
                timeout_s=12.0,
                priority=_SAY_RESULT,
            )
            self._busy = False
            return

        # Same card as the research question, drawn by the app itself: the
        # answer is a dataclass (.text / .checkbox), and None means cancelled.
        options = ask_question(
            title="Question sur la capture",
            description=(
                "Le texte a ete lu. Pose une question facultative sur cette "
                "capture, ou laisse vide pour une explication generale."
            ),
            placeholder="ex: pourquoi cette erreur ?",
            checkbox_label="Chercher aussi sur Internet",
            checkbox_default=True,
        )
        if options is None:
            self._busy = False
            self._say("Comme tu veux", "J'oublie cette capture.", timeout_s=5.0)
            return
        question, use_web = options.text, options.checkbox

        results: list = []
        if use_web:
            self._say("Je fouille le web", "Je ramène quelques sources avant de répondre.", timeout_s=8.0)
            try:
                import asyncio

                results = asyncio.run(search_from_screen_text(ocr_text, question=question))
            except Exception:
                results = []  # OCR alone remains useful when the network fails.

        prompt = (
            build_screen_search_prompt(ocr_text, results, question=question)
            if results
            else build_screen_reading_prompt(ocr_text, question=question)
        )
        if prompt is None:
            self._say(
                "Rien à lire",
                "Je n'ai trouvé aucun texte lisible dans cette zone.",
                timeout_s=12.0,
                priority=_SAY_RESULT,
            )
            self._busy = False
            return

        self._say("Je réfléchis", "J'envoie tout ça au modèle et je reviens.", timeout_s=8.0)
        run_prompt_in_background(
            prompt,
            port=self._port,
            on_done=self._finish_prompt,
            on_started=lambda: self._say(
                "Je planche sur ta question", "Encore un instant, je lis et je recoupe.", timeout_s=8.0
            ),
        )

    # -- context menu ----------------------------------------------------

    def _show_context_menu(self, hwnd: wintypes.HWND) -> None:
        """Right-click menu: assistant mode, its cadence, and the way out.

        Destroying the window on any right-click (a very early behaviour)
        risked closing it by accident with no way back short of relaunching the
        app. A menu makes it a deliberate choice, and gives the screen-reading
        mode a home that does not need the main window open.
        """

        cursor = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(
            menu,
            MF_STRING | (MF_CHECKED if self._assistant_enabled else MF_UNCHECKED),
            ID_TOGGLE_ASSISTANT,
            "Assistant de recherche",
        )
        user32.AppendMenuW(menu, MF_STRING, ID_READ_SCREEN_NOW, "Lire l'ecran maintenant")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        for index, minutes in enumerate(INTERVAL_CHOICES_MINUTES):
            user32.AppendMenuW(
                menu,
                MF_STRING | (MF_CHECKED if minutes == self._interval_minutes else MF_UNCHECKED),
                ID_INTERVAL_BASE + index,
                f"Toutes les {minutes} minutes",
            )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_CLOSE_MASCOT, "Fermer la mascotte")
        # Recommended Win32 pattern for a popup menu owned by a window that
        # is not the foreground window: without this the menu can fail to
        # dismiss itself when the user clicks elsewhere.
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, cursor.x, cursor.y, 0, hwnd, None)
        user32.DestroyMenu(menu)

    def _teardown(self) -> None:
        """Release everything the widget owns, on the way out."""

        self._hwnd = None
        # Stop asking the server for a character before the window disappears,
        # so the poll cannot outlive the widget it was drawing for.
        self._theme_poll_stop.set()
        try:
            if self._watcher is not None:
                self._watcher.stop()
        except Exception:
            pass
        try:
            if self._research_timer is not None:
                self._research_timer.cancel()
        except Exception:
            pass
        try:
            self._bubble.destroy()
        except Exception:
            pass

    # -- window procedure -------------------------------------------------

    def _wndproc(self, hwnd: wintypes.HWND, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TIMER:
            self._consume_pending_character()
            self._redraw()
            return 0
        if msg == WM_BUBBLE_REQUEST:
            self._drain_ui()
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
            # Clamped every step, and computed from the *absolute* drag origin
            # rather than from the last position: while the pointer runs past the
            # taskbar the character just sits on the edge, and it picks the
            # pointer up again exactly where it left it instead of having drifted.
            # Probe the proposed absolute point instead of the window's current
            # monitor. Because the drag delta is measured from its fixed origin,
            # the proposal eventually enters the neighbouring monitor even while
            # the visible window is clamped at this one's edge; passing a null
            # hwnd makes _work_area use MonitorFromPoint for that proposal.
            new_x, new_y = self._clamp_to_work_area(
                self._drag_start_window[0] + dx,
                self._drag_start_window[1] + dy,
                hwnd=0,
            )
            self._x, self._y = new_x, new_y
            user32.SetWindowPos(
                hwnd, None, new_x, new_y, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            )
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
                hovering = self._hovering
                if hovering and self._point_in_rect(local_x, local_y, self._icon_rects["mic"]):
                    self._start_mic_flow()
                elif hovering and self._point_in_rect(local_x, local_y, self._icon_rects["ocr"]):
                    self._start_ocr_flow()
                elif hovering and self._point_in_rect(local_x, local_y, self._icon_rects["web"]):
                    self._start_research_flow()
                elif hovering and self._point_in_rect(local_x, local_y, self._icon_rects[_VARIANT_WATCH]):
                    self._toggle_assistant_mode()
                elif not self._accept_gaze_help() and not self._reveal_pending_result():
                    # A body click means "open the app", unless the companion is
                    # offering help or holding a research result - then it means
                    # "yes please" / "show me".
                    try:
                        self._on_click()
                    except Exception:
                        pass
            return 0
        if msg == WM_RBUTTONUP:
            self._show_context_menu(hwnd)
            return 0
        if msg in (WM_DISPLAYCHANGE, WM_SETTINGCHANGE):
            # The floor just moved: a resolution change, a monitor unplugged, or
            # a taskbar that grew / moved / switched to auto-hide (that one
            # arrives as WM_SETTINGCHANGE with SPI_SETWORKAREA). Without this, a
            # character parked at the bottom of the old work area is behind the
            # new taskbar and can no longer be grabbed to be moved out.
            self._reclamp_position(hwnd)
            return 0
        if msg == WM_COMMAND:
            command = wparam & 0xFFFF
            if command == ID_CLOSE_MASCOT:
                user32.DestroyWindow(hwnd)
            elif command == ID_TOGGLE_ASSISTANT:
                self._toggle_assistant_mode()
            elif command == ID_READ_SCREEN_NOW:
                self._read_screen_now()
            elif ID_INTERVAL_BASE <= command < ID_INTERVAL_BASE + len(INTERVAL_CHOICES_MINUTES):
                self._set_watch_interval(INTERVAL_CHOICES_MINUTES[command - ID_INTERVAL_BASE])
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            self._teardown()
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
