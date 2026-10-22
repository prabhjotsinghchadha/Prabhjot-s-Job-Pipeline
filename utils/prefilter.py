"""
Deterministic prefilter — skip obvious misses BEFORE AI scoring (zero tokens).

Every scoring loop (scheduler, dashboard SCORE ALL, YOLO, CLI) calls
``run_prefilter(profile)`` first. It walks the unscored backlog, and for each
job that fails a rule it calls ``log_skipped`` with a ``Prefilter: …`` reason,
so the job stays visible in the dashboard (status *skipped*) and can still be
force-scored with RESCORE. Only the survivors reach ``ClaudeBrain.match_job``.

Rules (all configurable under ``profile.yaml → preferences.prefilter``):

  excluded company    preferences.exclude_companies (whole-word, case-insensitive)
  not remote          preferences.remote_only — location/title/description never
                      mention remote, or the location is explicitly on-site/hybrid
  location mismatch   when NOT remote_only: location matches none of
                      preferences.locations (remote postings always pass)
  blocklisted title   phrases that are never an engineering IC role
                      (account executive, recruiter, …) + prefilter.title_blocklist
  unrelated title     prefilter.require_role_keyword — the title shares no
                      word family with preferences.roles (engineer/developer/
                      full stack/frontend/…); prefilter.title_allowlist always passes
  junior / intern     prefilter.skip_junior — when the target roles are senior
  management          prefilter.skip_management — director/VP/head-of/chief titles
                      unless the target roles include them
  stale posting       prefilter.max_age_days (0 = off) using date_posted when known

Nothing here calls the network or an LLM. False positives are cheap: the job
is one click (RESCORE) away from a real AI score.
"""
from __future__ import annotations

import re
from datetime import datetime, date
from typing import Any, Optional

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

DEFAULTS = {
    "enabled": True,
    "require_role_keyword": True,
    "skip_junior": True,
    "skip_management": True,
    "max_age_days": 60,
    "title_blocklist": [],
    "title_allowlist": [],
}

# Titles that are never the engineering IC roles this system targets. Phrases,
# not single words, so "Developer Support Engineer" is not caught by "support".
DEFAULT_TITLE_BLOCKLIST = [
    "account executive", "account manager", "sales engineer", "solutions engineer",
    "solution engineer", "sales development", "business development", "customer success",
    "customer support", "support specialist", "recruiter", "recruiting", "talent acquisition",
    "sourcer", "product manager", "project manager", "program manager", "scrum master",
    "marketing", "growth manager", "accountant", "accounting", "bookkeeper", "paralegal",
    "legal counsel", "attorney", "data entry", "executive assistant", "administrative assistant",
    "office manager", "copywriter", "content writer", "social media", "community manager",
    "nurse", "physician", "pharmacist", "warehouse", "forklift", "driver", "cashier",
    "sales representative", "sales manager", "sales associate", "insurance agent",
    "real estate", "financial analyst", "financial advisor", "underwriter", "loan officer",
    "technical writer", "ux researcher", "graphic designer", "brand designer",
]

# Word families: a role word in preferences.roles expands to these title tokens.
ROLE_SYNONYMS = {
    "engineer": ["engineer", "engineers", "engineering", "developer", "developers",
                 "programmer", "swe", "sde", "dev"],
    "developer": ["developer", "developers", "engineer", "engineering", "programmer", "dev"],
    "software": ["software", "engineer", "developer", "programmer"],
    "full stack": ["full stack", "fullstack", "full-stack"],
    "fullstack": ["full stack", "fullstack", "full-stack"],
    "frontend": ["frontend", "front end", "front-end", "ui engineer", "web developer", "web engineer"],
    "front end": ["frontend", "front end", "front-end"],
    "backend": ["backend", "back end", "back-end", "server side"],
    "back end": ["backend", "back end", "back-end"],
    "web3": ["web3", "blockchain", "solidity", "crypto", "defi", "smart contract"],
    "blockchain": ["blockchain", "web3", "solidity", "smart contract"],
    "founding": ["founding", "founder", "first engineer"],
    "architect": ["architect", "architecture"],
    "mobile": ["mobile", "ios", "android", "react native", "flutter"],
    "devops": ["devops", "sre", "site reliability", "platform engineer", "infrastructure"],
    "data": ["data engineer", "data scientist", "machine learning", "ml engineer", "analytics engineer"],
    "machine learning": ["machine learning", "ml engineer", "ai engineer", "deep learning"],
    "ai": ["ai engineer", "ai", "machine learning", "llm"],
    "security": ["security", "appsec", "infosec"],
    "platform": ["platform"],
    "product": ["product engineer"],
}

# Seniority / generic words that carry no role information on their own.
STOPWORDS = {
    "senior", "sr", "staff", "lead", "principal", "junior", "jr", "mid", "level", "ii", "iii",
    "iv", "i", "the", "a", "an", "and", "or", "of", "to", "in", "remote", "role", "position",
    "job", "jobs", "experienced",
}

