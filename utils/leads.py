"""
Clients mode: finding leads.

A lead is a row with kind='lead'. Two flavours, told apart by metadata.lead_type:

  gig       a contract/freelance posting someone published
            (HN "Seeking freelancer" thread, jobspy contract search, contract
            postings on the regular boards)
  prospect  a company that is hiring in your stack and might buy outside help
            instead — built from the jobs already in the database, no network

Everything here is deterministic and free; Claude only sees leads at scoring time.
"""

import copy
import json
import re
from typing import Optional

CONTRACT_RX = re.compile(
    r"\b(contract(or|ing)?|freelanc(e|er|ing)|fractional|consultant|consulting|"
    r"hourly|part[- ]time|1099|c2c|corp[- ]to[- ]corp|project[- ]based|"
    r"seeking freelancer|short[- ]term|fixed[- ]term|interim)\b", re.I)
_EMPLOYMENT_HINTS = ("contract", "freelance", "part", "temporary", "project")


def offer_cfg(profile: dict) -> dict:
    return (profile or {}).get("offer") or {}


def offer_source_enabled(profile: dict, key: str) -> bool:
    return (offer_cfg(profile).get("sources") or {}).get(key) is not False


def is_contract_posting(job) -> Optional[str]:
    """Why this posting looks like contract/freelance work, or None."""
    meta = getattr(job, "metadata", None) if not isinstance(job, dict) else job.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    meta = meta or {}
    emp = str(meta.get("employment_type") or "").lower()
    if any(h in emp for h in _EMPLOYMENT_HINTS):
        return f"employment type: {emp}"
    tags = meta.get("tags")
    tags_s = " ".join(tags) if isinstance(tags, list) else str(tags or "")
    get = (lambda k: job.get(k)) if isinstance(job, dict) else (lambda k: getattr(job, k, ""))
    title = str(get("title") or "")
    m = CONTRACT_RX.search(title) or CONTRACT_RX.search(tags_s)
    if m:
        return f"title/tags: {m.group(0)}"
    desc = str(get("description") or "")[:1500]
    m = CONTRACT_RX.search(desc)
    if m and not re.search(r"\bfull[- ]time\b", title, re.I):
        return f"posting mentions: {m.group(0)}"
    return None


def _as_lead(job, lead_type: str = "gig", **extra):
    """Tag a discovery Job as a lead. Gigs get their own id so a posting that is
    already tracked as a job can also live in the clients pipeline."""
    job.metadata = dict(job.metadata or {})
    job.metadata.update({"kind": "lead", "lead_type": lead_type, **extra})
    if lead_type == "gig" and not job.id.startswith("gig_"):
        job.id = f"gig_{job.id}"
    return job


# --------------------------------------------------------------------------
# Gigs
# --------------------------------------------------------------------------

def gigs_from_hn_freelancer(profile: dict) -> list:
    """'SEEKING FREELANCER' posts in this month's HN freelancer thread."""
    from utils.hn_source import find_thread, parse_comment, clean_comment, GENERIC_TECH_WORDS
    gigs = []
    try:
        _, title, children = find_thread("Ask HN: Freelancer? Seeking freelancer?")
        for c in children:
            text = clean_comment(c.get("text", ""))
            if not re.match(r"\s*seeking\s+freelancer", text, re.I):
                continue
            job = parse_comment(c, title, GENERIC_TECH_WORDS)
            if job:
                job.title = re.sub(r"^\s*seeking\s+freelancer\s*[|:\-]?\s*", "", job.title, flags=re.I)[:120] or "Freelance work"
                job.company = re.sub(r"^\s*seeking\s+freelancer\s*[|:\-]?\s*", "", job.company, flags=re.I)[:80] or "Unknown"
                gigs.append(_as_lead(job, "gig", source="hn_freelancer"))
        print(f"  HN freelancer thread: {len(gigs)} seeking-freelancer posts")
    except Exception as e:
        print(f"  ⚠ HN freelancer thread failed: {e}")
    return gigs


def gigs_from_jobspy(profile: dict) -> list:
    from utils.jobspy_source import discover_jobspy_jobs
    queries = offer_cfg(profile).get("gig_queries") or []
    if not queries:
        return []
    try:
        jobs = discover_jobspy_jobs(profile, queries=queries, job_type="contract")
        return [_as_lead(j, "gig", source="jobspy_contract") for j in jobs]
    except Exception as e:
        print(f"  ⚠ jobspy contract search failed: {e}")
        return []


