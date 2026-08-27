#!/usr/bin/env python3
"""
Prabhjot's Pipeline — AI-Powered Job Intelligence
======================================

Uses Claude Code CLI as the AI brain + Playwright for browser automation.

Usage:
    # Discover & review matches (no applications sent)
    python main.py discover

    # Dry run — fill forms but don't submit
    python main.py apply --dry-run

    # Actually apply (use with caution!)
    python main.py apply

    # Apply to a single URL
    python main.py single https://boards.greenhouse.io/company/jobs/12345

    # View stats
    python main.py stats
"""

import asyncio
from utils.browser import headed_supported
import argparse
import os
import random
import sys
import yaml
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:  # bare host install — only `login` explains how to fix
    async_playwright = None

from utils.brain import ClaudeBrain
from utils.discovery import discover_all_jobs
from utils.tracker import (
    is_already_seen, log_discovered, log_matched,
    log_applied, log_skipped, get_today_count, print_stats,
    reset_unscored, delete_all, get_unscored_jobs
)
from adapters.stagehand_adapter import apply_smart


def load_profile(path: str = "profile.yaml") -> dict:
    """Load and validate profile config."""
    p = Path(path)
    if not p.exists():
        print(f"❌ Profile not found: {path}")
        print(f"   Copy profile.yaml.example to profile.yaml and fill it out.")
        sys.exit(1)

    with open(p) as f:
        profile = yaml.safe_load(f)

    # Validate required fields
    personal = profile.get("personal", {})
    required = ["first_name", "last_name", "email"]
    missing = [f for f in required if not personal.get(f)]
    if missing:
        print(f"❌ Missing required fields in profile.yaml: {', '.join(missing)}")
        sys.exit(1)

    # Validate resume exists
    resume = profile.get("resume_path", "")
    if resume and not Path(resume).exists():
        print(f"⚠ Resume not found at: {resume}")
        print(f"  Applications requiring resume upload will fail.")

    return profile


async def cmd_discover(profile: dict):
    """Discover jobs and score them — no applications sent."""
    brain = ClaudeBrain(verbose=True, profile=profile)
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))

    print("\n🔍 Discovering jobs from configured boards...\n")
    jobs = await discover_all_jobs(profile)

    if not jobs:
        print("\n😕 No matching jobs found. Try:")
        print("   - Adding more companies to target_boards in profile.yaml")
        print("   - Broadening role keywords in preferences.roles")
        return

    min_score = profile["preferences"].get("min_match_score", 65)
    matches = []

    print(f"\n🧠 Scoring {len(jobs)} jobs with Claude (min score: {min_score})...\n")

    for i, job in enumerate(jobs):
        # Skip already-seen jobs
        if is_already_seen(job.id):
            print(f"  [{i+1}/{len(jobs)}] ⏭ Already seen: {job.title} @ {job.company}")
            continue

        log_discovered(job)

        print(f"  [{i+1}/{len(jobs)}] 🔍 {job.title} @ {job.company} ({job.location})")

        try:
            result = brain.match_job(job.description, profile, resume_text=resume_text)
            score = result.get("score", 0)
            should_apply = result.get("apply", False)
            reasoning = result.get("reasoning", "")
            cover_letter = result.get("cover_letter", "")

            log_matched(job.id, score, reasoning, cover_letter)

            emoji = "✅" if should_apply else "❌"
            print(f"           {emoji} Score: {score} — {reasoning}")

            if should_apply and score >= min_score:
                matches.append((job, result))
            else:
                log_skipped(job.id, f"Score {score} < {min_score}: {reasoning}")

        except Exception as e:
            print(f"           ⚠ Scoring failed: {e}")

    print(f"\n{'='*60}")
    print(f"📊 Results: {len(matches)} jobs above threshold out of {len(jobs)} scanned")
    print(f"{'='*60}")
    for job, result in matches:
        print(f"\n  🎯 {job.title} @ {job.company}")
        print(f"     Location: {job.location}")
        print(f"     Score: {result['score']}")
        print(f"     URL: {job.apply_url}")
        if result.get("skill_overlap"):
            print(f"     Matching: {', '.join(result['skill_overlap'][:5])}")
        if result.get("red_flags"):
            print(f"     Flags: {', '.join(result['red_flags'])}")

    print_stats()


