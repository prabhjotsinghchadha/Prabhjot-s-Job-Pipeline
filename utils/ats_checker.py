"""
ATS Checker — deterministic, token-free scoring of a resume against a job
posting the way applicant-tracking systems and recruiter keyword filters do.

Two halves, combined 60/40 into a 0–100 score:

  keyword_score  how many of the posting's requirement keywords appear in the
                 resume text (skills, tools, methodologies, seniority terms).
                 Multi-word terms ("distributed systems") count as one keyword.

  format_score   parseability checks: contact block present, standard section
                 headings, bullets that start with action verbs and carry
                 numbers, sane length, consistent dates, no text-extraction
                 damage (one-word-per-line PDFs, missing spaces).

No LLM involved — cheap enough to run on every render and every tailor.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Multi-word tech/skill terms that must be matched as a phrase.
PHRASES = [
    "machine learning", "deep learning", "natural language processing", "computer vision",
    "large language models", "distributed systems", "system design", "data structures",
    "microservices", "event driven", "event-driven", "real-time", "real time", "web sockets",
    "ci/cd", "continuous integration", "continuous delivery", "infrastructure as code",
    "unit testing", "integration testing", "end-to-end testing", "test driven",
    "object oriented", "functional programming", "design patterns", "restful apis",
    "rest apis", "rest api", "graphql", "grpc", "message queues", "pub/sub",
    "cloud infrastructure", "google cloud platform", "google cloud", "amazon web services",
    "react native", "next.js", "nextjs", "node.js", "nodejs", "vue.js", "nuxt.js",
    "tailwind css", "styled components", "material ui", "design system", "design systems",
    "state management", "server side rendering", "server-side rendering", "static site generation",
    "performance optimization", "web performance", "core web vitals", "accessibility",
    "smart contracts", "smart contract", "solidity", "ethers.js", "web3.js", "defi", "web3",
    "blockchain", "prediction market", "prediction markets", "fintech", "payments",
    "product management", "technical leadership", "team leadership", "mentoring",
    "cross-functional", "cross functional", "stakeholder management", "agile", "scrum",
    "kanban", "roadmap", "architecture", "scalability", "high availability", "observability",
    "monitoring", "logging", "security", "authentication", "authorization", "oauth", "sso",
    "data modeling", "data pipelines", "etl", "analytics", "a/b testing", "experimentation",
    "prompt engineering", "rag", "retrieval augmented generation", "vector database",
    "vector databases", "embeddings", "fine-tuning", "fine tuning", "llm", "llms", "ai agents",
    "agentic", "openai", "anthropic", "claude", "gpt", "langchain", "hugging face",
    "typescript", "javascript", "python", "golang", "rust", "java", "kotlin", "swift", "c++",
    "c#", ".net", "ruby", "rails", "django", "fastapi", "flask", "express", "nestjs", "spring",
    "react", "angular", "svelte", "redux", "zustand", "html", "css", "sass",
    "postgresql", "postgres", "mysql", "sqlite", "mongodb", "redis", "elasticsearch",
    "dynamodb", "firestore", "firebase", "supabase", "prisma", "drizzle", "sql", "nosql",
    "aws", "gcp", "azure", "vercel", "netlify", "cloudflare", "heroku", "railway",
    "docker", "kubernetes", "k8s", "terraform", "helm", "serverless", "lambda",
    "cloud functions", "cloud run", "github actions", "gitlab ci", "jenkins", "circleci",
    "git", "github", "gitlab", "linux", "bash", "nginx",
    "jest", "vitest", "cypress", "playwright", "pytest", "selenium", "storybook",
    "figma", "ux", "ui", "responsive design", "mobile", "ios", "android", "flutter", "expo",
    "websockets", "websocket", "webrtc", "kafka", "rabbitmq", "sqs", "pubsub",
    "stripe", "twilio", "sendgrid", "segment", "mixpanel", "datadog", "sentry", "grafana",
    "prometheus", "opentelemetry", "startup", "founding engineer", "0 to 1", "zero to one",
    "ownership", "remote", "full stack", "full-stack", "fullstack", "frontend", "front-end",
    "backend", "back-end", "devops", "sre", "platform engineering", "staff engineer",
    "senior engineer", "tech lead", "engineering manager", "cto",
]

# Words that are never keywords on their own.
STOPWORDS = set("""
a about above across after again against all also am an and any are as at be because been
before being below between both but by can could did do does doing down during each few for
from further had has have having he her here hers herself him himself his how i if in into
is it its itself just let me more most my myself no nor not now of off on once only or other
our ours ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were
what when where which while who whom why will with would you your yours yourself yourselves
years year experience experienced work working team teams role roles company companies job
strong ability able skills skill including include includes required requirements
requirement preferred plus nice bonus must will well good great excellent knowledge
understanding familiarity familiar proficient proficiency looking seeking hiring join us our
we're you'll you're candidate candidates ideal minimum equivalent related relevant using use
used new build building built develop developing developed design designing designed
help helping ensure ensuring support supporting across within etc e.g i.e via per day
benefits salary compensation equity location remote-first applicants apply application
full stack engineer engineers engineering developer developers software senior junior lead
level position opportunity responsibilities qualifications years title mid staff principal
""".split())

ACTION_VERBS = set("""
achieved architected automated built championed created cut decreased delivered deployed
designed developed directed drove eliminated engineered established expanded founded grew
implemented improved increased integrated launched led managed mentored migrated modernized
optimized orchestrated owned partnered pioneered promoted reduced re-architected rebuilt
refactored released scaled shipped spearheaded streamlined transformed
""".split())

SECTION_PATTERNS = {
    "contact": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "summary": r"^\s*(professional |executive )?(summary|profile|about)\b",
    "experience": r"^\s*(professional |work )?experience\b|^\s*employment\b",
    "education": r"^\s*education\b",
    "skills": r"^\s*(core |technical |key )?(skills|competencies|technologies)\b",
}

DATE_RX = re.compile(
    r"\b((jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d{2}\b"
    r"|\bpresent\b|\bcurrent\b",
    re.I,
)
NUMBER_RX = re.compile(r"\d|\$|%|\bk\b|\bm\b", re.I)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_pdf_text(path: str | Path) -> str:
    """Text layer of a PDF via pdfplumber, else pypdf. Empty string if neither."""
    path = Path(path)
    if not path.exists():
        return ""
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    except Exception:
        return ""
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    except Exception:
        return ""


def extract_docx_text(path: str | Path) -> str:
    try:
        from docx import Document
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    text = text.lower()
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    return re.sub(r"[ \t]+", " ", text)


def _phrase_rx(term: str) -> re.Pattern:
    # Word boundaries fail on terms like "c++" / ".net" — use lookarounds.
    # "real time" / "real-time" / "real  time" are the same keyword: split on
    # spaces and hyphens, escape each piece, rejoin with a flexible separator.
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term.lower()) if p]
    esc = r"[\s\-]+".join(parts) if parts else re.escape(term.lower())
    return re.compile(rf"(?<![a-z0-9]){esc}(?![a-z0-9])", re.I)


_PHRASE_RX = {p: _phrase_rx(p) for p in PHRASES}

# Aliases collapse to one canonical keyword so "Node.js" in the resume
# satisfies "NodeJS" in the posting.
ALIASES = {
    "nodejs": "node.js", "nextjs": "next.js", "golang": "go", "postgres": "postgresql",
    "k8s": "kubernetes", "real time": "real-time", "event driven": "event-driven",
    "full stack": "full-stack", "fullstack": "full-stack", "front-end": "frontend",
    "back-end": "backend", "rest api": "rest apis", "restful apis": "rest apis",
    "google cloud platform": "gcp", "google cloud": "gcp", "amazon web services": "aws",
    "websocket": "websockets", "web sockets": "websockets", "llms": "llm",
    "large language models": "llm", "prediction markets": "prediction market",
    "smart contract": "smart contracts", "cross functional": "cross-functional",
    "fine tuning": "fine-tuning", "design systems": "design system",
    "server side rendering": "server-side rendering", "zero to one": "0 to 1",
    "continuous integration": "ci/cd", "continuous delivery": "ci/cd",
    "retrieval augmented generation": "rag", "pubsub": "pub/sub",
}


def canonical(term: str) -> str:
    t = term.lower().strip()
    return ALIASES.get(t, t)


def extract_keywords(job_description: str, limit: int = 40, exclude: Iterable[str] = ()) -> list[str]:
    """
    Requirement keywords from a posting, most important first.

    Weighting: known tech phrases 3× (+3 inside a requirements block),
    capitalised mid-sentence tokens (product names) 1×, frequent plain tokens
    0.35× per occurrence. Generic words are stopworded; `exclude` drops e.g.
    the hiring company's name so it never counts as a "missing" keyword.
    """
    if not job_description:
        return []
    text = _norm(job_description)
    scores: Counter = Counter()
    excluded = set()
    for ex in exclude:
        for tok in re.findall(r"[a-z0-9+#.]+", str(ex).lower()):
            if len(tok) >= 3:
                excluded.add(tok)

    # Requirement block gets a bonus
    req_block = ""
    m = re.search(
        r"(requirements?|qualifications?|what you.ll need|what we.re looking for|must have|"
        r"you have|about you|skills)(.*?)(benefits|perks|about us|compensation|what we offer|$)",
        text, re.S,
    )
    if m:
        req_block = m.group(2)

    for phrase, rx in _PHRASE_RX.items():
        n = len(rx.findall(text))
        if n:
            key = canonical(phrase)
            scores[key] += 3 * min(n, 4)
            if rx.search(req_block):
                scores[key] += 3

    # Capitalised single tokens from the original text (product / tool names).
    # Sentence- and line-initial capitals are ordinary words ("Ship fast",
    # "Fully remote"), so only mid-sentence capitals count.
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9+#.]{2,}\b", job_description):
        tok = m.group(0)
        before = job_description[max(0, m.start() - 3):m.start()]
        if not before.strip() or re.search(r"[.!?:;\-–—•|(\[\"]\s*$", before):
            continue
        low = tok.lower().rstrip(".")
        if low in STOPWORDS or len(low) < 3 or low in _PHRASE_RX or low in excluded:
            continue
        scores[canonical(low)] += 1

    # Frequent plain tokens
    for tok in re.findall(r"[a-z][a-z0-9+#./-]{2,}", text):
        tok = tok.strip("./-")
        if tok in STOPWORDS or len(tok) < 4 or tok in _PHRASE_RX or tok.isdigit() or tok in excluded:
            continue
        scores[canonical(tok)] += 0.35

    ranked = [k for k, v in scores.most_common() if v >= 1.0]
    return ranked[:limit]


def keywords_present(text: str, keywords: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split keywords into (matched, missing) against resume text, alias-aware."""
    norm = _norm(text)
    matched, missing = [], []
    for kw in keywords:
        kw_c = canonical(kw)
        variants = {kw.lower(), kw_c} | {a for a, c in ALIASES.items() if c == kw_c}
        if any(_phrase_rx(v).search(norm) for v in variants):
            matched.append(kw)
        else:
            missing.append(kw)
    return matched, missing