JUNIOR_RX = re.compile(
    r"\b(intern|interns|internship|apprentice|apprenticeship|co-?op|junior|jr\.?|entry[\s-]?level|"
    r"graduate|new[\s-]grad|trainee|student|early[\s-]career)\b", re.IGNORECASE)
SENIOR_ROLE_RX = re.compile(r"\b(senior|sr|staff|lead|principal|founding)\b", re.IGNORECASE)
MANAGEMENT_RX = re.compile(
    r"\b(director|vp|vice president|head of|chief|cto|ceo|coo|cfo|cpo|general manager)\b", re.IGNORECASE)
REMOTE_RX = re.compile(
    r"\b(remote|anywhere|distributed|work from home|wfh|telecommut\w*|home[\s-]based|"
    r"remote[\s-]first|remote[\s-]friendly)\b", re.IGNORECASE)
ONSITE_RX = re.compile(r"\b(on[\s-]?site|in[\s-]office|in person|hybrid)\b", re.IGNORECASE)
NOT_REMOTE_RX = re.compile(
    r"\b(not (a )?remote|no remote|remote (is )?not (available|possible|an option)|"
    r"must be (located|based) in|relocat\w* (is )?required|onsite only|on-site only)\b", re.IGNORECASE)
TAG_RX = re.compile(r"<[^>]+>")

# Sources whose inventory is remote by construction.
REMOTE_PLATFORMS = {"remoteok", "remotive", "himalayas", "weworkremotely", "jobspy_remote"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _norm(text: Any) -> str:
    """lowercase, punctuation → spaces, single-spaced, padded for word matching."""
    s = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    return " " + " ".join(s.split()) + " "


def _has_phrase(haystack_norm: str, phrase: str) -> bool:
    p = _norm(phrase).strip()
    if not p:
        return False
    return f" {p} " in haystack_norm


def _clean_desc(desc: Any, limit: int = 6000) -> str:
    text = TAG_RX.sub(" ", str(desc or ""))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return text[:limit]


def _cfg(profile: dict) -> dict:
    prefs = (profile or {}).get("preferences", {}) or {}
    raw = prefs.get("prefilter")
    if raw is False:
        return {**DEFAULTS, "enabled": False}
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if v is not None})
    return cfg


def role_keywords(profile: dict) -> set[str]:
    """Title tokens/phrases derived from preferences.roles (+ synonyms)."""
    roles = (profile or {}).get("preferences", {}).get("roles") or []
    kws: set[str] = set()
    for role in roles:
        rn = _norm(role).strip()
        if not rn:
            continue
        # Multi-word families first ("full stack", "front end", "machine learning").
        for family, syns in ROLE_SYNONYMS.items():
            if " " in family and f" {family} " in f" {rn} ":
                kws.update(syns)
        for word in rn.split():
            if word in STOPWORDS:
                continue
            kws.add(word)
            kws.update(ROLE_SYNONYMS.get(word, []))
    return kws


def job_to_dict(job: Any) -> dict:
    """Accept tracker rows (dicts) or discovery Job dataclasses."""
    if isinstance(job, dict):
        return job
    out = {}
    for k in ("id", "title", "company", "location", "description", "platform",
              "date_posted", "url", "apply_url"):
        out[k] = getattr(job, k, None)
    return out


# --------------------------------------------------------------------------
# Rules — each returns a reason string or None
# --------------------------------------------------------------------------

