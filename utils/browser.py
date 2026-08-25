"""Browser launch helpers."""

import os
import platform


def headed_supported() -> bool:
    """
    Whether this environment can show a visible (headed) browser window.

    On Linux a headed Chromium needs an X/Wayland display server — absent in
    Docker, so launching headed there crashes at startup. macOS and Windows
    always support headed windows.
    """
    if platform.system() != "Linux":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
