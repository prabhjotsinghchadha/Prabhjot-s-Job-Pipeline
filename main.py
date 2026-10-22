#!/usr/bin/env python3
"""
Job pipeline CLI.

    python main.py server            # dashboard + scheduler at http://localhost:8080
    python main.py discover          # find new jobs from every enabled source, then score
    python main.py score             # score the unscored backlog (prefilter + batched Claude calls)
    python main.py stats             # pipeline counts
"""

import argparse
import asyncio
import sys

from utils import pipeline
from utils.tracker import print_stats


def require_profile() -> dict:
    profile = pipeline.load_profile()
    if profile is None:
        print("No profile.yaml found. Start the server and fill in Settings, "
              "or copy profile.yaml.example to profile.yaml.")
        sys.exit(1)
    personal = profile.get("personal", {})
    missing = [k for k in ("first_name", "last_name", "email") if not personal.get(k)]
    if missing:
        print(f"profile.yaml is missing: {', '.join(missing)}")
        sys.exit(1)
    return profile


async def cmd_discover(profile: dict, tracks: list = None):
    print("Finding jobs...")
    r = await pipeline.run_discovery(profile, tracks or None)
    print(f"Discovery: {r}")
    print("Scoring...")
    r = await pipeline.run_scoring(profile)
    print(f"Scoring: {r}")
    print_stats()


async def cmd_score(profile: dict):
    r = await pipeline.run_scoring(profile)
    print(f"Scoring: {r}")
    print_stats()


def main():
    parser = argparse.ArgumentParser(description="Job pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    disc = sub.add_parser("discover", help="Find new jobs from every enabled source, then score them")
    disc.add_argument("--track", action="append", default=[],
                      help="Resume track id to search for (repeatable; default: every track)")
    sub.add_parser("score", help="Score the unscored backlog")
    sub.add_parser("stats", help="Show pipeline counts")
    srv = sub.add_parser("server", help="Run the dashboard + scheduler")
    srv.add_argument("--port", type=int, default=8080)
    srv.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.command == "stats":
        print_stats()
    elif args.command == "server":
        from dashboard.app import run_server
        run_server(host=args.host, port=args.port)
    elif args.command == "discover":
        asyncio.run(cmd_discover(require_profile(), args.track))
    elif args.command == "score":
        asyncio.run(cmd_score(require_profile()))


if __name__ == "__main__":
    main()
