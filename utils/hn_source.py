"""
Hacker News monthly threads via the Algolia API.

  "Ask HN: Who is hiring?"                  → jobs  (discover_hn_jobs)
  "Ask HN: Freelancer? Seeking freelancer?" → gigs  (used by utils/leads.py)

Both are posted by the `whoishiring` account on the 1st of each month.
"""

import hashlib
import html as html_mod
import re

import httpx

_ALGOLIA = "https://hn.algolia.com/api/v1"
_ATS_DOMAINS = ["greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
                "smartrecruiters.com", "jobvite.com", "icims.com"]
_ROLE_WORDS = ["engineer", "developer", "designer", "manager", "lead", "senior", "junior",
               "architect", "devops", "sre", "data", "ml", "ai", "frontend", "backend",
               "fullstack", "full-stack", "full stack", "platform", "infrastructure", "security"]
_LOCATION_WORDS = ["remote", "onsite", "hybrid", "on-site", "sf", "nyc", "la", "seattle", "austin",
                   "denver", "chicago", "london", "berlin", "toronto", "new york", "san francisco",
                   "los angeles", "boston", "usa", "eu"]
GENERIC_TECH_WORDS = ["engineer", "developer", "architect", "sre", "devops", "sde", "staff", "lead",
                      "software", "backend", "frontend", "fullstack", "platform", "infrastructure",
                      "data", "ml", "ai", "cloud", "security", "systems"]


def find_thread(title_prefix: str) -> tuple:
    """Newest whoishiring story whose title starts with `title_prefix`.
    Returns (thread_id, title, top-level comments). Raises on network errors."""
    resp = httpx.get(f"{_ALGOLIA}/search_by_date",
                     params={"query": title_prefix, "tags": "story,author_whoishiring", "hitsPerPage": 10},
                     timeout=30)
    resp.raise_for_status()
    hits = [h for h in resp.json().get("hits", [])
            if (h.get("title") or "").lower().startswith(title_prefix.lower())]
    if not hits:
        raise RuntimeError(f"no thread titled '{title_prefix}…'")
    thread = hits[0]
    resp = httpx.get(f"{_ALGOLIA}/items/{thread['objectID']}", timeout=30)
    resp.raise_for_status()
    return str(thread["objectID"]), thread.get("title", ""), resp.json().get("children", [])


def clean_comment(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def parse_comment(comment: dict, thread_title: str, keywords: list, extra_meta: dict = None):
    """One top-level comment → Job, or None when no keyword matches."""
    from utils.discovery import Job

    clean_text = clean_comment(comment.get("text", ""))
    if not clean_text:
        return None
    text_lower = clean_text.lower()
    if keywords and not any(kw in text_lower for kw in keywords):
        return None

    # Common format: "Company | Role | Location | Remote | ..."
    parts = [p.strip() for p in clean_text.split("|")]
    company = parts[0][:80] if parts else "Unknown"
    title, location = "See posting", "See posting"
    for part in parts[1:]:
        pl = part.lower()
        if part.startswith("http") or len(part) > 120:
            continue
        if any(kw in pl for kw in _ROLE_WORDS):
            title = part[:120]
        elif any(kw in pl for kw in _LOCATION_WORDS):
            location = part[:100]
        elif title == "See posting" and len(part) < 80:
            title = part[:120]

    comment_id = str(comment.get("id", ""))
    hn_url = f"https://news.ycombinator.com/item?id={comment_id}"
    urls = [u.rstrip(".,;:!?)") for u in re.findall(r'https?://[^\s<>"\')\],;]+', clean_text) if len(u) > 10]
    apply_url = hn_url
    for u in urls:
        if any(d in u.lower() for d in _ATS_DOMAINS):
            apply_url = u
            break
    else:
        for u in urls:
            if any(k in u.lower() for k in ("careers", "jobs", "apply", "hiring")):
                apply_url = u
                break
        else:
            if urls:
                apply_url = urls[0]
    email = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", clean_text)

    meta = {"source": "hackernews", "thread_title": thread_title,
            "date_posted": comment.get("created_at", ""),
            "apply_email": email.group(0) if email else None}
    meta.update(extra_meta or {})
    return Job(
        id=f"hn_{hashlib.md5(hn_url.encode()).hexdigest()[:16]}",
        title=title, company=company, location=location, url=hn_url, apply_url=apply_url,
        platform="hackernews", description=clean_text[:5000], department="", metadata=meta,
    )


def discover_hn_jobs(profile: dict) -> list:
    """Scrape the newest 'Who is hiring?' thread for matching jobs."""
    role_keywords = [kw.lower() for kw in profile["preferences"]["roles"]]
    keywords = list(set(role_keywords + GENERIC_TECH_WORDS))
    jobs = []
    try:
        _, thread_title, children = find_thread("Ask HN: Who is hiring?")
        print(f"  📰 Found: {thread_title} ({len(children)} postings)")
        for comment in children:
            job = parse_comment(comment, thread_title, keywords)
            if job:
                jobs.append(job)
        print(f"  📊 HN Who is Hiring: {len(jobs)} matching jobs")
    except Exception as e:
        print(f"  ⚠ HN Who is Hiring failed: {e}")
    return jobs
