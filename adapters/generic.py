"""
Generic Form Adapter — AI-driven form filling for any job application page.

Uses Claude Code CLI to analyze arbitrary HTML forms and generate
fill instructions. This is the fallback for sites without a dedicated adapter.
"""

import random
import re
from playwright.async_api import Page

# Buttons that finalize an application — in dry-run these must NEVER be
# clicked, no matter how the AI labeled the step.
_SUBMIT_BTN_RX = re.compile(
    r"submit|send|apply now|apply\b|finish|complete application|reach out",
    re.IGNORECASE,
)
from utils.brain import ClaudeBrain
from utils.answers import find_cached_answer, get_personal_field


async def apply_generic(
    page: Page,
    job_url: str,
    profile: dict,
    brain: ClaudeBrain,
    cover_letter: str = "",
    dry_run: bool = True,
    max_wizard_steps: int = 8
):
    """
    AI-driven form filler for arbitrary job application pages.
    Handles both single-page forms and multi-step wizards.
    """
    personal = profile["personal"]

    print(f"  📝 Loading application page...")
    await page.goto(job_url, wait_until="networkidle")
    await page.wait_for_timeout(2000)
    # Client-rendered apps (React/Inertia, e.g. Work at a Startup) hydrate
    # after networkidle — snapshotting too early sees only the server shell
    # (a data-page JSON blob and scripts, no buttons). Wait until the DOM
    # actually has interactive elements.
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('button, input, select, textarea').length > 3",
            timeout=8000,
        )
        await page.wait_for_timeout(500)
    except Exception:
        pass  # static page or slow app — proceed with what's there

    step = 0
    while step < max_wizard_steps:
        step += 1
        print(f"\n  --- Step {step} ---")

        # Grab current visible form HTML
        form_html = await page.evaluate("""() => {
            // Strip payload blobs so the size-truncated snapshot keeps UI
            // structure: scripts/styles and framework data attributes
            // (Inertia's data-page JSON can alone exceed the size budget).
            const cleaned = (el) => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll('script, style, svg, noscript')
                     .forEach(n => n.remove());
                [clone, ...clone.querySelectorAll('[data-page]')]
                    .forEach(n => n.removeAttribute && n.removeAttribute('data-page'));
                return clone.outerHTML;
            };
            // Try to find the most relevant form container
            const selectors = [
                '[role="dialog"]',
                'form[class*="application"]',
                'form[class*="apply"]',
                'form',
                'main',
                '[class*="application"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.innerHTML.length > 100) {
                    return cleaned(el);
                }
            }
            return cleaned(document.body);
        }""")

        # Truncate for CLI context limits
        form_html = form_html[:14000]

        # Ask Claude to analyze the form
        try:
            instructions = brain.ask_json(f"""You are automating a job application form.

APPLICANT:
{_format_profile(profile)}

COVER LETTER (use if there's a cover letter field):
{cover_letter[:1000] if cover_letter else 'N/A'}

CURRENT PAGE HTML:
{form_html}

Analyze the form and return a JSON object:
{{
  "status": "fill_and_next" | "submit" | "done" | "no_form",
  "description": "<what you see on this page>",
  "fields": [
    {{"action": "fill", "selector": "<CSS>", "value": "<text>", "note": "<field name>"}},
    {{"action": "select", "selector": "<CSS>", "value": "<option>", "note": "<field name>"}},
    {{"action": "check", "selector": "<CSS>", "note": "<checkbox name>"}},
    {{"action": "upload", "selector": "<CSS>", "file_key": "resume", "note": "resume upload"}}
  ],
  "next_button": "<CSS selector for next/submit/continue button>"
}}

Rules:
- status "done" = form already submitted / confirmation page
- status "no_form" = no application form AND no way to reach one from here
- If there is no form yet but an Apply / Apply Now button likely opens or
  reveals the application form (modal or next page), return "fill_and_next"
  with an empty fields list and that button as next_button
- Use robust CSS selectors (prefer #id, [name=...], [aria-label=...])
- For file uploads, set file_key to "resume"
- Put the most standard fields first (name, email, phone)
""")
        except Exception as e:
            print(f"  ⚠ Claude analysis failed: {e}")
            break

        status = instructions.get("status", "no_form")
        desc = instructions.get("description", "")
        print(f"  📋 {desc}")

        if status == "done":
            print(f"  ✅ Application appears to be submitted!")
            return True

        if status == "no_form":
            print(f"  ⚠ No application form found on this page")
            return False

        # Execute field fills
        fields = instructions.get("fields", [])
        for field_inst in fields:
            action = field_inst.get("action", "")
            selector = field_inst.get("selector", "")
            value = field_inst.get("value", "")
            note = field_inst.get("note", "")

            try:
                el = await page.wait_for_selector(selector, timeout=3000)
                if not el:
                    continue

                if action == "fill":
                    await el.fill(value)
                    print(f"    ✅ {note}: filled")
                elif action == "select":
                    try:
                        await el.select_option(value=value)
                    except Exception:
                        await el.select_option(label=value)
                    print(f"    ✅ {note}: selected '{value}'")
                elif action == "check":
                    is_checked = await el.is_checked()
                    if not is_checked:
                        await el.check()
                    print(f"    ✅ {note}: checked")
                elif action == "upload":
                    await el.set_input_files(profile["resume_path"])
                    print(f"    ✅ {note}: uploaded")

                await page.wait_for_timeout(random.randint(300, 700))

            except Exception as e:
                print(f"    ⚠ {note or selector}: {e}")

        # Click next/submit
        next_btn_selector = instructions.get("next_button")
        if next_btn_selector:
            if status == "submit" and dry_run:
                print(f"\n  🏁 DRY RUN — Would click submit: {next_btn_selector}")
                print(f"     Review the form in the browser window")
                return True

            try:
                btn = await page.wait_for_selector(next_btn_selector, timeout=5000)
                if btn:
                    if dry_run:
                        # Defense in depth: the status label above comes from
                        # the AI and can be wrong (a Send button labeled
                        # "fill_and_next" once submitted a real application
                        # in dry-run). Check what the button actually says.
                        label = ((await btn.inner_text()) or "").strip()
                        if _SUBMIT_BTN_RX.search(label):
                            print(f"  🏁 DRY RUN — refusing to click "
                                  f"'{label or next_btn_selector}' (submit-like button)")
                            return True
                    if status == "submit":
                        print(f"  🚀 Submitting...")
                    else:
                        print(f"  ➡️  Next step...")
                    await btn.click()
                    await page.wait_for_timeout(2000)
            except Exception as e:
                print(f"  ⚠ Button click failed ({next_btn_selector}): {e}")
                break

        if status == "submit":
            # Check for confirmation
            await page.wait_for_timeout(2000)
            body_text = await page.inner_text("body")
            if any(w in body_text.lower() for w in ["thank you", "submitted", "confirmation", "received"]):
                print(f"  ✅ Application submitted successfully!")
                return True
            else:
                print(f"  ⚠ Submitted but no clear confirmation")
                return True

    print(f"  ⚠ Reached max wizard steps ({max_wizard_steps})")
    return False


def _format_profile(profile: dict) -> str:
    """Format profile for the AI prompt."""
    p = profile["personal"]
    common = profile.get("common_answers", {})
    lines = [
        f"Name: {p.get('first_name', '')} {p.get('last_name', '')}",
        f"Email: {p.get('email', '')}",
        f"Phone: {p.get('phone', '')}",
        f"Location: {p.get('location', '')}",
        f"LinkedIn: {p.get('linkedin', '')}",
        f"GitHub: {p.get('github', '')}",
        f"Website: {p.get('portfolio', '')}",
    ]
    for key, val in common.items():
        lines.append(f"{key}: {val}")
    return "\n".join(lines)