def _rule_excluded_company(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    excluded = (profile.get("preferences", {}).get("exclude_companies") or [])
    company = _norm(job.get("company"))
    if not company.strip():
        return None
    for ex in excluded:
        exn = _norm(ex).strip()
        if exn and (company.strip() == exn or f" {exn} " in company):
            return f"excluded company ({str(ex).strip()})"
    return None


def _rule_remote(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    prefs = profile.get("preferences", {})
    if not prefs.get("remote_only"):
        return None
    if (job.get("platform") or "") in REMOTE_PLATFORMS:
        return None
    location = str(job.get("location") or "")
    title = str(job.get("title") or "")
    head = f"{location} {title}"
    desc = _clean_desc(job.get("description"))
    # An explicit "not remote" in the posting beats a "Remote" tag from an aggregator.
    if NOT_REMOTE_RX.search(desc):
        return "posting says not remote"
    if REMOTE_RX.search(head):
        return None
    if ONSITE_RX.search(location):
        return f"on-site/hybrid location ({location.strip()[:40]})"
    if REMOTE_RX.search(desc):
        return None
    if location.strip():
        if ONSITE_RX.search(desc):
            return f"on-site/hybrid posting ({location.strip()[:40]})"
        if desc.strip():
            return f"no remote mention ({location.strip()[:40]})"
    return None


def _rule_location(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    prefs = profile.get("preferences", {})
    if prefs.get("remote_only"):
        return None  # remote rule owns this case
    wanted = [str(l) for l in (prefs.get("locations") or []) if str(l).strip()]
    location = str(job.get("location") or "")
    if not wanted or not location.strip():
        return None
    if (job.get("platform") or "") in REMOTE_PLATFORMS or REMOTE_RX.search(location):
        return None
    locn = _norm(location)
    for w in wanted:
        if w.lower().strip() == "remote":
            continue
        if _has_phrase(locn, w) or _norm(w).strip() in locn:
            return None
    return f"location not in preferences ({location.strip()[:40]})"


def _rule_title_blocklist(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    tn = _norm(job.get("title"))
    for phrase in cfg.get("title_allowlist") or []:
        if _has_phrase(tn, phrase):
            return None
    for phrase in DEFAULT_TITLE_BLOCKLIST + list(cfg.get("title_blocklist") or []):
        if _has_phrase(tn, phrase):
            return f"non-engineering title ({phrase})"
    return None


def _rule_role_keyword(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    if not cfg.get("require_role_keyword", True):
        return None
    kws = role_keywords(profile)
    if not kws:
        return None
    tn = _norm(job.get("title"))
    for phrase in cfg.get("title_allowlist") or []:
        if _has_phrase(tn, phrase):
            return None
    compact = tn.replace(" ", "")
    for kw in kws:
        if _has_phrase(tn, kw):
            return None
        kwc = _norm(kw).replace(" ", "")
        if len(kwc) >= 6 and kwc in compact:  # "fullstack" vs "full stack"
            return None
    return "title unrelated to target roles"


def _rule_seniority(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    title = str(job.get("title") or "")
    roles = " ".join(profile.get("preferences", {}).get("roles") or [])
    if cfg.get("skip_junior", True) and SENIOR_ROLE_RX.search(roles) and not JUNIOR_RX.search(roles):
        if JUNIOR_RX.search(title):
            return "junior/intern title"
    if cfg.get("skip_management", True) and not MANAGEMENT_RX.search(roles):
        if MANAGEMENT_RX.search(title):
            return "management/executive title"
    return None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s[:19].replace("Z", "")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _rule_stale(job: dict, profile: dict, cfg: dict) -> Optional[str]:
    try:
        max_age = int(cfg.get("max_age_days") or 0)
    except (TypeError, ValueError):
        max_age = 0
    if max_age <= 0:
        return None
    posted = _parse_date(job.get("date_posted"))
    if posted is None:
        return None
    age = (date.today() - posted).days
    if age > max_age:
        return f"posted {age} days ago (> {max_age})"
    return None


RULES = [
    _rule_excluded_company,
    _rule_title_blocklist,
    _rule_seniority,
    _rule_role_keyword,
    _rule_remote,
    _rule_location,
    _rule_stale,
]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def prefilter_reason(job: Any, profile: dict) -> Optional[str]:
    """Reason this job should NOT be sent to AI scoring, or None to score it."""
    cfg = _cfg(profile)
    if not cfg.get("enabled", True):
        return None
    j = job_to_dict(job)
    for rule in RULES:
        try:
            reason = rule(j, profile, cfg)
        except Exception:
            reason = None  # a broken rule must never block scoring
        if reason:
            return reason
    return None


def reason_key(reason: str) -> str:
    """Bucket a reason for the summary ("excluded company (Acme)" → "excluded company")."""
    return re.sub(r"\s*\(.*$", "", reason).strip()


def run_prefilter(profile: dict, jobs: Optional[list] = None, dry_run: bool = False) -> dict:
    """
    Skip every unscored job that fails a rule. Returns
    {"enabled", "checked", "skipped", "kept", "reasons": {bucket: n}, "samples": [...]}.

    `jobs` defaults to the unscored backlog. With dry_run nothing is written.
    """
    cfg = _cfg(profile)
    summary = {"enabled": bool(cfg.get("enabled", True)), "checked": 0, "skipped": 0,
               "kept": 0, "reasons": {}, "samples": []}
    if not summary["enabled"]:
        return summary

    if jobs is None:
        from utils.tracker import get_unscored_jobs
        jobs = get_unscored_jobs()

    for job in jobs:
        j = job_to_dict(job)
        summary["checked"] += 1
        reason = prefilter_reason(j, profile)
        if not reason:
            summary["kept"] += 1
            continue
        summary["skipped"] += 1
        bucket = reason_key(reason)
        summary["reasons"][bucket] = summary["reasons"].get(bucket, 0) + 1
        if len(summary["samples"]) < 12:
            summary["samples"].append(
                {"id": j.get("id"), "title": j.get("title"), "company": j.get("company"), "reason": reason})
        if not dry_run and j.get("id"):
            from utils.tracker import log_skipped
            log_skipped(j["id"], f"Prefilter: {reason}")

    if not dry_run and summary["checked"]:
        try:
            from utils.events import EventBus
            EventBus.emit("prefilter_complete", {k: v for k, v in summary.items() if k != "samples"})
        except Exception:
            pass
    return summary
