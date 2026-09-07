"""
AI Resume Tailoring — Generates tailored resume content per job posting.

Two modes, chosen by what the profile carries:

  Structured (profile.yaml has a `resume:` section — see utils/resume_builder):
    The model receives the candidate's actual roles and bullets and returns
    per-role rewrites, a re-ordered skills block, a job-specific headline and
    summary. utils/resume_builder overlays that on the base data and renders a
    real PDF/DOCX, so the file the adapters upload is tailored, not just the
    notes in the dashboard. Output is validated against the base data (index
    range, bullet count) and ATS-scored with utils/ats_checker.

  Legacy (only an uploaded PDF): summary + free-floating bullets + cover
    letter, as before.

Rule that both modes enforce in the prompt: never invent employers, titles,
dates, degrees or metrics. Tailoring rephrases and re-prioritises — it does
not fabricate.
"""

import json
import re

from utils.resume_builder import (
    apply_tailoring,
    has_structured_resume,
    render_text,
    resume_from_profile,
)


def _norm_skill(s) -> str:
    return re.sub(r"[^a-z0-9+#]", "", str(s or "").lower())


def skill_pool(profile: dict, base: dict = None) -> list[str]:
    """
    Every skill the candidate has claimed anywhere — resume skill groups,
    profile skills.primary / secondary, preference keywords. Tailoring may
    surface any of these when a posting asks; nothing outside the pool is
    ever added without the user claiming it first.
    """
    base = base or resume_from_profile(profile)
    items: list = []
    for g in base["skills"]:
        items += g["items"]
    sk = profile.get("skills") or {}
    items += list(sk.get("primary") or []) + list(sk.get("secondary") or [])
    items += list((profile.get("preferences") or {}).get("keywords") or [])
    seen, out = set(), []
    for i in items:
        k = _norm_skill(i)
        if k and k not in seen:
            seen.add(k)
            out.append(str(i).strip())
    return out


def _base_text(base: dict) -> str:
    parts = [base.get("headline", ""), base.get("summary", "")]
    for e in base["experience"]:
        parts += [e.get("tagline", ""), *e["bullets"]]
    for p in base["projects"]:
        parts += [p.get("description", ""), *p["tech"], *p["bullets"]]
    return "\n".join(parts)

DEFAULTS = {
    "tailored_headline": "",
    "tailored_summary": "",
    "tailored_bullets": [],
    "tailored_experience": [],
    "tailored_skills": [],
    "project_order": [],
    "emphasis_areas": [],
    "keywords_to_include": [],
    "suggested_skills": [],
    "deemphasize": [],
    "tailored_cover_letter": "",
}


def _with_defaults(result: dict) -> dict:
    for key, default in DEFAULTS.items():
        if key not in result or result[key] is None:
            result[key] = json.loads(json.dumps(default))
    return result


def _error(msg: str) -> dict:
    out = _with_defaults({})
    out["error"] = msg
    return out


def _validate_structured(result: dict, base: dict, pool: list = None) -> dict:
    """Drop anything the model returned that doesn't line up with the base resume."""
    pool = pool if pool is not None else [i for g in base["skills"] for i in g["items"]]
    pool_norms = {_norm_skill(p) for p in pool}
    from utils.ats_checker import _phrase_rx, canonical
    base_text = _base_text(base)

    def _has_skill(item: str) -> bool:
        n = _norm_skill(item)
        if n in pool_norms or _norm_skill(canonical(item)) in pool_norms:
            return True
        try:
            return bool(_phrase_rx(item).search(base_text))
        except re.error:
            return False

    exp = []
    for item in result.get("tailored_experience") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(base["experience"])):
            continue
        bullets = [str(b).strip() for b in (item.get("bullets") or []) if str(b).strip()]
        if not bullets:
            continue
        # Never let a role grow beyond its original bullet count + 1 — that is
        # where invented achievements creep in.
        max_len = len(base["experience"][idx]["bullets"]) + 1
        exp.append({"index": idx, "bullets": bullets[:max_len]})
    result["tailored_experience"] = exp

    # Skills: only what the candidate has claimed (pool) or evidenced in
    # bullets survives; anything else becomes a suggestion for the user.
    skills, unclaimed = [], []
    for g in result.get("tailored_skills") or []:
        if isinstance(g, dict):
            items = [str(i).strip() for i in (g.get("items") or []) if str(i).strip()]
            kept = [i for i in items if _has_skill(i)]
            unclaimed += [i for i in items if i not in kept]
            if kept:
                skills.append({"category": str(g.get("category") or "").strip(), "items": kept})
    result["tailored_skills"] = skills

    suggested, seen = [], set()
    for i in [*unclaimed, *(result.get("suggested_skills") or [])]:
        i = str(i).strip()
        n = _norm_skill(i)
        if i and n and n not in seen and not _has_skill(i):
            seen.add(n)
            suggested.append(i)
    result["suggested_skills"] = suggested[:15]

    order = result.get("project_order")
    result["project_order"] = [i for i in order if isinstance(i, int)] if isinstance(order, list) else []

    # Keep the legacy field populated for the dashboard's "Key Bullets" panel.
    if not result.get("tailored_bullets") and exp:
        result["tailored_bullets"] = exp[0]["bullets"][:6]
    return result


