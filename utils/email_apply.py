"""
Email Apply — apply to jobs by sending the application over email.

Many postings (HN Who-is-Hiring, startup career pages, smaller companies)
say "email your resume to jobs@..." instead of running an ATS. Browser
automation hits login walls on those sites anyway, so email is often the
MORE reliable apply channel:

  1. extract_apply_email()      — regex-scan a posting for a hiring inbox
  2. compose_application_email()— build a draft (cover-letter based; AI fallback)
  3. send_application_email()   — SMTP send with the resume PDF attached

Sending reuses the profile.yaml `email` section (same Gmail app password as
IMAP checking). SMTP host/port can be overridden with `smtp_server` /
`smtp_port`; otherwise they're derived from the IMAP host.

INVARIANT: nothing in this module decides to send on its own. Every send is
triggered by an explicit user confirmation in the dashboard.
"""

import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Optional

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Local parts that signal a hiring inbox — strongly preferred
HIRING_LOCALPARTS = (
    "jobs", "job", "careers", "career", "hiring", "apply", "applications",
    "application", "recruit", "recruiting", "recruitment", "talent", "hr",
    "people", "join", "work", "resume", "resumes", "cv",
)

# Never send an application to these local parts
EXCLUDE_LOCALPART_PATTERNS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notification", "unsubscribe", "abuse", "privacy", "security",
    "billing", "sales", "press", "marketing", "newsletter", "webmaster",
    "postmaster", "mailer-daemon", "bounce", "support", "help", "feedback",
    # Disability-accommodation inboxes (every big-co ATS posting has one —
    # e.g. accommodations-ext@figma.com); NOT where applications go
    "accommodation", "accessibility", "disability",
)

# An email mentioned right after accommodation/legal boilerplate is that
# boilerplate's contact, not the hiring inbox
_NEGATIVE_CONTEXT = re.compile(
    r"accommodat|disabilit|accessibil|equal opportunit|harassment|complaint",
    re.IGNORECASE,
)

# Domains that are never a human hiring inbox (ATS notifiers, aggregators,
# placeholder domains, common scrape false-positives)
EXCLUDE_DOMAINS = (
    "example.com", "example.org", "yourcompany.com", "company.com",
    "email.com", "domain.com", "test.com", "sentry.io", "wixpress.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
    "myworkday.com", "myworkdayjobs.com", "icims.com",
    "smartrecruiters.com", "jobvite.com", "bamboohr.com", "jazz.co",
    "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "remoteok.com", "remoteok.io", "adzuna.com", "ycombinator.com",
    "wellfound.com", "angel.co", "monster.com", "dice.com",
)

# Words near an address that mark it as the place to apply
_APPLY_CONTEXT = re.compile(
    r"(apply|application|resume|resumes|cv|email us|reach out|contact|"
    r"send|interested|write to|drop (?:us|me)|ping)",
    re.IGNORECASE,
)

_IMAGE_TLDS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def _candidate_score(email: str, text: str, pos: int) -> int:
    """Score an email candidate; higher = more likely the hiring inbox."""
    local, _, domain = email.lower().partition("@")
    score = 0
    if any(local == lp or local.startswith(lp + ".") or local.startswith(lp + "-")
           or local.startswith(lp + "_") or local == lp + "s"
           for lp in HIRING_LOCALPARTS):
        score += 10
    # "send your resume to X" style context just before the address
    context = text[max(0, pos - 120):pos]
    if _NEGATIVE_CONTEXT.search(context):
        return -1  # accommodation/legal boilerplate contact — never use
    if _APPLY_CONTEXT.search(context):
        score += 5
    # Personal-looking addresses on a company domain still beat nothing
    return score


