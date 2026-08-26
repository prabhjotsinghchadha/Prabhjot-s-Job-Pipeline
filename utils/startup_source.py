"""
Startup & niche job board sources.

Covers the startup-focused boards that allow programmatic access:
  - YC Jobs (ycombinator.com/jobs)   — public server-rendered pages; same inventory
                                        as workatastartup.com without the login wall
  - Remotive                          — public JSON API, remote-only jobs
  - Himalayas                         — public JSON API, remote-only jobs
  - Arbeitnow                         — public JSON API (EU-heavy)
  - WeWorkRemotely                    — public RSS feeds
  - web3.career                       — server-rendered HTML job tables

Deliberately NOT included (verified blocked as of 2026-08):
  - wellfound.com        — Cloudflare challenge + login-walled GraphQL
  - workatastartup.com   — HTTP 406 to non-browser clients; YC Jobs covers it
  - rustjobs.dev         — HTTP 429 rate-limits every path

Each fetcher is independently optional — one failing MUST NOT break the others.
"""

import hashlib
import html as html_lib
import re

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_TIMEOUT = 30


def _job_id(prefix: str, url: str) -> str:
    return f"{prefix}_{hashlib.md5(url.encode()).hexdigest()[:16]}"


def _matches(title: str, keywords: list) -> bool:
    t = title.lower()
    return any(kw in t for kw in keywords)


def _strip_tags(text: str, limit: int = 5000) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()[:limit]


def discover_startup_jobs(profile: dict) -> list:
    """Discover jobs from startup-focused and niche boards."""
    all_jobs = []
    role_keywords = [kw.lower() for kw in profile["preferences"]["roles"]]
    # Same wide-net philosophy as rss_source: titles only, AI scoring filters later
    all_keywords = list(set(
        role_keywords +
        ["engineer", "developer", "architect", "sre", "devops", "sde", "staff", "lead",
         "software", "backend", "frontend", "fullstack", "full-stack", "full stack",
         "platform", "infrastructure", "web3", "blockchain", "solidity", "react",
         "typescript", "node", "rust", "mobile", "ai", "ml"]
    ))
    remote_only = profile["preferences"].get("remote_only", False)
    skill_words = [s.lower() for s in
                   profile.get("skills", {}).get("primary", []) +
                   profile.get("skills", {}).get("secondary", [])]

    from utils.discovery import source_enabled

    sources = [
        ("yc_jobs", "YC Jobs", lambda: _fetch_yc_jobs(all_keywords)),
        ("remotive", "Remotive", lambda: _fetch_remotive(all_keywords)),
        ("himalayas", "Himalayas", lambda: _fetch_himalayas(all_keywords)),
        ("arbeitnow", "Arbeitnow", lambda: _fetch_arbeitnow(all_keywords, remote_only)),
        ("weworkremotely", "WeWorkRemotely", lambda: _fetch_weworkremotely(all_keywords)),
        ("web3career", "web3.career", lambda: _fetch_web3_career(all_keywords, skill_words)),
    ]
    for key, name, fetch in sources:
        if not source_enabled(profile, key):
            continue
        try:
            jobs = fetch()
            all_jobs.extend(jobs)
            print(f"  🚀 {name}: {len(jobs)} matching jobs")
        except Exception as e:
            print(f"  ⚠ {name} failed: {e}")

    return all_jobs


