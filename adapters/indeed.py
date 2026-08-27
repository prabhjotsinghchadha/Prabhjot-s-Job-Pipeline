"""
Indeed Native Apply Adapter — drives Indeed's hosted apply wizard.

Many Indeed postings ("Easily apply") have no external ATS link: the only
apply surface is Indeed's own multi-step wizard at smartapply.indeed.com,
opened by the "Apply now" button on the viewjob page. The URL resolver can
never turn these into a Greenhouse/Lever form, so apply_smart routes
indeed.com job URLs here instead.

Requires a logged-in Indeed session: utils/browser.py imports the cookies
exported by `main.py login` (.cache/login_state.json) into every apply
browser. Anonymous browsers get Indeed's bot wall and cannot proceed.

Flow:
  1. Load the viewjob page, detect the apply surface:
     - "Apply now" (native)          -> click, capture the smartapply wizard
                                        (new tab, same-tab nav, or iframe)
     - "Apply on company site" link  -> return the external URL so
                                        apply_smart can run the normal
                                        ATS adapters on it
  2. Drive the wizard with the AI form-filling loop (generic.py pattern):
     per step, send the form HTML to ClaudeBrain.ask_json, execute the
     returned fill/select/check/click/upload actions, click Continue.
  3. dry_run=True NEVER clicks the final submit: the review step stops the
     run, and every button is text-checked against SUBMIT_RX before any
     click as a second guard.
"""

import re
import asyncio
import logging
from urllib.parse import urlparse, urljoin

from utils.brain import ClaudeBrain
from adapters.generic import _format_profile

logger = logging.getLogger("indeed_adapter")

# Button text that means "this click submits the application" — the hard
# dry-run guard checks every button against this before clicking.
SUBMIT_RX = re.compile(r"submit|send\s+(my\s+)?application|apply\s*$", re.IGNORECASE)

CONFIRMATION_MARKERS = (
    "application submitted",
    "your application has been submitted",
    "application sent",
)

BOT_WALL_MARKERS = (
    "security check",
    "verify you are human",
    "just a moment",
    "additional verification required",
    "request blocked",
)


