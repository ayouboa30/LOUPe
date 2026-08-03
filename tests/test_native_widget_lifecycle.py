"""Guards on the mascot's lifecycle: it must outlive the main window.

Closing the main app window used to end the whole process (the widget's
thread is a daemon), taking the mascot down with it - the opposite of the
"the mascot stays on screen, close it explicitly" behaviour the app wants.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32-only widget")


def test_on_close_fires_when_the_window_is_destroyed() -> None:
    """Right-click -> "Fermer la mascotte" is the only way to end the app
    now that closing the main window merely hides it; WM_DESTROY must reach
    the caller's teardown callback rather than silently ending the thread.
    """

    from three_loop.native_widget import ID_CLOSE_MASCOT, WM_COMMAND, WM_DESTROY, NativeWidget

    closed = []
    widget = NativeWidget(lambda: None, on_close=lambda: closed.append(True))

    # WM_COMMAND for the menu's one item must trigger DestroyWindow, which a
    # real Win32 window then follows with WM_DESTROY - simulate the destroy
    # step directly, since DestroyWindow itself needs a live hwnd/thread.
    assert widget._wndproc(0, WM_DESTROY, 0, 0) == 0
    assert closed == [True]


def test_on_close_is_optional() -> None:
    """A widget with no on_close (e.g. a standalone script) must not crash."""

    from three_loop.native_widget import WM_DESTROY, NativeWidget

    widget = NativeWidget(lambda: None)

    assert widget._wndproc(0, WM_DESTROY, 0, 0) == 0


def test_context_menu_command_id_closes_the_window(monkeypatch) -> None:
    """WM_COMMAND with the menu's id must call DestroyWindow, not any other id."""

    from three_loop.native_widget import ID_CLOSE_MASCOT, WM_COMMAND, NativeWidget

    destroyed = []
    monkeypatch.setattr(
        "three_loop.native_widget.user32.DestroyWindow", lambda hwnd: destroyed.append(hwnd)
    )
    widget = NativeWidget(lambda: None)

    widget._wndproc(42, WM_COMMAND, ID_CLOSE_MASCOT, 0)
    assert destroyed == [42]

    destroyed.clear()
    widget._wndproc(42, WM_COMMAND, 9999, 0)  # unrelated command id
    assert destroyed == []