async def cmd_apply(profile: dict, dry_run: bool = True):
    """Discover, score, and apply to matching jobs."""
    brain = ClaudeBrain(verbose=True, profile=profile)
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))
    rate_limits = profile.get("rate_limits", {})
    max_per_day = rate_limits.get("max_applications_per_day", 25)
    min_delay = rate_limits.get("min_delay_seconds", 60)
    max_delay = rate_limits.get("max_delay_seconds", 180)

    today_count = get_today_count()
    if today_count >= max_per_day:
        print(f"🛑 Daily limit reached ({today_count}/{max_per_day}). Try again tomorrow.")
        return

    # Discover
    print("\n🔍 Discovering jobs...\n")
    jobs = await discover_all_jobs(profile)
    if not jobs:
        print("No matching jobs found.")
        return

    # Score
    min_score = profile["preferences"].get("min_match_score", 65)
    matches = []

    print(f"\n🧠 Scoring {len(jobs)} jobs...\n")
    for job in jobs:
        if is_already_seen(job.id):
            continue
        log_discovered(job)
        try:
            result = brain.match_job(job.description, profile, resume_text=resume_text)
            score = result.get("score", 0)
            log_matched(job.id, score, result.get("reasoning", ""), result.get("cover_letter", ""))
            if result.get("apply") and score >= min_score:
                matches.append((job, result))
                print(f"  ✅ {score}: {job.title} @ {job.company}")
            else:
                log_skipped(job.id, result.get("reasoning", "Low score"))
                print(f"  ❌ {score}: {job.title} @ {job.company}")
        except Exception as e:
            print(f"  ⚠ {job.title} @ {job.company}: {e}")

    if not matches:
        print("\nNo jobs above the match threshold.")
        print_stats()
        return

    # Apply
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'='*60}")
    print(f"🚀 Applying to {len(matches)} jobs [{mode}]")
    print(f"{'='*60}\n")

    async with async_playwright() as p:
        from utils.browser import launch_apply_browser
        page, close_browser = await launch_apply_browser(p)

        for i, (job, result) in enumerate(matches):
            if get_today_count() >= max_per_day:
                print(f"\n🛑 Daily limit reached ({max_per_day}). Stopping.")
                break

            print(f"\n{'─'*50}")
            print(f"[{i+1}/{len(matches)}] {job.title} @ {job.company}")
            print(f"  URL: {job.apply_url}")
            print(f"  Score: {result['score']} — {result.get('reasoning', '')}")

            try:
                cover_letter = result.get("cover_letter", "")

                success = await apply_smart(
                    page, job.apply_url, profile, brain,
                    cover_letter=cover_letter, dry_run=dry_run,
                    platform=job.platform,
                    company=job.company, title=job.title,
                    description=getattr(job, 'description', ''),
                )

                if not dry_run:
                    log_applied(job.id, success)

            except Exception as e:
                print(f"  ❌ Application failed: {e}")
                if not dry_run:
                    log_applied(job.id, False)

            # Rate limiting
            if i < len(matches) - 1:
                delay = random.randint(min_delay, max_delay)
                print(f"  ⏳ Waiting {delay}s before next application...")
                await asyncio.sleep(delay)

        await close_browser()

    print_stats()


async def cmd_single(profile: dict, url: str, dry_run: bool = True):
    """Apply to a single job URL."""
    brain = ClaudeBrain(verbose=True, profile=profile)

    print(f"\n🎯 Single application: {url}")
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"   Mode: {mode}\n")

    async with async_playwright() as p:
        from utils.browser import launch_apply_browser
        page, close_browser = await launch_apply_browser(p)

        await apply_smart(page, url, profile, brain, dry_run=dry_run)

        if dry_run:
            print("\n💡 Browser staying open for review. Press Ctrl+C to exit.")
            try:
                await asyncio.sleep(300)  # Keep browser open 5 min for review
            except KeyboardInterrupt:
                pass

        await close_browser()