# ---------------------------------------------------------------------------
# Format checks
# ---------------------------------------------------------------------------

def _format_checks(text: str) -> tuple[int, list[dict], dict]:
    """Return (score 0-100, issues, stats). Each issue: {severity, message}."""
    issues: list[dict] = []
    lines = [l.rstrip() for l in text.splitlines()]
    nonblank = [l for l in lines if l.strip()]
    words = re.findall(r"\S+", text)
    word_count = len(words)
    score = 100

    def issue(sev: str, msg: str, penalty: int):
        nonlocal score
        issues.append({"severity": sev, "message": msg})
        score -= penalty

    # Contact block
    if not re.search(SECTION_PATTERNS["contact"], text):
        issue("high", "No email address found — ATS can't build a candidate record.", 15)
    if not re.search(r"(\+?\d[\d\s().-]{8,}\d)", text):
        issue("medium", "No phone number detected.", 6)
    if not re.search(r"linkedin\.com/in/", text, re.I):
        issue("low", "No LinkedIn URL — recruiters cross-check it.", 3)

    # Sections
    found = {}
    for key in ("summary", "experience", "education", "skills"):
        found[key] = any(re.search(SECTION_PATTERNS[key], l, re.I) for l in nonblank)
    for key, label in (("experience", "EXPERIENCE"), ("education", "EDUCATION"), ("skills", "SKILLS")):
        if not found[key]:
            issue("high", f"No standard '{label}' heading — parsers key on exact section names.", 12)
    if not found["summary"]:
        issue("low", "No SUMMARY section — a keyword-dense summary lifts match rates.", 4)

    # Length
    if word_count < 250:
        issue("medium", f"Only {word_count} words — too thin for keyword matching (aim 400–800).", 10)
    elif word_count > 1100:
        issue("medium", f"{word_count} words — likely over two pages; trim older roles.", 8)

    # Bullets
    bullets = [l.strip().lstrip("•-*·▪◦ ").strip() for l in nonblank if re.match(r"^\s*[•\-*·▪◦]", l)]
    if bullets:
        quantified = sum(1 for b in bullets if NUMBER_RX.search(b))
        verbs = sum(1 for b in bullets if b.split(" ", 1)[0].lower().strip(",.") in ACTION_VERBS)
        q_ratio, v_ratio = quantified / len(bullets), verbs / len(bullets)
        if q_ratio < 0.3:
            issue("medium", f"Only {quantified}/{len(bullets)} bullets contain a number — add metrics.", 8)
        if v_ratio < 0.5:
            issue("low", f"{len(bullets) - verbs}/{len(bullets)} bullets don't open with an action verb.", 5)
        long_bullets = sum(1 for b in bullets if len(b.split()) > 45)
        if long_bullets:
            issue("low", f"{long_bullets} bullet(s) exceed ~45 words — split them.", 3)
    else:
        issue("medium", "No bullet points detected — achievements should be bulleted.", 8)
        q_ratio = v_ratio = 0.0

    # Dates
    dates = DATE_RX.findall(text)
    if len(dates) < 2:
        issue("medium", "Few or no dates detected — every role needs a start and end.", 8)

    # Extraction damage (typical of design-tool PDFs)
    if nonblank:
        one_word = sum(1 for l in nonblank if len(l.split()) == 1)
        if one_word / len(nonblank) > 0.5:
            issue("high", "Text extracts one word per line — the PDF text layer is fragmented; "
                          "regenerate from the builder.", 20)
    if re.search(r"[a-z]{25,}", text):
        issue("medium", "Words run together without spaces — text layer is damaged.", 8)
    if re.search(r"[-]", text):
        issue("low", "Private-use glyphs (icon fonts) found — they turn into garbage in ATS.", 4)

    stats = {
        "words": word_count,
        "bullets": len(bullets),
        "quantified_ratio": round(q_ratio, 2),
        "action_verb_ratio": round(v_ratio, 2),
        "sections_found": [k for k, v in found.items() if v],
        "dates_found": len(dates),
    }
    return max(0, min(100, score)), issues, stats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_ats(resume_text: str, job_description: str = "",
              keywords: Optional[list[str]] = None) -> dict:
    """
    Score `resume_text` for ATS friendliness and, if given, against a posting.

    `keywords` overrides automatic extraction (e.g. the tailor's
    keywords_to_include merged with the extractor's output).
    """
    resume_text = resume_text or ""
    fmt_score, issues, stats = _format_checks(resume_text)

    kws = list(keywords) if keywords else extract_keywords(job_description)
    if kws:
        matched, missing = keywords_present(resume_text, kws)
        # Weight by rank: the first keywords matter more.
        weights = {k: max(1.0, 3.0 - 2.0 * i / max(1, len(kws) - 1)) for i, k in enumerate(kws)}
        got = sum(weights[k] for k in matched)
        total = sum(weights.values()) or 1.0
        kw_score = round(100 * got / total)
        score = round(0.6 * kw_score + 0.4 * fmt_score)
    else:
        matched, missing, kw_score = [], [], None
        score = fmt_score

    suggestions = []
    if missing:
        top = missing[:8]
        suggestions.append(
            "Work these posting keywords into your summary, skills or bullets (only where true): "
            + ", ".join(top)
        )
    for iss in issues:
        if iss["severity"] in ("high", "medium"):
            suggestions.append(iss["message"])

    return {
        "score": score,
        "keyword_score": kw_score,
        "format_score": fmt_score,
        "matched_keywords": matched,
        "missing_keywords": missing,
        "keywords_checked": len(kws),
        "issues": issues,
        "suggestions": suggestions[:8],
        "stats": stats,
        "grade": "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 55 else "D",
    }


def check_ats_file(path: str | Path, job_description: str = "",
                   keywords: Optional[list[str]] = None) -> dict:
    """Extract text from a PDF/DOCX on disk and score it."""
    path = Path(path)
    text = extract_docx_text(path) if path.suffix.lower() == ".docx" else extract_pdf_text(path)
    result = check_ats(text, job_description, keywords)
    result["source"] = str(path)
    if not text.strip():
        result["issues"].insert(0, {
            "severity": "high",
            "message": "No text could be extracted — the file is an image or an unreadable PDF.",
        })
        result["score"] = 0
    return result
