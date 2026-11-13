"""Read the foreground game's client rectangle; never capture a fallback desktop.

BORROW: Wingman's auto_screenshot/active_window.py at f0364106 (MIT) establishes
the absolute mss region convention. This narrows that pattern to the actual
foreground StarCitizen.exe client and reads only Windows window/process metadata.
It supplies a screenshot filter, not game attestation or input authority.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import PureWindowsPath


@dataclass(frozen=True)
class GameWindow:
    window_id: int
    process_id: int
    process_created: int
    image_path: str
    left: int
    top: int
    width: int
    height: int

    @property
    def region(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in ("left", "top", "width", "height")}

    @property
    def scope(self) -> str:
        raw = (self.window_id, self.process_id, self.process_created,
               self.image_path.casefold(), self.left, self.top, self.width, self.height)
        return hashlib.sha256(json.dumps(raw).encode()).hexdigest()


def _read_windows() -> GameWindow:
    import ctypes
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.GetForegroundWindow.argtypes = []
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetClientRect.restype = wintypes.BOOL
    user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user.ClientToScreen.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL

    hwnd = user.GetForegroundWindow()
    pid = wintypes.DWORD()
    if not hwnd or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        raise OSError("foreground_window_unavailable")
    # QUERY_LIMITED_INFORMATION only: no process memory, injection or elevation.
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        created, exited, system, elapsed = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(elapsed)):
            raise ctypes.WinError(ctypes.get_last_error())
        creation = (created.dwHighDateTime << 32) | created.dwLowDateTime
        path = image.value
    finally:
        kernel.CloseHandle(handle)

    # mss uses physical pixels. Affect only this thread, then restore its DPI context.
    previous = None
    set_dpi = getattr(user, "SetThreadDpiAwarenessContext", None)
    if set_dpi is None:
        raise OSError("physical_pixel_context_unavailable")
    set_dpi.argtypes, set_dpi.restype = [wintypes.HANDLE], wintypes.HANDLE
    previous = set_dpi(wintypes.HANDLE(-4))
    if not previous:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        rect, origin = wintypes.RECT(), wintypes.POINT()
        if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(
                hwnd, ctypes.byref(origin)):
            raise ctypes.WinError(ctypes.get_last_error())
        if user.GetForegroundWindow() != hwnd:
            raise OSError("foreground_window_changed")
        return GameWindow(int(hwnd), pid.value, creation, path, origin.x, origin.y,
                          rect.right - rect.left, rect.bottom - rect.top)
    finally:
        if previous is not None and not set_dpi(previous):
            raise ctypes.WinError(ctypes.get_last_error())


# Test stand-in for a player: gameplay video played in an ordinary browser window.
# Edge is deliberately absent: it is the owner's own browser and agents leave it alone.
VIDEO_PLAYERS = frozenset({"chrome.exe", "firefox.exe"})
SCREEN_SOURCES = ("game", "video_playback")


def foreground_game(*, probe=None, platform: str | None = None,
                    source: str = "game") -> tuple[GameWindow | None, str]:
    """Return a current screenshot region or a concrete non-admitting status.

    Other hosts may still supply models; this Windows source selector is not a
    restriction on where inference runs. No desktop/whole-monitor fallback.
    ``source="video_playback"`` reads a foreground Chrome or Firefox window instead
    of the game, for testing against played-back footage; it still reads only the
    window in front and never the whole desktop.
    """
    if source not in SCREEN_SOURCES:
        return None, "screen_source_unknown"
    if (sys.platform if platform is None else platform) != "win32":
        return None, "foreground_source_not_supported_on_this_platform"
    try:
        window = (probe or _read_windows)()
        allowed = {"starcitizen.exe"} if source == "game" else VIDEO_PLAYERS
        if (type(window) is not GameWindow
                or PureWindowsPath(window.image_path).name.casefold() not in allowed):
            return None, ("star_citizen_not_foreground" if source == "game"
                          else "video_player_not_foreground")
        if (any(type(getattr(window, name)) is not int for name in
                ("window_id", "process_id", "process_created", "left", "top", "width", "height"))
                or min(window.window_id, window.process_id, window.process_created,
                       window.width, window.height) <= 0):
            return None, "foreground_geometry_unavailable"
        return window, "ready"
    except OSError as error:
        code = getattr(error, "winerror", None)
        return None, f"windows_source_unavailable:{code}" if code else str(error)
    except (ValueError, TypeError):
        return None, "foreground_source_unavailable"