async def cmd_login(url: str):
    """
    Open the persistent apply browser HEADED so the user can log in to job
    sites once (LinkedIn, Indeed, Google, Workday accounts, ...). On exit,
    exports a portable session snapshot (Playwright storage_state) that the
    headless/Docker apply browser imports on every launch — cookies can't
    cross the macOS/Linux boundary inside the raw profile, but this file can.
    """
    import subprocess
    from utils.browser import (
        find_chrome_executable, login_profile_dir, login_state_path,
        export_profile_state,
    )

    if not headed_supported():
        print("❌ No display available. Run this on your desktop (macOS), "
              "not inside Docker/SSH.")
        return

    if async_playwright is None:
        print("❌ Playwright is not installed for this Python. One-time setup:")
        print("     python3.11 -m pip install playwright")
        print("     python3.11 -m playwright install chromium")
        return

    chrome = find_chrome_executable()
    if not chrome:
        # No real Chrome — fall back to a Playwright-driven window
        await _cmd_login_playwright_fallback(url)
        return

    profile_dir = login_profile_dir()
    state_path = login_state_path()

    # NATIVE flow: the window the user browses in is plain Chrome with zero
    # automation flags — full speed, sandbox intact, no warning bar, nothing
    # for Google to detect. Playwright only touches the profile headlessly
    # before (seed previous sessions in) and after (export cookies out).
    async with async_playwright() as p:
        if state_path.exists():
            try:
                n = await export_profile_state(p, profile_dir, state_path,
                                               seed_state=True)
                if n:
                    print(f"  🔐 Carried over {n} previously saved session cookie(s)")
            except Exception as e:
                print(f"  ⚠ Could not carry over previous sessions ({e}) — "
                      f"you may need to log in again")

        proc = subprocess.Popen(
            [chrome, f"--user-data-dir={profile_dir}",
             "--no-first-run", "--no-default-browser-check", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        print("\n" + "=" * 60)
        print("🔐 LOGIN BROWSER (your real Chrome — full speed, no flags)")
        print("=" * 60)
        print("Log in to any job sites you need in the window that opened —")
        print("LinkedIn, Indeed, Google, workatastartup.com, Workday, ...")
        print("Open more tabs and sites freely. This window is separate from")
        print("your personal Chrome profile.")
        print("\nWhen you're done:")
        print("\n   >>> come back here and press ENTER to save & exit <<<\n")

        try:
            await asyncio.to_thread(input)
        except (KeyboardInterrupt, EOFError):
            pass

        print("  Closing the login window and exporting sessions...")
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        await asyncio.sleep(1)  # let Chrome finish flushing the profile

        try:
            count = await export_profile_state(p, profile_dir, state_path)
            print(f"\n✅ Saved {count} session cookie(s) to {state_path}")
            print("   Headless/Docker apply sessions import these automatically.")
            print("   Hosted (Railway) instance: upload this file via PROFILE →")
            print("   BROWSER SESSIONS → UPLOAD SESSIONS FILE.")
        except Exception as e:
            print(f"⚠ Could not export session state: {e}")


async def _cmd_login_playwright_fallback(url: str):
    """Login window via Playwright's bundled Chromium (no real Chrome found)."""
    from utils.browser import launch_login_browser, login_state_path

    async with async_playwright() as p:
        try:
            page, close = await launch_login_browser(p)
        except Exception as e:
            print(f"❌ Could not open the login browser: {e}")
            print("   Is another login window already running?")
            return

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass  # slow site — the window is open, which is what matters

        print("\n" + "=" * 60)
        print("🔐 LOGIN BROWSER")
        print("=" * 60)
        print("Log in to any job sites you need in the browser window.")
        print("Google sign-in may refuse this automated window — use the")
        print("site's own email+password login instead.")
        print("\nWhen you're done, keep the window OPEN and:")
        print("\n   >>> come back here and press ENTER to save & exit <<<\n")

        try:
            await asyncio.to_thread(input)
        except (KeyboardInterrupt, EOFError):
            pass

        try:
            state_path = login_state_path()
            state = await page.context.storage_state(path=str(state_path))
            os.chmod(state_path, 0o600)
            print(f"\n✅ Saved {len(state.get('cookies', []))} session cookie(s) "
                  f"to {state_path}")
        except Exception as e:
            print(f"⚠ Could not export session state: {e}")
            print("  (Did you close the browser window? Leave it open and press ENTER instead.)")

        try:
            await close()
        except Exception:
            pass


def cmd_reset():
    """Delete all tracked jobs for a fresh start."""
    count = delete_all()
    print(f"Deleted {count} jobs. Database is clean.")


async def cmd_rescore(profile: dict):
    """Re-score all unscored jobs."""
    import httpx
    import re as _re
    brain = ClaudeBrain(verbose=True, profile=profile)
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))
    unscored = get_unscored_jobs()

    if not unscored:
        print("No unscored jobs found.")
        return

    min_score = profile["preferences"].get("min_match_score", 65)
    print(f"\nRe-scoring {len(unscored)} unscored jobs...\n")

    for i, job_row in enumerate(unscored):
        print(f"  [{i+1}/{len(unscored)}] {job_row['title']} @ {job_row['company']}")
        try:
            desc = ""
            if job_row['platform'] == 'greenhouse':
                url = (
                    f"https://boards-api.greenhouse.io/v1/boards/"
                    f"{job_row['company']}/jobs/{job_row['id']}?content=true"
                )
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw = data.get("content", "")
                        desc = _re.sub(r'<[^>]+>', ' ', raw)
                        desc = _re.sub(r'\s+', ' ', desc).strip()[:5000]
            elif job_row['platform'] == 'lever':
                url = (
                    f"https://api.lever.co/v0/postings/"
                    f"{job_row['company']}/{job_row['id']}"
                )
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        desc = data.get("descriptionPlain", "")[:5000]

            if not desc:
                desc = (
                    f"Job: {job_row['title']} at {job_row['company']}. "
                    f"Location: {job_row['location']}"
                )

            result = brain.match_job(desc, profile, resume_text=resume_text)
            score = result.get("score", 0)
            reasoning = result.get("reasoning", "")
            cover_letter = result.get("cover_letter", "")

            log_matched(job_row['id'], score, reasoning, cover_letter)

            emoji = "✅" if score >= min_score else "❌"
            print(f"           {emoji} Score: {score} — {reasoning}")

            if score < min_score:
                log_skipped(job_row['id'], f"Score {score} < {min_score}: {reasoning}")

        except Exception as e:
            print(f"           ⚠ Scoring failed: {e}")

    print_stats()


