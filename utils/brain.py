"""
Claude Code CLI brain — every AI call in the app goes through here.

Shells out to `claude -p --output-format json`, which uses the Pro/Max
subscription instead of API credits. The subprocess MUST run without the
CLAUDECODE env var or a nested-session error is raised.
"""

import json
import os
import re
import subprocess
from typing import Optional


class ClaudeBrain:
    def __init__(self, verbose: bool = True, profile: dict = None, timeout: int = 120):
        self.verbose = verbose
        self.profile = profile or {}
        self.timeout = timeout
        self._verify_cli()

    # ------------------------------------------------------------------ core

    def _verify_cli(self):
        try:
            result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        except FileNotFoundError:
            raise RuntimeError(
                "Claude CLI not found. Install with:\n"
                "  npm install -g @anthropic-ai/claude-code\nThen run: claude auth"
            )
        if result.returncode != 0:
            raise RuntimeError("Claude CLI not responding. Run `claude auth` and try again.")
        if self.verbose:
            print(f"  Claude CLI ready: {result.stdout.strip()}")

    def ask(self, prompt: str, timeout: Optional[int] = None) -> str:
        """Send a prompt, return the text response."""
        if self.verbose:
            print(f"  AI: {prompt[:80].replace(chr(10), ' ')}...")
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json"],
            input=prompt, capture_output=True, text=True,
            timeout=timeout or self.timeout, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr.strip() or 'unknown error'}")
        try:
            return json.loads(result.stdout).get("result", result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()

    def ask_json(self, prompt: str, timeout: Optional[int] = None):
        """Send a prompt, parse the JSON it returns (object or array)."""
        raw = self.ask(
            prompt + "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown fencing, no preamble.",
            timeout=timeout,
        )
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            if self.verbose:
                print(f"  Warning: JSON parse failed:\n{raw[:500]}")
            raise ValueError(f"Claude did not return valid JSON: {e}")

    # --------------------------------------------------------------- prompts

    def _applicant_block(self, profile: dict, resume_text: str) -> str:
        prefs = profile.get("preferences", {})
        skills = profile.get("skills", {})
        primary = ", ".join(skills.get("primary", prefs.get("keywords", [])))
        secondary = ", ".join(skills.get("secondary", []))
        favorites = ", ".join(profile.get("favorite_companies", []))
        return f"""APPLICANT PROFILE:
- Roles sought: {', '.join(prefs.get('roles', []))}
- Primary skills: {primary}
- Secondary skills: {secondary}
- Location preference: {', '.join(prefs.get('locations', []))}
- Remote only: {prefs.get('remote_only', False)}
- Ideal job: {profile.get('ideal_job_description', '')[:500]}
- Favorite companies (bonus +10 if match): {favorites}

APPLICANT RESUME:
{resume_text[:4000] if resume_text else '(No resume provided)'}"""

    _SCORING_RULES = """Scoring guidelines (be critical and honest):
- 90-100: Perfect match (role, skills, location all align)
- 70-89: Strong match (most criteria met)
- 50-69: Partial match (some gaps but worth considering)
- Below 50: Poor match
- Penalise hard conflicts heavily: on-site when the applicant is remote-only,
  citizenship/visa requirements the applicant cannot meet, wrong seniority.
- If the company is in the favorites list, add 10 bonus points (max 100)."""

    def match_job(self, job_description: str, profile: dict, resume_text: str = "") -> dict:
        """Score ONE posting. Returns {score, reasoning, skill_overlap, missing_skills, red_flags}."""
        prompt = f"""You are a job matching engine.

{self._applicant_block(profile, resume_text)}

JOB POSTING:
{job_description[:8000]}

Return this exact JSON structure:
{{
  "score": <integer 0-100>,
  "reasoning": "<one sentence explaining the score>",
  "skill_overlap": ["<matching skills>"],
  "missing_skills": ["<skills they want that applicant lacks>"],
  "red_flags": ["<any concerns>"]
}}

{self._SCORING_RULES}"""
        result = self.ask_json(prompt)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result

    def match_jobs(self, jobs: list, profile: dict, resume_text: str = "",
                   per_job_chars: int = 3000) -> dict:
        """
        Score SEVERAL postings in one call. `jobs` is a list of dicts with
        id, title, company, location, description. Returns {job_id: result}.
        Jobs missing from the response are simply absent from the dict.
        """
        blocks = []
        for j in jobs:
            desc = (j.get("description") or "").strip() or "(no description available)"
            blocks.append(
                f"--- JOB id={j['id']} ---\n"
                f"Title: {j.get('title', '')}\nCompany: {j.get('company', '')}\n"
                f"Location: {j.get('location', '')}\n{desc[:per_job_chars]}"
            )
        prompt = f"""You are a job matching engine. Score each posting below independently.

{self._applicant_block(profile, resume_text)}

JOB POSTINGS ({len(jobs)}):
{chr(10).join(blocks)}

Return this exact JSON structure, one entry per posting, same ids as given:
{{
  "results": [
    {{
      "id": "<job id>",
      "score": <integer 0-100>,
      "reasoning": "<one sentence explaining the score>",
      "skill_overlap": ["<matching skills>"],
      "missing_skills": ["<skills they want that applicant lacks>"],
      "red_flags": ["<any concerns>"]
    }}
  ]
}}

{self._SCORING_RULES}"""
        result = self.ask_json(prompt, timeout=max(self.timeout, 60 + 20 * len(jobs)))
        entries = result.get("results", []) if isinstance(result, dict) else result
        wanted = {str(j["id"]) for j in jobs}
        out = {}
        for e in entries or []:
            if isinstance(e, dict) and str(e.get("id")) in wanted and "score" in e:
                out[str(e["id"])] = e
        return out

    def write_cover_letter(self, job_description: str, profile: dict, resume_text: str = "",
                           company: str = "", title: str = "") -> str:
        personal = profile.get("personal", {})
        name = f"{personal.get('first_name', '')} {personal.get('last_name', '')}".strip()
        skills = profile.get("skills", {})
        primary = ", ".join(skills.get("primary", profile.get("preferences", {}).get("keywords", [])))
        heading = f"{title} at {company}" if (title or company) else "the posting below"
        prompt = f"""Write a tailored cover letter for {heading}.

APPLICANT:
- Name: {name}
- Primary skills: {primary}
- Ideal job: {profile.get('ideal_job_description', '')[:500]}

APPLICANT RESUME:
{resume_text[:4000] if resume_text else '(No resume provided)'}

JOB POSTING:
{job_description[:8000]}

Rules:
- 2-3 short paragraphs, plain text, no subject line, no placeholders like [Company].
- Lead with the strongest overlap between the resume and the posting's requirements.
- Only reference experience that is actually in the resume — never invent employers, titles, dates or metrics.
- End with a one-line sign-off using the applicant's name.

Return this exact JSON structure:
{{"cover_letter": "<the letter>"}}"""
        result = self.ask_json(prompt)
        return (result.get("cover_letter") or "").strip() if isinstance(result, dict) else ""