async def gigs_from_boards(profile: dict) -> list:
    """Run the regular boards with the gig queries as roles; keep contract-looking postings."""
    from utils.discovery import discover_all_jobs
    p = copy.deepcopy(profile)
    p["preferences"]["roles"] = offer_cfg(profile).get("gig_queries") or p["preferences"].get("roles", [])
    p["sources"] = {**(p.get("sources") or {}), "jobspy": False}  # jobspy handled separately
    try:
        jobs = await discover_all_jobs(p)
    except Exception as e:
        print(f"  ⚠ board scan for gigs failed: {e}")
        return []
    gigs = []
    for j in jobs:
        why = is_contract_posting(j)
        if why:
            gigs.append(_as_lead(j, "gig", source="boards_contract", contract_signal=why))
    print(f"  Boards: {len(gigs)} contract-looking postings out of {len(jobs)}")
    return gigs


async def discover_gigs(profile: dict) -> list:
    import asyncio
    gigs = []
    if offer_source_enabled(profile, "hn_freelancer"):
        gigs += await asyncio.to_thread(gigs_from_hn_freelancer, profile)
    if offer_source_enabled(profile, "jobspy_contract"):
        gigs += await asyncio.to_thread(gigs_from_jobspy, profile)
    if offer_source_enabled(profile, "boards_contract"):
        gigs += await gigs_from_boards(profile)
    seen, out = set(), []
    for g in gigs:
        key = (g.title.lower().strip(), g.company.lower().strip())
        if key not in seen:
            seen.add(key)
            out.append(g)
    return out


# --------------------------------------------------------------------------
# Prospects (outbound), from the jobs already in the database
# --------------------------------------------------------------------------

def build_prospects(profile: dict, limit: int = 100) -> list:
    """Companies hiring in your stack → one prospect lead each. No network."""
    from utils.discovery import Job
    from utils.tracker import get_db

    cfg = offer_cfg(profile)
    stacks = [s.lower() for s in (cfg.get("prospect_stacks") or []) if str(s).strip()]
    exclude = {str(x).lower().strip() for x in (cfg.get("prospect_exclude") or [])}
    if not stacks:
        return []

    conn = get_db()
    existing = {r["company"].lower().strip() for r in conn.execute(
        "SELECT DISTINCT company FROM applications WHERE kind = 'lead'").fetchall()}
    rows = conn.execute(
        "SELECT company, title, url, apply_url, apply_email, description, discovered_at "
        "FROM applications WHERE kind = 'job' AND status != 'archived' "
        "AND company != '' AND lower(company) != 'unknown' ORDER BY discovered_at DESC"
    ).fetchall()
    conn.close()

    by_company: dict = {}
    for r in rows:
        name = r["company"].strip()
        key = name.lower()
        if key in exclude or key in existing:
            continue
        text = f"{r['title']} {r['description'] or ''}".lower()
        hits = [s for s in stacks if s in text]
        if not hits:
            continue
        c = by_company.setdefault(key, {"name": name, "titles": [], "stacks": set(), "urls": [],
                                        "email": "", "sample": "", "latest": r["discovered_at"]})
        c["titles"].append(r["title"])
        c["stacks"].update(hits)
        c["urls"].append(r["apply_url"] or r["url"] or "")
        if r["apply_email"] and not c["email"]:
            c["email"] = r["apply_email"]
        if not c["sample"] and r["description"]:
            c["sample"] = r["description"][:1500]

    prospects = []
    for key, c in sorted(by_company.items(), key=lambda kv: -len(kv[1]["titles"]))[:limit]:
        titles = list(dict.fromkeys(c["titles"]))
        n = len(c["titles"])
        headline = ", ".join(titles[:2]) + (f" +{n - 2} more" if n > 2 else "")
        url = next((u for u in c["urls"] if u), "")
        desc = (f"{c['name']} has {n} open role(s) in your stack ({', '.join(sorted(c['stacks']))}): "
                f"{'; '.join(titles[:6])}.\n\nSample posting:\n{c['sample']}")
        prospects.append(_as_lead(Job(
            id=f"prospect_{re.sub(r'[^a-z0-9]+', '-', key)[:48]}",
            title=f"Prospect: hiring {headline}", company=c["name"], location="",
            url=url, apply_url=url, platform="prospect", description=desc, department="",
            metadata={"source": "prospects_from_jobs", "apply_email": c["email"] or None},
        ), "prospect", roles_hiring=titles[:10], postings=n, careers_url=url, stacks=sorted(c["stacks"])))
    print(f"  Prospects: {len(prospects)} companies hiring in your stack")
    return prospects