def main():
    parser = argparse.ArgumentParser(
        description="Prabhjot's Pipeline — AI-Powered Job Intelligence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py discover                          # Find & score jobs
  python main.py apply --dry-run                   # Fill forms, don't submit
  python main.py apply                             # Actually submit applications
  python main.py single https://boards.greenhouse.io/company/jobs/123
  python main.py single https://jobs.lever.co/company/abc --live
  python main.py stats                             # View application stats
        """
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # discover
    subparsers.add_parser("discover", help="Discover and score jobs (no applications)")

    # apply
    apply_parser = subparsers.add_parser("apply", help="Discover, score, and apply")
    apply_parser.add_argument("--dry-run", action="store_true", default=True,
                              help="Fill forms but don't submit (default)")
    apply_parser.add_argument("--live", action="store_true",
                              help="Actually submit applications")

    # single
    single_parser = subparsers.add_parser("single", help="Apply to a single URL")
    single_parser.add_argument("url", help="Job posting URL")
    single_parser.add_argument("--live", action="store_true",
                               help="Actually submit (default: dry run)")

    # login
    login_parser = subparsers.add_parser(
        "login",
        help="Open a browser to log in to job sites once — sessions are "
             "saved and reused by every apply run (incl. Docker)")
    login_parser.add_argument(
        "url", nargs="?", default="https://www.linkedin.com/login",
        help="Site to open first (default: LinkedIn login)")

    # stats
    subparsers.add_parser("stats", help="View application stats")

    # reset
    subparsers.add_parser("reset", help="Delete all jobs and start fresh")

    # rescore
    subparsers.add_parser("rescore", help="Re-score all unscored jobs")

    # server
    server_parser = subparsers.add_parser("server", help="Launch web dashboard")
    server_parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    server_parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")

    args = parser.parse_args()

    if args.command == "stats":
        print_stats()
        return

    if args.command == "reset":
        cmd_reset()
        return

    if args.command == "login":
        asyncio.run(cmd_login(args.url))
        return

    if args.command == "server":
        # The server must boot without profile.yaml — on a fresh deploy the
        # dashboard's first-run wizard creates it. The dashboard reads the
        # profile per-request and the scheduler tolerates its absence.
        from dashboard.server import run_server
        try:
            from scheduler import setup_scheduler
            setup_scheduler()  # Configures jobs; actual start happens in FastAPI lifespan
        except Exception as e:
            print(f"  Scheduler setup warning: {e}")
        run_server(host=args.host, port=args.port)
        return

    profile = load_profile()

    if args.command == "discover":
        asyncio.run(cmd_discover(profile))
    elif args.command == "apply":
        dry_run = not args.live
        asyncio.run(cmd_apply(profile, dry_run=dry_run))
    elif args.command == "single":
        dry_run = not args.live
        asyncio.run(cmd_single(profile, args.url, dry_run=dry_run))
    elif args.command == "rescore":
        asyncio.run(cmd_rescore(profile))


if __name__ == "__main__":
    main()
