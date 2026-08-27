"""Browser launch helpers."""

import json
import os
import platform

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)

# Automation tells that trip bot detection (Google's "This browser or app
# may not be secure", LinkedIn/Indeed challenges). AutomationControlled is
# what sets navigator.webdriver=true; --enable-automation is a default
# Playwright switch that also flags the browser.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
IGNORE_DEFAULT_ARGS = ["--enable-automation"]


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


def _profile_dir(channel: str = ""):
    """
    Persistent Chromium profile dir, PER-OS (and per browser channel).

    The host (macOS) and the Docker container (Linux) must not share a raw
    profile: Chromium encrypts cookies with an OS-specific key, so a profile
    written on one OS is unreadable on the other. Sessions cross the OS
    boundary via the portable login-state file instead (see below). Real
    Chrome and the Playwright Chromium build get separate dirs too — a
    profile touched by a newer Chromium is refused by an older Chrome.
    """
    from utils.usercontext import cache_dir
    suffix = f"_{channel}" if channel else ""
    d = cache_dir() / f"browser_profile_{platform.system().lower()}{suffix}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def login_state_path():
    """
    Portable session snapshot written by `main.py login` (Playwright
    storage_state: decrypted cookies + localStorage as JSON). This is what
    carries logins from a headed session on the Mac into the headless
    container. Contains live session tokens — never commit it (.cache is
    gitignored) and keep it chmod 600.
    """
    from utils.usercontext import cache_dir
    return cache_dir() / "login_state.json"


async def import_login_state(context) -> int:
    """Seed the context with cookies exported by `main.py login`, if any."""
    path = login_state_path()
    if not path.exists():
        return 0
    try:
        state = json.loads(path.read_text())
        cookies = state.get("cookies", [])
        if cookies:
            await context.add_cookies(cookies)
        return len(cookies)
    except Exception as e:
        print(f"  ⚠ Login-state import failed ({e}) — continuing without it")
        return 0


def find_chrome_executable():
    """Path to the real Google Chrome binary, or None."""
    import shutil
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chrome"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def login_profile_dir():
    """The native-Chrome login profile (separate from the user's daily Chrome)."""
    return _profile_dir("chrome")


async def export_profile_state(p, profile_dir, out_path, seed_state: bool = False):
    """
    Briefly open the given Chrome profile HEADLESS to read (or seed) its
    cookies via Playwright, using the real macOS keychain so cookies written
    by natively-launched Chrome decrypt correctly (Playwright's default
    --use-mock-keychain would make them unreadable, and vice versa).

    seed_state=True: instead of exporting, inject the cookies from the
    existing login-state file into the profile (so a fresh native window
    starts already logged in). Returns the cookie count handled.
    """
    context = await p.chromium.launch_persistent_context(
        str(profile_dir),
        channel="chrome",
        headless=True,
        ignore_default_args=["--use-mock-keychain", "--enable-automation"],
    )
    try:
        if seed_state:
            return await import_login_state(context)
        state = await context.storage_state(path=str(out_path))
        os.chmod(out_path, 0o600)
        return len(state.get("cookies", []))
    finally:
        await context.close()


async def launch_login_browser(p):
    """
    Headed browser for `main.py login` — tuned to pass login flows.

    Prefers the user's REAL installed Google Chrome (channel="chrome") with
    the automation tells disabled and Chrome's native user agent: Google's
    sign-in refuses the Playwright Chromium build ("This browser or app may
    not be secure") but generally accepts genuine Chrome. Falls back to the
    bundled Chromium when Chrome isn't installed. No incognito fallback —
    a login saved into a throwaway context would be pointless.

    Returns (page, close).
    """
    common = dict(
        headless=False,
        viewport={"width": 1400, "height": 900},
        args=STEALTH_ARGS,
        ignore_default_args=IGNORE_DEFAULT_ARGS,
    )
    try:
        context = await p.chromium.launch_persistent_context(
            str(_profile_dir("chrome")), channel="chrome", **common,
        )
        print("  🌐 Using installed Google Chrome (best odds with Google sign-in)")
    except Exception:
        context = await p.chromium.launch_persistent_context(
            str(_profile_dir()), user_agent=USER_AGENT, **common,
        )
        print("  🌐 Google Chrome not found — using bundled Chromium "
              "(Google sign-in may be refused; site-native logins still work)")

    page = context.pages[0] if context.pages else await context.new_page()
    await import_login_state(context)  # previous sessions carry over

    async def close():
        await context.close()

    return page, close


async def launch_apply_browser(p, slow_mo: int = 100, headless=None,
                               fallback: bool = True):
    """
    Launch Chromium for an apply session with a PERSISTENT profile, so
    cookies and logins survive between runs (no more fresh-incognito login
    walls on every application). The profile lives in the per-user .cache
    dir, which the Docker setup bind-mounts, so it also survives rebuilds.
    Sessions exported by `main.py login` on the host are imported on top.

    Only one session can hold the persistent profile at a time; if it's
    locked (or corrupt), fall back to the old ephemeral incognito context
    (still seeded with the exported logins) rather than failing the apply.
    Pass fallback=False to make that a hard error instead — used by
    `main.py login`, where silently landing in incognito would mean the
    session the user just created gets thrown away.

    Returns (page, close) — call `await close()` when done.
    """
    if headless is None:
        headless = not headed_supported()
    kwargs = dict(headless=headless, slow_mo=slow_mo,
                  args=STEALTH_ARGS, ignore_default_args=IGNORE_DEFAULT_ARGS)
    try:
        context = await p.chromium.launch_persistent_context(
            str(_profile_dir()),
            viewport={"width": 1920, "height": 1080},
            user_agent=USER_AGENT,
            **kwargs,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        imported = await import_login_state(context)
        if imported:
            print(f"  🔐 Imported {imported} saved session cookie(s)")

        async def close():
            await context.close()

        return page, close
    except Exception as e:
        if not fallback:
            raise
        print(f"  ⚠ Persistent browser profile unavailable ({e}); "
              f"using a fresh incognito session")
        browser = await p.chromium.launch(**kwargs)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=USER_AGENT,
        )
        page = await context.new_page()
        await import_login_state(context)

        async def close():
            await browser.close()

        return page, close
