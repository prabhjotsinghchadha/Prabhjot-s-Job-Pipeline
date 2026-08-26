"""
Job Discovery — Scrape job listings from ATS platforms and job boards.
Supports: Greenhouse, Lever, JobSpy (Indeed/LinkedIn/Glassdoor/ZipRecruiter/Google),
RSS feeds (RemoteOK), and custom career page scraping.
"""

import asyncio
import json
import re
from dataclasses import dataclass, asdict, field
from typing import Optional
from playwright.async_api import async_playwright, Page


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    apply_url: str
    platform: str  # "greenhouse" | "lever" | "linkedin" | "jobspy_*" | "remoteok" | "career_page"
    description: str = ""
    department: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


# Every toggleable discovery source. The dashboard renders this catalog and the
# per-source on/off switches live in profile.yaml under `sources:` (missing key
# means enabled, so existing profiles keep discovering from everything).
SOURCE_REGISTRY = [
    {"key": "greenhouse", "label": "Greenhouse Boards",
     "description": "Target company boards via the Greenhouse API (configure slugs under Target Boards)"},
    {"key": "lever", "label": "Lever Boards",
     "description": "Target company boards via the Lever API (configure slugs under Target Boards)"},
    {"key": "jobspy", "label": "Indeed / LinkedIn / Google",
     "description": "Keyword search across major job boards via JobSpy"},
    {"key": "remoteok", "label": "RemoteOK",
     "description": "Remote-first tech jobs from remoteok.com"},
    {"key": "yc_jobs", "label": "Y Combinator Jobs",
     "description": "YC startup roles from ycombinator.com/jobs"},
    {"key": "remotive", "label": "Remotive",
     "description": "Remote software jobs from remotive.com"},
    {"key": "himalayas", "label": "Himalayas",
     "description": "Remote jobs with salary data from himalayas.app"},
    {"key": "arbeitnow", "label": "Arbeitnow",
     "description": "EU-heavy job board from arbeitnow.com"},
    {"key": "weworkremotely", "label": "WeWorkRemotely",
     "description": "Remote programming jobs from weworkremotely.com"},
    {"key": "web3career", "label": "web3.career",
     "description": "Web3 and blockchain roles from web3.career"},
    {"key": "adzuna", "label": "Adzuna",
     "description": "Adzuna aggregator API (needs a free API key)"},
    {"key": "hn", "label": "HN Who is Hiring",
     "description": "Monthly Hacker News hiring thread via Algolia"},
    {"key": "career_pages", "label": "Custom Career Pages",
     "description": "Any website URL you add, scraped with Playwright + AI"},
]

# Sources implemented inside utils/startup_source.py — the module runs when any
# of these is enabled and skips the disabled ones internally.
STARTUP_BOARD_KEYS = ["yc_jobs", "remotive", "himalayas", "arbeitnow",
                      "weworkremotely", "web3career"]


def source_enabled(profile: dict, key: str) -> bool:
    """True unless profile.yaml explicitly disables the source (sources.<key>: false)."""
    sources = profile.get("sources") or {}
    return sources.get(key) is not False