def is_indeed_job_url(url: str) -> bool:
    """True for indeed.com job/apply URLs the native flow can handle."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not (host == "indeed.com" or host.endswith(".indeed.com")):
            return False
        if host.startswith("smartapply."):
            return True
        target = f"{parsed.path}?{parsed.query}".lower()
        return any(marker in target for marker in
                   ("viewjob", "jk=", "/job/", "/rc/clk", "/pagead/clk", "applystart"))
    except Exception:
        return False


def _is_indeed_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "indeed.com" or host.endswith(".indeed.com")


async def _detect_wall(page_like) -> str:
    """Detect Indeed's bot wall / login wall / Cloudflare challenge.
    Returns a reason or ''."""
    url = getattr(page_like, "url", "") or ""
    if "secure.indeed.com" in url:
        if "bot-detection" in url:
            return "Indeed bot detection (anonymous session)"
        return "Indeed login wall"
    try:
        # Cloudflare interstitials sometimes render no body text at all —
        # detect them by title / challenge script instead
        cf = await page_like.evaluate("""() => {
            const title = (document.title || '').toLowerCase();
            if (title.includes('just a moment') || title.includes('security check'))
                return true;
            return !!document.querySelector(
                '#challenge-form, script[src*="challenge-platform"]');
        }""")
        if cf:
            return "Cloudflare challenge (rate-limited — wait before retrying)"
    except Exception:
        pass
    try:
        body = (await page_like.inner_text("body"))[:3000].lower()
    except Exception:
        return ""
    for marker in BOT_WALL_MARKERS:
        if marker in body:
            return f"Indeed challenge page ({marker})"
    return ""


# After a wait for manual verification times out, don't stall every
# subsequent apply in the same process (batch runs) — fail fast instead
# until this cooldown passes.
_VERIFICATION_WAIT_COOLDOWN_S = 1800
_last_wait_timeout_at = 0.0


async def _await_manual_verification(page, retry_url: str,
                                     timeout_s: int = 600) -> bool:
    """
    Indeed/Cloudflare is asking for human verification. Instead of failing,
    wait for the user to solve it manually, then continue automatically.

    Two ways the user can clear it:
    1. Headed run (host): solve the challenge right in the visible apply
       browser — Cloudflare then redirects this tab onward by itself.
    2. Headless run (Docker): run `python3.11 main.py login` on the host,
       open indeed.com in that window, complete the check, close it. The
       refreshed .cache/login_state.json (bind-mounted into the container)
       carries the new clearance cookie; we watch the file, re-import the
       cookies, and reload. NOTE: Cloudflare may bind its clearance to the
       solving browser — if the wall persists after import, the only sure
       path is waiting out the rate limit.

    Returns True when the wall is gone and the page is loaded again.
    """
    global _last_wait_timeout_at
    import time as _time

    if _time.monotonic() - _last_wait_timeout_at < _VERIFICATION_WAIT_COOLDOWN_S \
            and _last_wait_timeout_at > 0:
        print("  [!] Recent verification wait timed out — failing fast "
              "(retry later or complete the check via `python3.11 main.py login`)")
        return False

    from utils.browser import login_state_path, import_login_state
    from utils.system_state import is_paused

    path = login_state_path()
    try:
        baseline = path.stat().st_mtime if path.exists() else 0
    except Exception:
        baseline = 0

    print("  [=] WAITING for human verification (up to "
          f"{timeout_s // 60} min)...")
    print("      Option A (visible browser): solve the challenge in the "
          "apply window.")
    print("      Option B (Docker/headless): on your Mac run "
          "`python3.11 main.py login`,")
    print("      open https://www.indeed.com, complete the check, close "
          "the window.")

    waited = 0
    while waited < timeout_s:
        await asyncio.sleep(10)
        waited += 10

        try:
            if is_paused():
                print("  [!] System paused — abandoning the verification wait")
                return False
        except Exception:
            pass

        # Case 1: solved in this very browser (headed run) — Cloudflare
        # moves the tab on by itself once the check passes.
        try:
            if not await _detect_wall(page):
                print("  [+] Verification cleared in the apply browser — continuing")
                return True
        except Exception:
            pass

        # Case 2: refreshed login-state export from `main.py login`
        try:
            mtime = path.stat().st_mtime if path.exists() else 0
        except Exception:
            mtime = 0
        if mtime > baseline:
            baseline = mtime
            print("  [*] Refreshed login state detected — importing cookies "
                  "and retrying")
            try:
                await import_login_state(page.context)
            except Exception:
                pass
            try:
                await page.goto(retry_url, wait_until="domcontentloaded",
                                timeout=30000)
                await asyncio.sleep(3)
            except Exception as e:
                print(f"  [!] Reload failed: {e}")
                continue
            wall = await _detect_wall(page)
            if not wall:
                print("  [+] Verification cleared — continuing")
                return True
            print(f"  [!] Still walled after cookie import ({wall}) — "
                  "the clearance may be bound to the solving browser; "
                  "still waiting")

    print(f"  [!] Timed out waiting for manual verification ({timeout_s}s)")
    _last_wait_timeout_at = _time.monotonic()
    return False


# ──────────────────────────────────────────────────────────────
# Apply-surface detection on the viewjob page
# ──────────────────────────────────────────────────────────────

_SCAN_APPLY_BUTTONS_JS = """() => {
    const visible = el => !!(el.offsetParent ||
        (el.getClientRects && el.getClientRects().length));
    const found = [];
    for (const el of document.querySelectorAll('button, a')) {
        if (!visible(el)) continue;
        const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
        const id = el.id || '';
        const testid = el.getAttribute('data-testid') || '';
        const cls = typeof el.className === 'string' ? el.className : '';
        if (/apply/i.test(text) || /apply/i.test(id) ||
            /apply/i.test(testid) || /indeed-?apply/i.test(cls)) {
            found.push({
                tag: el.tagName.toLowerCase(),
                id, testid,
                text: text.slice(0, 80),
                href: el.getAttribute('href') || '',
                cls: cls.slice(0, 120),
            });
        }
        if (found.length >= 12) break;
    }
    return found;
}"""


def _classify_apply_candidates(candidates: list, page_url: str) -> dict:
    """
    Pick the best apply control from the JS scan.

    Returns {"mode": "native"|"external"|"external_button"|"none",
             "selector": str, "url": str}
    """
    native = None
    native_fallback = None
    external_link = None
    external_button = None

    for cand in candidates:
        text = cand.get("text", "")
        ident = " ".join([cand.get("id", ""), cand.get("testid", ""),
                          cand.get("cls", "")])
        href = cand.get("href", "")

        # Absolute-ify relative hrefs for classification
        abs_href = urljoin(page_url, href) if href else ""

        # The native button's label varies: "Apply now" / "Apply with Indeed".
        # Its id/testid is a per-render hash, so text is the main signal.
        is_native_marker = (
            cand.get("id") == "indeedApplyButton"
            or re.search(r"indeed-?apply", ident, re.I)
            or re.match(r"apply\s+(now|with\s+indeed)\b", text, re.I)
        )
        is_company_site = re.search(r"apply on company (web)?site", text, re.I)

        if is_company_site:
            if abs_href.startswith("http") and not _is_indeed_host(abs_href):
                external_link = external_link or (cand, abs_href)
            else:
                external_button = external_button or cand
        elif is_native_marker and (not href or _is_indeed_host(abs_href)):
            native = native or cand
        elif abs_href.startswith("http") and not _is_indeed_host(abs_href) \
                and re.search(r"\bapply\b", text, re.I):
            external_link = external_link or (cand, abs_href)
        elif cand.get("tag") == "button" and not href \
                and re.match(r"apply\b", text, re.I):
            native_fallback = native_fallback or cand

    def selector_for(cand: dict) -> str:
        # [id="..."] not #id — Indeed's hashed ids can start with a digit
        if cand.get("id"):
            return f'[id="{cand["id"]}"]'
        if cand.get("testid"):
            return f'[data-testid="{cand["testid"]}"]'
        tag = cand.get("tag", "button")
        text = cand.get("text", "").replace('"', '\\"')[:40]
        return f'{tag}:has-text("{text}")'

    if native:
        return {"mode": "native", "selector": selector_for(native),
                "url": "", "cand": native}
    if external_link:
        cand, url = external_link
        return {"mode": "external", "selector": selector_for(cand),
                "url": url, "cand": cand}
    if external_button:
        return {"mode": "external_button",
                "selector": selector_for(external_button), "url": "",
                "cand": external_button}
    if native_fallback:
        return {"mode": "native", "selector": selector_for(native_fallback),
                "url": "", "cand": native_fallback}
    return {"mode": "none", "selector": "", "url": "", "cand": None}


_JS_CLICK_BY_INFO = """(info) => {
    const visible = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    let el = null;
    if (info.id) el = document.getElementById(info.id);
    if (!el && info.testid) {
        const matches = [...document.querySelectorAll(
            '[data-testid="' + info.testid + '"]')];
        el = matches.find(visible) || matches[0];
    }
    if (!el && info.text) {
        // Prefer VISIBLE matches — hidden modal templates (e.g. the exit
        // dialog) can contain the same button text and precede it in DOM
        el = [...document.querySelectorAll(info.tag || 'button')].find(
            e => visible(e) && (e.innerText || '').trim()
                 .replace(/\\s+/g, ' ').startsWith(info.text));
    }
    if (!el) return false;
    el.click();
    return true;
}"""


async def _click_apply_control(page, selector: str, cand: dict) -> bool:
    """
    Click the apply control. Indeed sometimes floats an aria-modal alert
    dialog over the page that intercepts real pointer events, so fall back
    to a JS-dispatched click (React handles synthetic clicks fine).
    """
    try:
        await page.click(selector, timeout=5000)
        return True
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")  # close any open popover
    except Exception:
        pass
    try:
        info = {"id": (cand or {}).get("id", ""),
                "testid": (cand or {}).get("testid", ""),
                "text": (cand or {}).get("text", "")[:40],
                "tag": (cand or {}).get("tag", "button")}
        return bool(await page.evaluate(_JS_CLICK_BY_INFO, info))
    except Exception as e:
        logger.debug(f"JS click fallback failed: {e}")
        return False


async def _click_and_capture(page, selector: str, cand: dict = None,
                             timeout_s: float = 12.0):
    """
    Click an apply control and return the page/frame that now holds the
    apply surface: a popup page, the same page after navigation, or an
    embedded smartapply iframe. Returns (page_like, url) or (None, "").
    """
    context = page.context
    start_url = page.url
    popup_holder = {}

    def _on_page(new_page):
        popup_holder.setdefault("page", new_page)

    context.on("page", _on_page)
    try:
        if not await _click_apply_control(page, selector, cand):
            print(f"  [!] Could not click apply button ({selector})")
            return None, ""

        waited = 0.0
        while waited < timeout_s:
            await asyncio.sleep(1.0)
            waited += 1.0

            popup = popup_holder.get("page")
            if popup:
                try:
                    await popup.wait_for_load_state("domcontentloaded",
                                                    timeout=15000)
                except Exception:
                    pass
                return popup, popup.url

            if "smartapply.indeed.com" in page.url \
                    or "secure.indeed.com" in page.url:
                return page, page.url

            for frame in page.frames:
                frame_url = getattr(frame, "url", "") or ""
                # A preloadresumeapply iframe exists BEFORE the click —
                # it is not the wizard, so don't grab it.
                if ("smartapply" in frame_url or "indeedapply" in frame_url) \
                        and "preload" not in frame_url:
                    return frame, frame_url

        # No popup/iframe — did the tab at least navigate somewhere new?
        if page.url and page.url != start_url:
            return page, page.url
        return None, ""
    finally:
        try:
            context.remove_listener("page", _on_page)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# Wizard step analysis (generic.py pattern: HTML -> ask_json -> act)
# ──────────────────────────────────────────────────────────────

_EXTRACT_STEP_HTML_JS = """() => {
    const selectors = ['main', 'form', '[data-testid*="Step"]',
                       '[class*="ia-"]', 'body'];
    for (const sel of selectors) {
        const el = document.querySelector(sel);
        if (el && el.innerHTML && el.innerHTML.length > 200) {
            return el.outerHTML;
        }
    }
    return document.body ? document.body.innerHTML : '';
}"""

WIZARD_PROMPT = """You are automating one step of Indeed's hosted job application wizard (smartapply.indeed.com). It is a multi-step form: typically contact info -> resume -> employer questions -> review & submit. Contact fields are often pre-filled from the Indeed account; leave correct pre-filled values alone.