def _structured_prompt(job_description: str, base: dict, profile: dict,
                       pool: list = None, notes: str = "") -> str:
    roles = profile.get("preferences", {}).get("roles", [])
    pool = pool if pool is not None else skill_pool(profile, base)
    notes_block = f"\nEXTRA CONTEXT FROM THE CANDIDATE (honour it):\n{notes.strip()[:1500]}\n" if notes.strip() else ""
    exp_json = json.dumps(
        [
            {
                "index": i,
                "title": e["title"],
                "company": e["company"],
                "dates": f"{e['start']} – {e['end']}",
                "bullets": e["bullets"],
            }
            for i, e in enumerate(base["experience"])
        ],
        indent=1,
    )
    skills_json = json.dumps(base["skills"], indent=1)
    projects_json = json.dumps(
        [{"index": i, "name": p["name"], "description": p["description"], "tech": p["tech"]}
         for i, p in enumerate(base["projects"])],
        indent=1,
    )

    return f"""You are an expert resume writer who optimises resumes for applicant-tracking systems (ATS) and hiring managers. Tailor this candidate's resume to ONE specific job posting.

CANDIDATE HEADLINE: {base['headline']}
CANDIDATE SUMMARY: {base['summary']}
CANDIDATE TARGET ROLES: {', '.join(roles)}

CANDIDATE SKILLS (grouped, as currently on the resume):
{skills_json}

CANDIDATE SKILL POOL (everything the candidate has claimed anywhere — you MAY add any of these to the resume when the posting asks for them):
{json.dumps(pool)}
{notes_block}
CANDIDATE EXPERIENCE (index, title, company, dates, current bullets):
{exp_json}

CANDIDATE PROJECTS:
{projects_json}

JOB POSTING:
{job_description[:7000]}

Return a JSON object:
{{
  "tailored_headline": "<one line, the job's title language applied to the candidate's real level, e.g. 'Senior Full Stack Engineer · TypeScript / Next.js / Node.js'>",
  "tailored_summary": "<2-3 sentences. Lead with the candidate's most relevant strengths for THIS posting, mirror its terminology, include 4-6 of its keywords naturally>",
  "tailored_experience": [
    {{"index": 0, "bullets": ["<rewritten bullet>", "..."]}},
    {{"index": 1, "bullets": ["..."]}}
  ],
  "tailored_skills": [
    {{"category": "<group name using the posting's vocabulary>", "items": ["<most relevant skills first>"]}}
  ],
  "project_order": [<project indexes, most relevant first>],
  "keywords_to_include": ["<posting keywords the candidate genuinely has — these must appear somewhere in the tailored text>"],
  "suggested_skills": ["<skills/tools the posting requires that are NOT in the skill pool or bullets — the candidate will be asked to confirm; do NOT put these in tailored_skills>"],
  "emphasis_areas": ["<what to foreground for this role>"],
  "deemphasize": ["<what matters less for this role>"],
  "tailored_cover_letter": "<3 short paragraphs, specific to this company and role, referencing real experience>"
}}

HARD RULES:
- Include EVERY experience index, in the same order. Rewrite bullets to emphasise what this posting cares about; you may merge, split, reorder or drop bullets, but keep at most (original count + 1) per role.
- Do NOT invent employers, titles, dates, technologies, metrics or outcomes. Every fact must trace to the input. If the posting wants something the candidate lacks, leave it out — never fabricate.
- Start every bullet with a strong past-tense action verb (present tense for the current role is fine). Keep the existing numbers; do not add new ones.
- Mirror the posting's exact terminology (e.g. if it says "React.js" use "React.js"; if it says "GCP" say "GCP").
- Keep bullets under 35 words. Plain text only: no markdown, no emojis.
- tailored_skills may contain any skill from the SKILL POOL or evidenced in the bullets — pull in pool skills the posting asks for, drop pool skills irrelevant to it, regroup using the posting's vocabulary, and put the posting's must-haves first. Never add a skill from outside the pool: list those under suggested_skills instead.
"""


def _legacy_prompt(job_description: str, base_resume_text: str, profile: dict) -> str:
    skills = profile.get("skills", {})
    roles = profile.get("preferences", {}).get("roles", [])
    return f"""You are an expert resume consultant. Analyze this candidate's resume against a specific job posting and produce tailored content.

CANDIDATE'S BASE RESUME:
{base_resume_text[:5000]}

CANDIDATE'S TARGET ROLES: {', '.join(roles)}
CANDIDATE'S KEY SKILLS: {', '.join(skills.get('primary', []))}

JOB POSTING:
{job_description[:6000]}

Produce a JSON object with these fields:
{{
  "tailored_summary": "<A 2-3 sentence professional summary optimized for THIS specific job, highlighting the most relevant experience and skills from the resume>",
  "tailored_bullets": [
    "<Achievement bullet rewritten to emphasize relevance to this job>",
    "<Another tailored bullet point>",
    "<Up to 6 total>"
  ],
  "emphasis_areas": ["<Skills/experience from resume to emphasize for this role>"],
  "keywords_to_include": ["<Important keywords from the job posting that match the candidate's experience>"],
  "deemphasize": ["<Areas of the resume less relevant to this specific role>"],
  "tailored_cover_letter": "<3 paragraph cover letter specifically for this job, referencing both the job requirements and the candidate's matching experience>"
}}

Rules:
- Only reference experience that ACTUALLY EXISTS in the resume
- Quantify achievements where the resume provides numbers
- Mirror the job posting's language and terminology
- Be specific, not generic — every bullet should connect resume experience to job requirements
- The cover letter should feel personal and specific, not templated
"""