def _fetch_yc_jobs(keywords: list) -> list:
    """Scrape YC's public jobs board (server-rendered)."""
    from utils.discovery import Job
    import httpx

    jobs = []
    seen = set()
    pages = [
        "https://www.ycombinator.com/jobs/role/software-engineer",
        "https://www.ycombinator.com/jobs/role/full-stack-engineer",
        "https://www.ycombinator.com/jobs/role/frontend-engineer",
    ]
    for page in pages:
        try:
            resp = httpx.get(page, headers=_UA, timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except Exception:
            continue
        anchors = re.findall(
            r'<a[^>]*href="(/companies/([^"/]+)/jobs/[^"]+)"[^>]*>(.*?)</a>',
            resp.text, re.S,
        )
        for rel_url, company_slug, inner in anchors:
            url = f"https://www.ycombinator.com{rel_url}"
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(inner, 200)
            if not title or not _matches(title, keywords):
                continue
            company = re.sub(r"-\d+$", "", company_slug).replace("-", " ").title()
            jobs.append(Job(
                id=_job_id("yc", url),
                title=title,
                company=company,
                location="See listing (YC startup)",
                url=url,
                apply_url=url,
                platform="yc_jobs",
                description="",
                metadata={"source": "ycombinator.com/jobs"},
            ))
    return jobs


def _fetch_remotive(keywords: list) -> list:
    """Remotive public API — remote software jobs."""
    from utils.discovery import Job
    import httpx

    resp = httpx.get(
        "https://remotive.com/api/remote-jobs",
        params={"category": "software-dev", "limit": 100},
        headers=_UA, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        title = j.get("title", "")
        if not title or not _matches(title, keywords):
            continue
        url = j.get("url", "")
        if not url:
            continue
        jobs.append(Job(
            id=_job_id("remotive", url),
            title=title,
            company=j.get("company_name", "Unknown"),
            location=j.get("candidate_required_location", "Remote"),
            url=url,
            apply_url=url,
            platform="remotive",
            description=_strip_tags(j.get("description", "")),
            metadata={
                "source": "remotive",
                "salary": j.get("salary", ""),
                "tags": j.get("tags", []),
                "date_posted": j.get("publication_date", ""),
            },
        ))
    return jobs


def _fetch_himalayas(keywords: list) -> list:
    """Himalayas public API — remote jobs with salary + location restrictions."""
    from utils.discovery import Job
    import httpx

    resp = httpx.get(
        "https://himalayas.app/jobs/api",
        params={"limit": 100},
        headers=_UA, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        title = j.get("title", "")
        if not title or not _matches(title, keywords):
            continue
        url = j.get("applicationLink") or j.get("guid") or ""
        if not url:
            continue
        restrictions = j.get("locationRestrictions") or []
        jobs.append(Job(
            id=_job_id("himalayas", url),
            title=title,
            company=j.get("companyName", "Unknown"),
            location=", ".join(restrictions) if restrictions else "Remote (Worldwide)",
            url=url,
            apply_url=url,
            platform="himalayas",
            description=_strip_tags(j.get("description", "")),
            metadata={
                "source": "himalayas",
                "salary_min": j.get("minSalary"),
                "salary_max": j.get("maxSalary"),
                "currency": j.get("currency"),
                "employment_type": j.get("employmentType"),
            },
        ))
    return jobs


def _fetch_arbeitnow(keywords: list, remote_only: bool) -> list:
    """Arbeitnow public API. EU-heavy and mostly onsite, so the remote flag matters."""
    from utils.discovery import Job
    import httpx

    resp = httpx.get(
        "https://www.arbeitnow.com/api/job-board-api",
        headers=_UA, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("data", []):
        if remote_only and not j.get("remote", False):
            continue
        title = j.get("title", "")
        if not title or not _matches(title, keywords):
            continue
        url = j.get("url", "")
        if not url:
            continue
        jobs.append(Job(
            id=_job_id("arbeitnow", url),
            title=title,
            company=j.get("company_name", "Unknown"),
            location=("Remote — " if j.get("remote") else "") + j.get("location", ""),
            url=url,
            apply_url=url,
            platform="arbeitnow",
            description=_strip_tags(j.get("description", "")),
            metadata={"source": "arbeitnow", "tags": j.get("tags", [])},
        ))
    return jobs


def _fetch_weworkremotely(keywords: list) -> list:
    """WeWorkRemotely RSS feeds. Item titles are 'Company: Job Title'."""
    from utils.discovery import Job
    import feedparser

    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    ]
    jobs = []
    seen = set()
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            continue
        for item in feed.entries:
            raw_title = item.get("title", "")
            url = item.get("link", "")
            if not url or url in seen:
                continue
            seen.add(url)
            company, _, title = raw_title.partition(":")
            title = title.strip() or raw_title
            if not _matches(title, keywords):
                continue
            jobs.append(Job(
                id=_job_id("wwr", url),
                title=title,
                company=company.strip() or "Unknown",
                location=item.get("region", "Remote"),
                url=url,
                apply_url=url,
                platform="weworkremotely",
                description=_strip_tags(item.get("summary", "")),
                metadata={"source": "weworkremotely",
                          "date_posted": item.get("published", "")},
            ))
    return jobs


# Tag pages verified to exist; only fetched when the matching skill is in the profile.
_WEB3_TAG_SLUGS = {
    "react": "react-jobs",
    "typescript": "typescript-jobs",
    "rust": "rust-jobs",
    "solidity": "solidity-jobs",
    "python": "python-jobs",
}


def _fetch_web3_career(keywords: list, skill_words: list) -> list:
    """Scrape web3.career tag pages (server-rendered job tables)."""
    from utils.discovery import Job
    import httpx

    pages = ["https://web3.career/remote-jobs"]
    for skill, slug in _WEB3_TAG_SLUGS.items():
        if any(skill in s for s in skill_words):
            pages.append(f"https://web3.career/{slug}")

    jobs = []
    seen = set()
    for page in pages[:4]:  # politeness cap per run
        try:
            resp = httpx.get(page, headers=_UA, timeout=_TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
        except Exception:
            continue
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.S):
            link = re.search(r'href="(/[a-z0-9-]+/\d{4,})"', row)
            title_m = re.search(r"<h2[^>]*>(.*?)</h2>", row, re.S)
            if not link or not title_m:
                continue
            url = f"https://web3.career{link.group(1)}"
            if url in seen:
                continue
            seen.add(url)
            title = _strip_tags(title_m.group(1), 200)
            if not title or not _matches(title, keywords):
                continue
            company_m = re.search(r"<h3[^>]*>(.*?)</h3>", row, re.S)
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            location = ""
            if tds:
                loc_m = re.search(r"📍(.*)", _strip_tags(tds[0], 300))
                if loc_m:
                    location = re.sub(r"\s*,\s*", ", ", loc_m.group(1)).strip()
            salary = _strip_tags(tds[2], 60) if len(tds) > 2 else ""
            tags = _strip_tags(tds[3], 200) if len(tds) > 3 else ""
            jobs.append(Job(
                id=_job_id("web3career", url),
                title=title,
                company=_strip_tags(company_m.group(1), 100) if company_m else "Unknown",
                location=location or "Remote (Web3)",
                url=url,
                apply_url=url,
                platform="web3career",
                description="",
                metadata={"source": "web3.career", "salary": salary, "tags": tags},
            ))
    return jobs