APPLICANT PROFILE (use these values; answer employer questions from them):
{profile_block}

COVER LETTER (only if there is a cover letter field):
{cover_letter}

CURRENT STEP URL: {step_url}

CURRENT STEP HTML:
{step_html}

Return JSON:
{{
  "status": "fill_and_continue" | "review_submit" | "done" | "blocked" | "no_form",
  "description": "<one line: what this step is>",
  "fields": [
    {{"action": "fill",   "selector": "<CSS>", "value": "<text>", "note": "<field>"}},
    {{"action": "select", "selector": "<CSS>", "value": "<option>", "note": "<field>"}},
    {{"action": "check",  "selector": "<CSS>", "note": "<checkbox>"}},
    {{"action": "click",  "selector": "<CSS>", "note": "<radio/card, e.g. saved resume>"}},
    {{"action": "upload", "selector": "<CSS>", "file_key": "resume", "note": "resume upload"}}
  ],
  "continue_selector": "<CSS selector of the Continue/Next button for this step>"
}}

Rules:
- "review_submit" = this is the final review step; the primary button SUBMITS the application (e.g. "Submit your application"). Do NOT put that button in continue_selector unless status is "review_submit".
- "done" = confirmation page, application already submitted.
- "blocked" = sign-in prompt, CAPTCHA, or error page that fill actions cannot fix.
- If a saved/Indeed resume card is shown, "click" to select it instead of uploading.
- Only include fields that need changing; skip correctly pre-filled ones.
- Smartapply tags most elements with data-testid — STRONGLY prefer [data-testid="..."] selectors (e.g. the step's continue button is button[data-testid="continue-button"]). Ignore the global nav header; the form is inside [data-testid="root-route"].
- Prefer robust selectors: [data-testid=...], #id, [name=...], [aria-label=...]. Never use generated CSS class names (css-xxxx, mosaic-provider-...).
- Employer screener questions: answer truthfully from the profile (years of experience, authorization, etc.)."""


async def _robust_el_click(el) -> bool:
    """Click an element; fall back to a JS-dispatched click if intercepted."""
    try:
        await el.click(timeout=5000)
        return True
    except Exception:
        try:
            await el.evaluate("el => el.click()")
            return True
        except Exception:
            return False


async def _run_wizard_step_actions(wizard, fields: list, profile: dict,
                                   dry_run: bool) -> int:
    """Execute the AI's field actions on the current wizard step."""
    done = 0
    for inst in fields:
        action = inst.get("action", "")
        selector = inst.get("selector", "")
        value = inst.get("value", "")
        note = inst.get("note", "") or selector
        if not selector:
            continue
        try:
            el = await wizard.wait_for_selector(selector, timeout=3000)
            if not el:
                continue

            if action == "fill":
                await el.fill(value)
                print(f"    [+] {note}: filled")
            elif action == "select":
                try:
                    await el.select_option(value=value)
                except Exception:
                    await el.select_option(label=value)
                print(f"    [+] {note}: selected '{value}'")
            elif action == "check":
                # Styled checkboxes hide the real input — check() then waits
                # forever on visibility; bound it and fall back to a JS click
                try:
                    if not await el.is_checked():
                        await el.check(timeout=4000)
                except Exception:
                    await el.evaluate(
                        "el => { el.click(); "
                        "el.dispatchEvent(new Event('change', {bubbles: true})); }")
                print(f"    [+] {note}: checked")
            elif action == "click":
                # Dry-run guard: never click anything submit-like
                text = ""
                try:
                    text = (await el.inner_text() or "").strip()
                except Exception:
                    pass
                if dry_run and SUBMIT_RX.search(text):
                    print(f"    [=] {note}: skipped (submit-like button in dry run)")
                    continue
                if not await _robust_el_click(el):
                    print(f"    [!] {note}: click failed")
                    continue
                print(f"    [+] {note}: clicked")
            elif action == "upload":
                resume_path = profile.get("resume_path", "")
                if resume_path:
                    try:
                        await el.set_input_files(resume_path)
                    except Exception:
                        # AI may target the visible card — the real
                        # input[type=file] is hidden next to it
                        file_el = await wizard.query_selector('input[type="file"]')
                        if not file_el:
                            raise
                        await file_el.set_input_files(resume_path)
                    print(f"    [+] {note}: uploaded resume")
            else:
                continue
            done += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            print(f"    [!] {note}: {e}")
    return done


_SCAN_STEP_BUTTONS_JS = """() => {
    const visible = el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const out = [];
    for (const el of document.querySelectorAll('button, input[type="submit"]')) {
        if (!visible(el)) continue;
        const text = (el.innerText || el.value || '').trim()
            .replace(/\\s+/g, ' ');
        if (!text) continue;
        out.push({tag: el.tagName.toLowerCase(),
                  testid: el.getAttribute('data-testid') || '',
                  id: el.id || '', text: text.slice(0, 60)});
    }
    return out;
}"""

# gnav / chrome buttons that must never be treated as step navigation
IGNORE_BUTTON_RX = re.compile(
    r"skip to main|save and close|report an issue|accept terms|"
    r"privacy|terms of service|cancel|^back\b|sign in|new update|^exit",
    re.IGNORECASE)

CONTINUE_RX = re.compile(
    r"^(continue|next\b|save and continue|review your application)",
    re.IGNORECASE)


async def _advance_wizard(wizard, continue_selector: str, status: str,
                          dry_run: bool) -> str:
    """
    Click the step's Continue (or final Submit) button.

    Buttons are found by scanning VISIBLE buttons in JS, not by selector
    waits: smartapply keeps hidden modal templates (the "Save and close"
    exit dialog) whose buttons can be the first DOM match for a text
    selector, making wait_for_selector spin on an invisible element. The
    continue button's data-testid also varies by step ("continue-button"
    on some, a per-render hash on others), so text is the real signal.

    Returns: "next" | "submitted" | "dry_run_stop" | "failed"
    """
    is_final = status == "review_submit"

    if is_final and dry_run:
        print("\n  [=] DRY RUN -- Reached the review step. NOT submitting.")
        print("      Review the filled application in the browser.")
        return "dry_run_stop"

    try:
        buttons = await wizard.evaluate(_SCAN_STEP_BUTTONS_JS)
    except Exception:
        buttons = []

    actionable = [b for b in buttons
                  if not IGNORE_BUTTON_RX.search(b.get("text", ""))]

    target = None
    for b in actionable:
        if b.get("testid") == "continue-button":
            target = b
            break
    if not target:
        for b in actionable:
            if CONTINUE_RX.match(b.get("text", "")):
                target = b
                break
    if not target and is_final:
        for b in actionable:
            if SUBMIT_RX.search(b.get("text", "")):
                target = b
                break
    if not target and len(actionable) == 1 \
            and SUBMIT_RX.search(actionable[0].get("text", "")):
        # The only actionable button submits — this IS the final step,
        # whatever the AI labeled it.
        target = actionable[0]
        is_final = True

    if not target and continue_selector:
        # Last resort: the AI's own selector, visibility- and text-guarded
        try:
            el = await wizard.wait_for_selector(continue_selector, timeout=2000)
            if el and await el.is_visible():
                el_text = ((await el.inner_text()) or "").strip()
                if not IGNORE_BUTTON_RX.search(el_text):
                    target = {"id": "", "testid": "", "tag": "button",
                              "text": el_text}
        except Exception:
            pass

    if not target:
        print(f"  [!] No Continue button found on this step "
              f"(visible buttons: {[b['text'] for b in actionable][:6]})")
        return "failed"

    text = target.get("text", "")

    # Hard dry-run guard: a mislabeled step must never submit.
    if dry_run and (is_final or SUBMIT_RX.search(text)):
        print(f"\n  [=] DRY RUN -- Next button is '{text}'. NOT clicking.")
        return "dry_run_stop"

    if is_final:
        print(f"  [>] Submitting application ('{text}')...")
    else:
        print(f"  [>] Continue ('{text}')...")
    clicked = False
    try:
        clicked = bool(await wizard.evaluate(_JS_CLICK_BY_INFO, {
            "id": target.get("id", ""), "testid": target.get("testid", ""),
            "text": text[:40], "tag": target.get("tag", "button")}))
    except Exception:
        clicked = False
    if not clicked:
        print("  [!] Failed to click the step's navigation button")
        return "failed"
    await asyncio.sleep(3)
    return "submitted" if is_final else "next"


async def _wait_step_rendered(wizard, tries: int = 10) -> None:
    """Wait out smartapply's spinner interstitials between steps so we
    don't waste a Claude CLI call analyzing an empty loading screen.
    Only counts elements inside the app container ([data-testid="root-route"])
    — the global nav header always has buttons, so a whole-document count
    would pass while the step is still loading."""
    for _ in range(tries):
        try:
            ready = await wizard.evaluate("""() => {
                if ((document.body.className || '').includes('loading'))
                    return false;
                const root = document.querySelector('[data-testid="root-route"]')
                    || document.querySelector('main')
                    || document.querySelector('form');
                if (root && root.querySelectorAll(
                        'input, select, textarea, button, [role="radio"]'
                    ).length > 0)
                    return true;
                // Some modules (contact-info) have no marked container —
                // document-wide form controls still signal readiness
                // (the gnav header has none of these)
                return document.querySelectorAll(
                    'input, select, textarea').length > 0;
            }""")
            if ready:
                return
        except Exception:
            return
        await asyncio.sleep(2)


async def _handle_resume_selection_step(wizard, profile: dict) -> bool:
    """
    Deterministic handler for smartapply's resume-selection step, which has
    stable data-testid markup:
      [data-testid="resume-selection-form"] containing radio cards
      ("...-file-resume-upload-radio-card", "...-build-resume-radio-card",
      a saved-resume card when one exists) with a HIDDEN file input, and
      button[data-testid="continue-button"].
    Returns True if the step was handled and Continue was clicked.
    """
    try:
        form = await wizard.query_selector('[data-testid="resume-selection-form"]')
    except Exception:
        form = None
    if not form:
        return False

    print("  [*] Resume selection step (deterministic handler)")
    resume_path = profile.get("resume_path", "")
    upload_card = await wizard.query_selector(
        '[data-testid="resume-selection-file-resume-upload-radio-card"]')

    selected = False
    if resume_path and upload_card:
        if await _robust_el_click(upload_card):
            print("    [+] Selected 'Upload a resume'")
        await asyncio.sleep(1)
        file_input = await wizard.query_selector(
            '[data-testid="resume-selection-file-resume-upload-radio-card-file-input"]'
        ) or await wizard.query_selector('input[type="file"]')
        if file_input:
            try:
                await file_input.set_input_files(resume_path)
                print(f"    [+] Uploaded resume: {resume_path}")
                selected = True
                await asyncio.sleep(4)  # let the upload process
            except Exception as e:
                print(f"    [!] Resume upload failed: {e}")

    if not selected:
        # Fall back to any saved-resume card (not build-your-own, which
        # leads into Indeed's multi-page resume builder)
        try:
            cards = await wizard.query_selector_all('[data-testid$="-radio-card"]')
        except Exception:
            cards = []
        for card in cards:
            testid = (await card.get_attribute("data-testid")) or ""
            if "build-resume" in testid or "file-resume-upload" in testid:
                continue
            if await _robust_el_click(card):
                print(f"    [+] Selected saved resume ({testid})")
                selected = True
                await asyncio.sleep(1)
                break

    if not selected:
        print("    [!] No usable resume option — leaving step to the AI loop")
        return False

    # The Continue button can lag behind the card selection re-render
    cont = None
    for _ in range(4):
        cont = await wizard.query_selector('button[data-testid="continue-button"]')
        if cont:
            break
        await asyncio.sleep(2)
    if cont and await _robust_el_click(cont):
        print("  [>] Continue (resume step)...")
        await asyncio.sleep(3)
        return True
    return False


async def _wizard_confirmed(wizard, clicked_submit: bool = False) -> bool:
    """Check the wizard page for a post-submit confirmation.

    Body-text markers only count AFTER a submit was actually clicked —
    intermediate wizard pages carry phrases like "keep track of your
    application" that once produced a false "submitted" verdict during a
    dry run (verified against Indeed My Jobs: nothing was submitted).
    Before a submit click, only an unambiguous post-apply URL counts.
    """
    try:
        url = getattr(wizard, "url", "") or ""
        if any(m in url for m in ("post-apply", "confirmation", "/success")):
            return True
        if not clicked_submit:
            return False
        body = (await wizard.inner_text("body"))[:4000].lower()
        return any(marker in body for marker in CONFIRMATION_MARKERS)
    except Exception:
        return False


async def _fill_wizard(wizard, profile: dict, brain: ClaudeBrain,
                       cover_letter: str, dry_run: bool,
                       max_steps: int = 12) -> bool:
    """Drive the smartapply wizard step-by-step. Returns True on success."""
    profile_block = _format_profile(profile)

    step = 0
    resume_step_attempts = 0
    no_form_streak = 0
    while step < max_steps:
        step += 1
        print(f"\n  --- Indeed Apply: step {step} ---")
        await asyncio.sleep(2)  # let the SPA render the step
        await _wait_step_rendered(wizard)

        wall = await _detect_wall(wizard)
        if wall:
            print(f"  [!] {wall}")
            # Frames can't be re-navigated the same way — wait only for pages
            if not hasattr(wizard, "goto") or \
                    not await _await_manual_verification(
                        wizard, getattr(wizard, "url", "") or ""):
                return False
            continue  # step re-renders after the reload — re-analyze it

        if await _wizard_confirmed(wizard):
            print("  [+] Application submitted successfully!")
            return True

        # Known steps with stable markup are handled without an AI call
        # (no-op when the step isn't present). After two tries, let the
        # AI loop take over (validation errors etc.).
        if resume_step_attempts < 2 \
                and await _handle_resume_selection_step(wizard, profile):
            resume_step_attempts += 1
            continue

        try:
            step_html = await wizard.evaluate(_EXTRACT_STEP_HTML_JS)
        except Exception as e:
            print(f"  [!] Could not read wizard step: {e}")
            return False
        step_url = getattr(wizard, "url", "") or ""

        prompt = WIZARD_PROMPT.format(
            profile_block=profile_block,
            cover_letter=(cover_letter[:1000] if cover_letter else "N/A"),
            step_url=step_url,
            step_html=(step_html or "")[:14000],
        )
        try:
            plan = brain.ask_json(prompt, timeout=90, component="form_analysis")
        except Exception as e:
            print(f"  [!] Claude analysis failed: {e}")
            return False

        status = plan.get("status", "no_form")
        print(f"  [*] {plan.get('description', status)}")

        if status == "done":
            # Corroborate the AI's verdict against the page — an AI "done"
            # on an intermediate page is the same false-positive class as
            # the marker bug (nothing had actually been submitted)
            if await _wizard_confirmed(wizard, clicked_submit=True):
                print("  [+] Application submitted successfully!")
                return True
            print("  [?] AI reported 'done' but no confirmation evidence — continuing")
            no_form_streak += 1
            if no_form_streak >= 3:
                return False
            await asyncio.sleep(4)
            continue
        if status == "blocked":
            print("  [!] Wizard blocked (sign-in/CAPTCHA/error) — cannot continue")
            return False
        if status == "no_form":
            # Step transitions re-render the SPA; give it a few chances
            no_form_streak += 1
            if no_form_streak >= 3:
                print("  [!] No form found in the apply wizard")
                return False
            print("  [*] Step not rendered yet — waiting before re-analyzing...")
            await asyncio.sleep(4)
            continue
        no_form_streak = 0

        filled = await _run_wizard_step_actions(
            wizard, plan.get("fields", []), profile, dry_run)
        if filled:
            print(f"  [+] Executed {filled} field action(s)")

        nav = await _advance_wizard(
            wizard, plan.get("continue_selector", ""), status, dry_run)

        if nav == "dry_run_stop":
            return True
        if nav == "submitted":
            if await _wizard_confirmed(wizard, clicked_submit=True):
                print("  [+] Application submitted successfully!")
            else:
                print("  [?] Submitted but no clear confirmation detected")
            return True
        if nav == "failed":
            return False
        # nav == "next": loop to the next step

    print(f"  [!] Reached max wizard steps ({max_steps})")
    return False


# ──────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────

async def apply_indeed(
    page,
    job_url: str,
    profile: dict,
    brain: ClaudeBrain,
    cover_letter: str = "",
    dry_run: bool = True,
    max_steps: int = 12,
) -> dict:
    """
    Apply to an Indeed posting via Indeed's hosted apply wizard.

    Returns a dict:
        {"status": "success"}                   applied (or dry-run complete)
        {"status": "external", "url": "..."}    posting links to an external
                                                ATS — caller should run the
                                                normal adapters on that URL
        {"status": "no_apply_button"}           nothing to click — caller may
                                                fall back to URL resolution
        {"status": "failed"}                    hard failure (bot wall, login
                                                wall, wizard error)
    """
    # Direct smartapply link: skip the viewjob page entirely
    if "smartapply.indeed.com" in job_url:
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  [!] Failed to load smartapply URL: {e}")
            return {"status": "failed"}
        await asyncio.sleep(2)
        wall = await _detect_wall(page)
        if wall:
            print(f"  [!] {wall}")
            if not await _await_manual_verification(page, job_url):
                return {"status": "failed"}
        ok = await _fill_wizard(page, profile, brain, cover_letter, dry_run,
                                max_steps=max_steps)
        return {"status": "success" if ok else "failed"}

    print(f"  [*] Loading Indeed posting: {job_url[:80]}")
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  [!] Failed to load Indeed page: {e}")
        return {"status": "failed"}
    await asyncio.sleep(3)

    wall = await _detect_wall(page)
    if wall:
        print(f"  [!] {wall}")
        if not await _await_manual_verification(page, job_url):
            return {"status": "failed"}

    try:
        candidates = await page.evaluate(_SCAN_APPLY_BUTTONS_JS)
    except Exception as e:
        print(f"  [!] Could not scan for apply button: {e}")
        candidates = []

    target = _classify_apply_candidates(candidates or [], page.url)

    if target["mode"] == "none":
        print("  [!] No apply button found on the Indeed posting")
        return {"status": "no_apply_button"}

    if target["mode"] == "external":
        return {"status": "external", "url": target["url"]}

    if target["mode"] == "external_button":
        # "Apply on company site" opens the ATS in a new tab via JS —
        # click it, grab the destination URL, hand it back to apply_smart.
        print("  [*] 'Apply on company site' button — capturing destination")
        surface, dest_url = await _click_and_capture(
            page, target["selector"], target.get("cand"))
        if dest_url and not _is_indeed_host(dest_url):
            if surface is not page and hasattr(surface, "close"):
                try:
                    await surface.close()
                except Exception:
                    pass
            return {"status": "external", "url": dest_url}
        print("  [!] Could not capture the external application URL")
        return {"status": "failed"}

    # Native "Apply now" -> smartapply wizard
    print("  [*] Native Indeed Apply detected — opening the apply wizard")
    wizard, wizard_url = await _click_and_capture(
        page, target["selector"], target.get("cand"))
    if wizard is None:
        print("  [!] Apply wizard did not open")
        return {"status": "failed"}

    if "secure.indeed.com" in (wizard_url or ""):
        print("  [!] Indeed asked to sign in — the saved session has expired.")
        print("      Refresh it with `python3.11 main.py login` on the host.")
        return {"status": "failed"}

    print(f"  [+] Apply wizard: {wizard_url[:80]}")
    ok = await _fill_wizard(wizard, profile, brain, cover_letter, dry_run,
                            max_steps=max_steps)
    return {"status": "success" if ok else "failed"}