def score_tailoring(profile: dict, tailored: dict, job_description: str, company: str = "") -> dict:
    """ATS report for the tailored render vs the posting (structured mode only)."""
    from utils.ats_checker import check_ats, extract_keywords

    base = resume_from_profile(profile)
    text = render_text(apply_tailoring(base, tailored))
    kws = extract_keywords(job_description, exclude=[company] if company else ())
    for kw in tailored.get("keywords_to_include") or []:
        kw = str(kw).strip()
        if kw and kw.lower() not in {k.lower() for k in kws}:
            kws.append(kw)
    return check_ats(text, job_description, kws[:50])


def tailor_resume(job_description: str, base_resume_text: str, profile: dict, brain=None,
                  notes: str = "", company: str = "") -> dict:
    """
    Generate tailored resume content for a specific job posting.
    `notes` is free-text context from the candidate (how they're applying,
    what to emphasise) that is passed to the model in structured mode.

    Returns a dict with DEFAULTS' keys (+ `ats` in structured mode, `mode`,
    and `error` on failure). Never raises.
    """
    structured = has_structured_resume(profile)
    if not structured and not base_resume_text:
        return _error("No resume text available — upload a PDF or fill the Resume Builder")

    if brain is None:
        from utils.brain import ClaudeBrain
        brain = ClaudeBrain(verbose=False, profile=profile)

    base = resume_from_profile(profile) if structured else None
    pool = skill_pool(profile, base) if structured else []
    prompt = (
        _structured_prompt(job_description, base, profile, pool, notes)
        if structured
        else _legacy_prompt(job_description, base_resume_text, profile)
    )

    try:
        result = brain.ask_json(prompt, timeout=180, component="resume_tailoring")
        if not isinstance(result, dict):
            raise ValueError("model returned a non-object")
        result = _with_defaults(result)
        result["mode"] = "structured" if structured else "legacy"
        if structured:
            result = _validate_structured(result, base, pool)
            try:
                result["ats"] = score_tailoring(profile, result, job_description, company)
            except Exception as exc:  # scoring must never sink the tailoring
                result["ats"] = {"error": str(exc)}
        return result
    except Exception as e:
        return _error(str(e))


# ---------------------------------------------------------------------------
# PDF → structured import (bootstraps the Resume Builder from an existing file)
# ---------------------------------------------------------------------------

def parse_resume_to_structure(resume_text: str, brain=None, profile: dict = None) -> dict:
    """
    Turn an existing resume's text into the `resume:` schema used by
    utils.resume_builder. The model transcribes — it must not embellish.
    Returns {} on failure.
    """
    if not (resume_text or "").strip():
        return {}
    if brain is None:
        from utils.brain import ClaudeBrain
        brain = ClaudeBrain(verbose=False, profile=profile)

    prompt = f"""Convert this resume text into structured JSON. Transcribe faithfully: keep every bullet's wording and numbers, do not add, merge or improve anything. Fix only obvious text-extraction damage (broken words, stray line breaks).

RESUME TEXT:
{resume_text[:9000]}

Return JSON with exactly this shape (omit nothing; use "" or [] when absent):
{{
  "headline": "<title line under the name, if any>",
  "summary": "<summary/profile paragraph>",
  "skills": [{{"category": "<group label or ''>", "items": ["<skill>", "..."]}}],
  "experience": [
    {{"company": "", "title": "", "location": "", "start": "<e.g. Jan 2023 or 2023>", "end": "<or Present>", "tagline": "<one-line company description if present>", "bullets": ["..."]}}
  ],
  "education": [{{"degree": "", "school": "", "location": "", "start": "", "end": "", "details": ""}}],
  "projects": [{{"name": "", "url": "", "description": "", "tech": [], "bullets": []}}],
  "certifications": [{{"name": "", "issuer": "", "year": ""}}],
  "awards": [],
  "languages": []
}}
Most recent experience first. Plain text values only."""

    try:
        data = brain.ask_json(prompt, timeout=180, component="resume_tailoring")
        if not isinstance(data, dict):
            return {}
        # Normalise through the builder so whatever comes back is well-formed.
        normalized = resume_from_profile({"resume": data})
        normalized.pop("contact", None)
        normalized.pop("use_tailored_when_applying", None)
        return normalized
    except Exception:
        return {}