def _is_excluded(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if any(p in local for p in EXCLUDE_LOCALPART_PATTERNS):
        return True
    if domain.endswith(_IMAGE_TLDS):
        return True
    for excl in EXCLUDE_DOMAINS:
        if domain == excl or domain.endswith("." + excl):
            return True
    return False


def extract_apply_email(text: str) -> Optional[str]:
    """
    Find the most likely application email address in a job posting.
    Returns None when nothing plausible is found. Pure regex — no tokens.
    """
    if not text:
        return None
    # De-obfuscate the common "name [at] domain [dot] com" patterns first
    cleaned = re.sub(r"\s*\[?\(?\bat\b\)?\]?\s*", "@", text, flags=re.IGNORECASE) \
        if re.search(r"\[\s*at\s*\]|\(\s*at\s*\)", text, re.IGNORECASE) else text
    cleaned = re.sub(r"\s*\[\s*dot\s*\]\s*|\s*\(\s*dot\s*\)\s*", ".", cleaned,
                     flags=re.IGNORECASE)

    best: Optional[str] = None
    best_score = -1
    for m in EMAIL_RE.finditer(cleaned):
        email = m.group(0).strip(".,;:!?").lower()
        if _is_excluded(email):
            continue
        score = _candidate_score(email, cleaned, m.start())
        if score > best_score:
            best, best_score = email, score
    return best


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------

def _signature(profile: dict) -> str:
    p = profile.get("personal", {})
    lines = [f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()]
    for key in ("phone", "email", "linkedin", "github", "portfolio"):
        if p.get(key):
            lines.append(str(p[key]))
    return "\n".join(lines)


def compose_application_email(job: dict, profile: dict, brain=None) -> dict:
    """
    Build an application email draft for a job.

    Uses the stored cover letter when one exists (no tokens). Falls back to
    an AI-written body via `brain`, then to a plain template. Returns
    {"to", "subject", "body"} — a DRAFT for the user to review, never sent
    from here.
    """
    p = profile.get("personal", {})
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    title = job.get("title", "the open position")
    company = job.get("company", "your company")
    to = job.get("apply_email") or ""

    subject = f"Application for {title} — {name}"
    cover_letter = (job.get("cover_letter") or "").strip()

    if cover_letter:
        # Don't stack greetings when the cover letter already opens with one
        has_greeting = re.match(r"^(hi|hello|hey|dear|greetings)\b",
                                cover_letter, re.IGNORECASE)
        greeting = "" if has_greeting else "Hi,\n\n"
        # ...and don't add a closing when the letter already signs off
        has_signoff = re.search(r"(best regards|kind regards|sincerely|"
                                r"best,|regards,|thanks,|thank you,)",
                                cover_letter[-200:], re.IGNORECASE)
        closing = ("" if has_signoff else
                   f"\n\nMy resume is attached.\n\nBest regards,\n{_signature(profile)}")
        body = f"{greeting}{cover_letter}{closing}"
        return {"to": to, "subject": subject, "body": body}

    if brain is not None:
        try:
            result = brain.ask_json(
                f"""Write a short job application email (NOT a letter to print).

APPLICANT:
{_signature(profile)}
Roles sought: {', '.join(profile.get('preferences', {}).get('roles', []))}

JOB: {title} at {company}
POSTING (may be truncated):
{(job.get('description') or '')[:3000]}

Return JSON: {{"subject": "<subject line>", "body": "<email body, 120-180 words,
plain text, professional but human, mentions the resume is attached, ends with
'Best regards,' and the applicant's name>"}}""",
                component="form_analysis",
            )
            if result.get("body"):
                return {
                    "to": to,
                    "subject": result.get("subject") or subject,
                    "body": result["body"].strip() + "\n\n" + _signature(profile),
                }
        except Exception:
            pass  # fall through to the plain template

    body = (
        f"Hi,\n\n"
        f"I'm writing to apply for the {title} role at {company}. "
        f"My background matches what you're looking for, and my resume is "
        f"attached with the full details.\n\n"
        f"I'd welcome the chance to talk about how I can contribute.\n\n"
        f"Best regards,\n{_signature(profile)}"
    )
    return {"to": to, "subject": subject, "body": body}


# ---------------------------------------------------------------------------
# Send (SMTP)
# ---------------------------------------------------------------------------

def smtp_config(profile: dict) -> dict:
    """
    Resolve SMTP settings from the profile `email` section.
    Raises ValueError with a user-facing message when not configured.
    """
    cfg = profile.get("email", {}) or {}
    email_addr = (cfg.get("email") or "").strip()
    password = (cfg.get("app_password") or "").strip()
    if not email_addr or not password:
        raise ValueError(
            "Email sending is not configured. Set email.email and "
            "email.app_password in your profile (Gmail: create an App "
            "Password at myaccount.google.com/apppasswords)."
        )
    imap = cfg.get("imap_server", "imap.gmail.com")
    server = cfg.get("smtp_server") or imap.replace("imap.", "smtp.", 1)
    port = int(cfg.get("smtp_port") or 465)
    return {"server": server, "port": port, "email": email_addr,
            "password": password}


def send_application_email(
    profile: dict,
    to: str,
    subject: str,
    body: str,
    resume_path: str = "",
) -> None:
    """
    Send one application email with the resume PDF attached.
    Raises on any failure (config, attachment, SMTP) — caller logs the result.
    """
    to = (to or "").strip()
    if not to or not EMAIL_RE.fullmatch(to):
        raise ValueError(f"Invalid recipient address: {to!r}")

    cfg = smtp_config(profile)
    p = profile.get("personal", {})
    name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()

    msg = EmailMessage()
    msg["From"] = formataddr((name or cfg["email"], cfg["email"]))
    msg["To"] = to
    msg["Subject"] = subject
    personal_email = (p.get("email") or "").strip()
    if personal_email and personal_email.lower() != cfg["email"].lower():
        msg["Reply-To"] = personal_email
    msg.set_content(body)

    if resume_path:
        path = Path(resume_path)
        if not path.exists():
            raise FileNotFoundError(f"Resume not found: {resume_path}")
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf" if path.suffix.lower() == ".pdf" else "octet-stream",
            filename=path.name,
        )

    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["server"], cfg["port"], timeout=30) as smtp:
            smtp.login(cfg["email"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["server"], cfg["port"], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg["email"], cfg["password"])
            smtp.send_message(msg)


def test_smtp_login(profile: dict) -> dict:
    """
    Verify the SMTP credentials by logging in (nothing is sent).
    Returns connection details on success; raises on failure.
    """
    cfg = smtp_config(profile)
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["server"], cfg["port"], timeout=15) as smtp:
            smtp.login(cfg["email"], cfg["password"])
    else:
        with smtplib.SMTP(cfg["server"], cfg["port"], timeout=15) as smtp:
            smtp.starttls()
            smtp.login(cfg["email"], cfg["password"])
    return {"email": cfg["email"], "server": cfg["server"], "port": cfg["port"]}


# ---------------------------------------------------------------------------
# Backfill — populate apply_email for already-discovered jobs
# ---------------------------------------------------------------------------

def backfill_apply_emails() -> dict:
    """
    Regex-scan every stored job that has a description but no apply_email
    yet, and persist any address found. Cheap (no tokens, no network).
    """
    from utils.tracker import get_db
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, description, metadata FROM applications
               WHERE (apply_email IS NULL OR apply_email = '')
                 AND (description != '' OR metadata != '{}')"""
        ).fetchall()
        found = 0
        for row in rows:
            text = (row["description"] or "") + " " + (row["metadata"] or "")
            email = extract_apply_email(text)
            if email:
                conn.execute(
                    "UPDATE applications SET apply_email = ? WHERE id = ?",
                    (email, row["id"]),
                )
                found += 1
        conn.commit()
    finally:
        conn.close()
    return {"scanned": len(rows), "found": found}
