"""
JobSpy integration — Broad keyword-based job search across multiple platforms.
Uses python-jobspy to search Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google.
"""

import traceback
from typing import Optional


import math


def _clean(val, fallback=""):
    """Sanitize a pandas value — convert NaN/None to fallback string."""
    if val is None:
        return fallback
    if isinstance(val, float) and math.isnan(val):
        return fallback
    s = str(val).strip()
    if s.lower() in ("nan", "none", ""):
        return fallback
    return s


# Indeed serves a separate domain per country. Without a country matching the search
# location the scraper silently returns US listings for a UK or AU search.
_INDEED_COUNTRIES = {
    "united states": "usa", "usa": "usa", "us": "usa",
    "united kingdom": "uk", "uk": "uk", "england": "uk", "london": "uk",
    "australia": "australia", "sydney": "australia", "melbourne": "australia",
    "canada": "canada",
    "germany": "germany", "berlin": "germany",
    "netherlands": "netherlands", "amsterdam": "netherlands",
    "ireland": "ireland", "dublin": "ireland",
    "france": "france",
    "spain": "spain",
    "portugal": "portugal",
    "poland": "poland",
    "india": "india",
    "singapore": "singapore",
}


def _indeed_country(location: str) -> str:
    """Map a search location to the country code JobSpy needs for Indeed."""
    loc = (location or "").strip().lower()
    if loc in _INDEED_COUNTRIES:
        return _INDEED_COUNTRIES[loc]
    # Substring pass skips short codes so "us" cannot match inside "australia".
    for name, country in _INDEED_COUNTRIES.items():
        if len(name) > 3 and name in loc:
            return country
    return "usa"


def discover_jobspy_jobs(profile: dict) -> list:
    """
    Search for jobs using python-jobspy across multiple job boards.
    Each source is independently optional — if one fails, others continue.
    """
    from utils.discovery import Job

    search_config = profile.get("search", {})
    queries = search_config.get("queries", profile["preferences"].get("roles", []))
    locations = search_config.get("locations", profile["preferences"].get("locations", ["United States"]))
    distance = search_config.get("distance_miles", 100)  # Wide net — let AI score relevance
    results_wanted = search_config.get("results_per_query", 50)  # More results per query

    # JobSpy resolves each location to a real country and errors out on placeholders
    # like "Remote" or "Worldwide", losing that whole search. Remote filtering is
    # handled by the is_remote flag below, so drop the placeholders instead.
    _PLACEHOLDERS = {"remote", "anywhere", "worldwide", "global", "europe"}
    geo_locations = [loc for loc in locations if loc.strip().lower() not in _PLACEHOLDERS]
    if not geo_locations:
        geo_locations = ["United States"]
    locations = geo_locations

    all_jobs = []

    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  ⚠ python-jobspy not installed. Run: pip install python-jobspy")
        return []

    # Glassdoor and ZipRecruiter return 403 to this traffic every time. Excluded so a
    # run doesn't spend a blocked round-trip per query/location on each of them.
    sites = ["indeed", "linkedin", "google"]

    for query in queries:
        for location in locations:
            print(f"  🔍 Searching: '{query}' in '{location}'...")
            try:
                results = scrape_jobs(
                    site_name=sites,
                    search_term=query,
                    location=location,
                    distance=distance,
                    results_wanted=results_wanted,
                    country_indeed=_indeed_country(location),
                    is_remote=profile["preferences"].get("remote_only", False),
                )

                if results is None or len(results) == 0:
                    continue

                for _, row in results.iterrows():
                    try:
                        title = _clean(row.get("title"), "Untitled")
                        company = _clean(row.get("company_name"), "Unknown")
                        job_location = _clean(row.get("location"), location)
                        job_url = _clean(row.get("job_url"))
                        description = _clean(row.get("description"))
                        site = _clean(row.get("site"), "jobspy")
                        date_posted = _clean(row.get("date_posted"))

                        # Skip garbage entries: no URL or "Unknown" company with no description
                        if not job_url or not job_url.startswith("http"):
                            continue
                        if company == "Unknown" and not description:
                            continue
                        if title == "Untitled":
                            continue

                        # Generate a stable ID from URL or title+company
                        import hashlib
                        job_id = hashlib.md5(
                            (job_url or f"{title}_{company}").encode()
                        ).hexdigest()[:16]

                        # Extract salary info if available
                        salary_min = None
                        salary_max = None
                        try:
                            raw_min = row.get("min_amount")
                            raw_max = row.get("max_amount")
                            if raw_min is not None and not (isinstance(raw_min, float) and math.isnan(raw_min)):
                                salary_min = int(raw_min)
                            if raw_max is not None and not (isinstance(raw_max, float) and math.isnan(raw_max)):
                                salary_max = int(raw_max)
                        except (ValueError, TypeError):
                            pass

                        job = Job(
                            id=f"jobspy_{job_id}",
                            title=title,
                            company=company,
                            location=job_location,
                            url=job_url,
                            apply_url=job_url,
                            platform=f"jobspy_{site}",
                            description=description[:5000],
                            department="",
                            metadata={
                                "source": site,
                                "date_posted": date_posted,
                                "salary_min": salary_min,
                                "salary_max": salary_max,
                            }
                        )
                        all_jobs.append(job)
                    except Exception as e:
                        continue

                print(f"    Found {len(results)} jobs for '{query}' in '{location}'")

            except Exception as e:
                print(f"    ⚠ Search failed for '{query}' in '{location}': {e}")
                continue

    print(f"  📊 JobSpy total: {len(all_jobs)} jobs found")
    return all_jobs