def deduplicate_jobs(jobs: list) -> list:
    """Deduplicate jobs by (title_lower, company_lower) to avoid cross-source duplicates."""
    seen = set()
    unique = []
    for job in jobs:
        key = (job.title.lower().strip(), job.company.lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


async def discover_greenhouse_jobs(company_slug: str, role_keywords: list[str]) -> list[Job]:
    """
    Scrape jobs from a Greenhouse board.
    URL pattern: https://boards.greenhouse.io/{company_slug}
    API pattern: https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs

    NOTE: We do LOOSE filtering here — check title AND description against
    role keywords AND skill keywords. The AI scoring engine makes the real
    relevance decision later. Better to surface too many jobs than miss good ones.
    """
    import httpx

    jobs = []
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()

        for job_data in data.get("jobs", []):
            title = job_data.get("title", "")

            # Loose filter: check title AND description for any role or skill keyword
            # This catches "SDE", "Software Developer", "Platform Eng" etc.
            title_lower = title.lower()
            raw_desc = job_data.get("content", "")
            desc_lower = re.sub(r'<[^>]+>', ' ', raw_desc).lower()
            combined = f"{title_lower} {desc_lower}"

            # Build broad keyword list: roles + any extra keywords from profile
            broad_keywords = [kw.lower() for kw in role_keywords]
            # Also match on common tech role stems
            broad_keywords.extend([
                "engineer", "developer", "architect", "sre", "devops",
                "sde", "sse", "staff", "principal", "lead",
            ])
            # Deduplicate
            broad_keywords = list(set(broad_keywords))

            if not any(kw in combined for kw in broad_keywords):
                continue

            location = job_data.get("location", {}).get("name", "Unknown")

            # Strip HTML from description
            raw_desc = job_data.get("content", "")
            description = re.sub(r'<[^>]+>', ' ', raw_desc)
            description = re.sub(r'\s+', ' ', description).strip()

            job = Job(
                id=str(job_data["id"]),
                title=title,
                company=company_slug,
                location=location,
                url=f"https://boards.greenhouse.io/{company_slug}/jobs/{job_data['id']}",
                apply_url=f"https://boards.greenhouse.io/{company_slug}/jobs/{job_data['id']}#app",
                platform="greenhouse",
                description=description[:5000],
                department=", ".join(
                    d.get("name", "") for d in job_data.get("departments", [])
                ),
                metadata={
                    "updated_at": job_data.get("updated_at", ""),
                    "requisition_id": job_data.get("requisition_id", ""),
                }
            )
            jobs.append(job)

    except Exception as e:
        print(f"  ⚠ Greenhouse [{company_slug}]: {e}")

    return jobs


async def discover_lever_jobs(company_slug: str, role_keywords: list[str]) -> list[Job]:
    """
    Scrape jobs from a Lever board.
    API pattern: https://api.lever.co/v0/postings/{company_slug}

    NOTE: Loose filtering — let the AI scoring decide relevance.
    """
    import httpx

    jobs = []
    api_url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(api_url)
            resp.raise_for_status()
            data = resp.json()

        for posting in data:
            title = posting.get("text", "")

            # Loose filter: check title AND description
            title_lower = title.lower()
            desc_lower = posting.get("descriptionPlain", "").lower()
            combined = f"{title_lower} {desc_lower}"

            broad_keywords = [kw.lower() for kw in role_keywords]
            broad_keywords.extend([
                "engineer", "developer", "architect", "sre", "devops",
                "sde", "sse", "staff", "principal", "lead",
            ])
            broad_keywords = list(set(broad_keywords))

            if not any(kw in combined for kw in broad_keywords):
                continue

            categories = posting.get("categories", {})
            location = categories.get("location", "Unknown")
            description = posting.get("descriptionPlain", "")

            job = Job(
                id=posting["id"],
                title=title,
                company=company_slug,
                location=location,
                url=posting.get("hostedUrl", ""),
                apply_url=posting.get("applyUrl", posting.get("hostedUrl", "")),
                platform="lever",
                description=description[:5000],
                department=categories.get("team", ""),
                metadata={
                    "commitment": categories.get("commitment", ""),
                    "created_at": posting.get("createdAt", ""),
                }
            )
            jobs.append(job)

    except Exception as e:
        print(f"  ⚠ Lever [{company_slug}]: {e}")

    return jobs


async def discover_all_jobs(profile: dict, on_source_jobs=None) -> list[Job]:
    """
    Discover jobs from all configured sources in profile.yaml.
    Runs enabled sources: greenhouse, lever, jobspy, rss, career_pages.
    Deduplicates results across sources.

    on_source_jobs: optional async callback (source_name, jobs) invoked as each
    source finishes, so callers can persist results incrementally instead of
    waiting the full multi-minute run. A callback failure never aborts the run.
    """
    all_jobs = []
    role_keywords = profile["preferences"]["roles"]
    boards = profile.get("target_boards", {})

    async def _notify(source: str, jobs: list) -> None:
        if on_source_jobs and jobs:
            try:
                await on_source_jobs(source, jobs)
            except Exception as e:
                print(f"  ⚠ incremental save failed for {source}: {e}")

    # Greenhouse boards
    gh_companies = boards.get("greenhouse", []) if source_enabled(profile, "greenhouse") else []
    if gh_companies:
        print(f"\n🌿 Scanning {len(gh_companies)} Greenhouse boards...")
        tasks = [discover_greenhouse_jobs(slug, role_keywords) for slug in gh_companies]
        results = await asyncio.gather(*tasks)
        gh_jobs = []
        for jobs in results:
            gh_jobs.extend(jobs)
            if jobs:
                print(f"   ✅ {jobs[0].company}: {len(jobs)} matching jobs")
        all_jobs.extend(gh_jobs)
        await _notify("greenhouse", gh_jobs)

    # Lever boards
    lever_companies = boards.get("lever", []) if source_enabled(profile, "lever") else []
    if lever_companies:
        print(f"\n🔧 Scanning {len(lever_companies)} Lever boards...")
        tasks = [discover_lever_jobs(slug, role_keywords) for slug in lever_companies]
        results = await asyncio.gather(*tasks)
        lever_jobs = []
        for jobs in results:
            lever_jobs.extend(jobs)
            if jobs:
                print(f"   ✅ {jobs[0].company}: {len(jobs)} matching jobs")
        all_jobs.extend(lever_jobs)
        await _notify("lever", lever_jobs)

    # JobSpy — keyword search across Indeed, LinkedIn, Glassdoor, etc.
    # Respects both the sources toggle and the legacy search.enabled flag.
    search_config = profile.get("search", {})
    if source_enabled(profile, "jobspy") and search_config.get("enabled", True):
        try:
            from utils.jobspy_source import discover_jobspy_jobs
            print(f"\n🔍 Searching job boards via JobSpy...")
            # Blocking network I/O — off the event loop so the dashboard, WebSocket
            # feed, and healthcheck stay live during a multi-minute scrape.
            jobspy_jobs = await asyncio.to_thread(discover_jobspy_jobs, profile)
            all_jobs.extend(jobspy_jobs)
            await _notify("jobspy", jobspy_jobs)
        except Exception as e:
            print(f"  ⚠ JobSpy search failed: {e}")

    # RSS feeds — RemoteOK, etc.
    if source_enabled(profile, "remoteok"):
        try:
            from utils.rss_source import discover_rss_jobs
            print(f"\n📡 Checking RSS feeds...")
            rss_jobs = await asyncio.to_thread(discover_rss_jobs, profile)
            all_jobs.extend(rss_jobs)
            await _notify("rss", rss_jobs)
        except Exception as e:
            print(f"  ⚠ RSS feeds failed: {e}")

    # Startup & niche boards — YC Jobs, Remotive, Himalayas, Arbeitnow, WWR, web3.career
    # The module itself skips whichever boards are toggled off.
    if any(source_enabled(profile, key) for key in STARTUP_BOARD_KEYS):
        try:
            from utils.startup_source import discover_startup_jobs
            print(f"\n🚀 Checking startup & niche boards...")
            startup_jobs = await asyncio.to_thread(discover_startup_jobs, profile)
            all_jobs.extend(startup_jobs)
            await _notify("startup_boards", startup_jobs)
        except Exception as e:
            print(f"  ⚠ Startup boards failed: {e}")

    # Adzuna API
    if source_enabled(profile, "adzuna"):
        try:
            from utils.adzuna_source import discover_adzuna_jobs
            print(f"\n📊 Searching Adzuna...")
            adzuna_jobs = await asyncio.to_thread(discover_adzuna_jobs, profile)
            all_jobs.extend(adzuna_jobs)
            await _notify("adzuna", adzuna_jobs)
        except Exception as e:
            print(f"  ⚠ Adzuna failed: {e}")

    # HN Who is Hiring
    if source_enabled(profile, "hn"):
        try:
            from utils.hn_source import discover_hn_jobs
            print(f"\n📰 Checking HN Who is Hiring...")
            hn_jobs = await asyncio.to_thread(discover_hn_jobs, profile)
            all_jobs.extend(hn_jobs)
            await _notify("hn", hn_jobs)
        except Exception as e:
            print(f"  ⚠ HN Who is Hiring failed: {e}")

    # Custom career pages
    if source_enabled(profile, "career_pages") and profile.get("custom_career_pages"):
        try:
            from utils.career_page_source import discover_career_page_jobs
            print(f"\n🌐 Scraping custom career pages...")
            career_jobs = await discover_career_page_jobs(profile)
            all_jobs.extend(career_jobs)
            await _notify("career_pages", career_jobs)
        except Exception as e:
            print(f"  ⚠ Career page scraping failed: {e}")

    # Deduplicate across sources
    before = len(all_jobs)
    all_jobs = deduplicate_jobs(all_jobs)
    if before != len(all_jobs):
        print(f"\n🔄 Deduplicated: {before} -> {len(all_jobs)} unique jobs")

    print(f"\n📊 Total: {len(all_jobs)} matching jobs found")
    return all_jobs
