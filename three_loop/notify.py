"""Native Windows tray balloon notifications via Shell_NotifyIcon.

No extra dependency: the same ctypes approach already used for the floating
widget's layered window, reused here for the "background task finished"
notification (mic/OCR question answered while the user was doing something
else).
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_INFO = 0x00000010
NIM_ADD = 0x00000000
NIM_DELETE = 0x00000002
NIIF_INFO = 0x00000001
IDI_APPLICATION = 32512

#: Message Shell_NotifyIconW sends back to `hWnd` on tray-icon/balloon mouse
#: events (wParam=icon id, lParam=the actual event, e.g. WM_LBUTTONUP or
#: NIN_BALLOONUSERCLICK). WM_APP+2 - clear of WM_APP+1, which the widget
#: doesn't currently use but reserving distinct offsets avoids collisions if
#: it grows more custom messages later.
WM_TRAYICON = 0x8000 + 2
NIN_BALLOONUSERCLICK = 0x0400 + 5
WM_LBUTTONUP = 0x0202

user32.LoadIconW.restype = wintypes.HICON
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


def show_toast(hwnd: int, title: str, message: str, *, icon_id: int = 1, clickable: bool = False) -> None:
    """Pop a Windows tray balloon, then remove the tray icon a few seconds later.

    ``clickable=True`` makes Windows deliver ``WM_TRAYICON`` to ``hwnd`` when
    the user clicks the balloon or the tray icon itself - the caller's
    ``_wndproc`` decides what that means (3loop's own widget opens the main
    window on it, for the proactive research nudge).
    """

    data = _NOTIFYICONDATAW()
    data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
    data.hWnd = hwnd
    data.uID = icon_id
    data.uFlags = NIF_INFO | NIF_ICON | NIF_MESSAGE
    data.uCallbackMessage = WM_TRAYICON if clickable else 0
    data.hIcon = user32.LoadIconW(None, ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR))
    data.szTip = "3loop"
    data.szInfo = message[:255]
    data.szInfoTitle = title[:63]
    data.dwInfoFlags = NIIF_INFO
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))

    def _cleanup() -> None:
        removal = _NOTIFYICONDATAW()
        removal.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        removal.hWnd = hwnd
        removal.uID = icon_id
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(removal))

    threading.Timer(10.0, _cleanup).start()
