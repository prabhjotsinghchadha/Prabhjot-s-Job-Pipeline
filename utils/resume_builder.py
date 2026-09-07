"""
Resume Builder — structured resume data (profile.yaml `resume:`) → ATS-friendly
DOCX / PDF / plain-text output, optionally tailored per job.

Design rules (what applicant-tracking systems parse reliably):
  * single column, no tables / text boxes / headers / footers / images
  * standard section headings in the canonical order
  * contact details as body text on the first lines
  * standard fonts (Calibri in DOCX, Helvetica in PDF), 10–11pt body
  * plain "•" bullets, one achievement per bullet
  * dates in a consistent "Mon YYYY – Mon YYYY" / "YYYY – Present" form
  * real text layer in the PDF (reportlab draws text, never rasterises)

The same normalized dict feeds every renderer, so the DOCX, the PDF and the
text used for ATS scoring are guaranteed to say the same thing.

profile.yaml schema (all keys optional):

    resume:
      headline: "Senior Full Stack Engineer"
      summary: "Two or three sentences..."
      skills:
        - category: Languages & Frameworks
          items: [TypeScript, React, Next.js]
      experience:
        - company: Acme
          title: Senior Engineer
          location: Remote
          start: "Jan 2023"
          end: "Present"
          tagline: "one-line description of the company (optional)"
          bullets: ["Did X, achieving Y"]
      education:
        - degree: B.S. Computer Science
          school: State University
          location: City, Country
          start: "2014"
          end: "2018"
          details: ""
      projects:
        - name: Side Project
          url: https://...
          description: "one line"
          tech: [Python, FastAPI]
          bullets: []
      certifications:
        - name: AWS Solutions Architect
          issuer: Amazon
          year: "2022"
      awards: ["..."]
      languages: ["English (fluent)"]
      use_tailored_when_applying: true
"""

from __future__ import annotations

import io
import re
from copy import deepcopy
from pathlib import Path
from typing import Optional

SECTION_TITLES = {
    "summary": "PROFESSIONAL SUMMARY",
    "skills": "CORE SKILLS",
    "experience": "PROFESSIONAL EXPERIENCE",
    "projects": "PROJECTS",
    "education": "EDUCATION",
    "certifications": "CERTIFICATIONS",
    "awards": "AWARDS",
    "languages": "LANGUAGES",
}

# Order ATS parsers expect. Skills before experience keeps the keyword block
# on page one, which is where most parsers weight matches highest.
SECTION_ORDER = [
    "summary", "skills", "experience", "projects",
    "education", "certifications", "awards", "languages",
]

EMPTY_RESUME = {
    "headline": "",
    "summary": "",
    "skills": [],
    "experience": [],
    "education": [],
    "projects": [],
    "certifications": [],
    "awards": [],
    "languages": [],
    "use_tailored_when_applying": True,
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _s(value) -> str:
    return str(value).strip() if value is not None else ""


def _list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split("\n") if v.strip()]
    return [v for v in value if v is not None]


def _str_list(value) -> list[str]:
    return [_s(v) for v in _list(value) if _s(v)]


def _strip_link(url: str) -> str:
    """Display form of a URL: no scheme, no trailing slash."""
    url = _s(url)
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.rstrip("/")


def has_structured_resume(profile: dict) -> bool:
    """True when profile.yaml carries enough `resume:` data to render."""
    r = profile.get("resume") or {}
    return bool(r.get("experience") or r.get("summary") or r.get("skills"))


def resume_from_profile(profile: dict) -> dict:
    """
    Merge `personal:` + `resume:` from profile.yaml into one normalized dict.
    Every renderer consumes this shape; unknown keys are dropped.
    """
    personal = profile.get("personal") or {}
    raw = profile.get("resume") or {}

    name = " ".join(
        p for p in (_s(personal.get("first_name")), _s(personal.get("last_name"))) if p
    )
    contact = {
        "name": name,
        "email": _s(personal.get("email")),
        "phone": _s(personal.get("phone")),
        "location": _s(personal.get("location")),
        "linkedin": _s(personal.get("linkedin")),
        "github": _s(personal.get("github")),
        "portfolio": _s(personal.get("portfolio")),
    }

    skills = []
    for group in _list(raw.get("skills")):
        if isinstance(group, str):
            skills.append({"category": "", "items": [group]})
            continue
        items = _str_list(group.get("items"))
        if items:
            skills.append({"category": _s(group.get("category")), "items": items})

    # Fallback: profile skills.primary/secondary when resume.skills is empty
    if not skills:
        sk = profile.get("skills") or {}
        if sk.get("primary"):
            skills.append({"category": "Primary", "items": _str_list(sk["primary"])})
        if sk.get("secondary"):
            skills.append({"category": "Also", "items": _str_list(sk["secondary"])})

    experience = []
    for e in _list(raw.get("experience")):
        if not isinstance(e, dict):
            continue
        experience.append({
            "company": _s(e.get("company")),
            "title": _s(e.get("title")),
            "location": _s(e.get("location")),
            "start": _s(e.get("start")),
            "end": _s(e.get("end")) or "Present",
            "tagline": _s(e.get("tagline")),
            "bullets": _str_list(e.get("bullets")),
        })

    education = []
    for e in _list(raw.get("education")):
        if not isinstance(e, dict):
            continue
        education.append({
            "degree": _s(e.get("degree")),
            "school": _s(e.get("school")),
            "location": _s(e.get("location")),
            "start": _s(e.get("start")),
            "end": _s(e.get("end")),
            "details": _s(e.get("details")),
        })

    projects = []
    for p in _list(raw.get("projects")):
        if not isinstance(p, dict):
            continue
        projects.append({
            "name": _s(p.get("name")),
            "url": _s(p.get("url")),
            "description": _s(p.get("description")),
            "tech": _str_list(p.get("tech")),
            "bullets": _str_list(p.get("bullets")),
        })

    certifications = []
    for c in _list(raw.get("certifications")):
        if isinstance(c, str):
            certifications.append({"name": c, "issuer": "", "year": ""})
        elif isinstance(c, dict):
            certifications.append({
                "name": _s(c.get("name")),
                "issuer": _s(c.get("issuer")),
                "year": _s(c.get("year")),
            })

    return {
        "contact": contact,
        "headline": _s(raw.get("headline")),
        "summary": _s(raw.get("summary")),
        "skills": skills,
        "experience": experience,
        "education": education,
        "projects": projects,
        "certifications": certifications,
        "awards": _str_list(raw.get("awards")),
        "languages": _str_list(raw.get("languages")),
        "use_tailored_when_applying": raw.get("use_tailored_when_applying", True) is not False,
    }


# ---------------------------------------------------------------------------
# Tailoring overlay
# ---------------------------------------------------------------------------

def apply_tailoring(resume: dict, tailored: Optional[dict]) -> dict:
    """
    Overlay per-job tailoring (from utils.resume_tailor) on a normalized resume.

    Recognised keys in `tailored`:
      tailored_headline    str
      tailored_summary     str
      tailored_experience  [{index:int, bullets:[str]}]   rewritten bullets per role
      tailored_skills      [{category:str, items:[str]}]  re-ordered/re-grouped skills
      project_order        [int]                          indexes, most relevant first

    Anything malformed is ignored — the base content always wins over garbage.
    Companies, titles, dates and education are never touched: tailoring may
    rephrase what the candidate did, never where or when.
    """
    out = deepcopy(resume)
    if not tailored or not isinstance(tailored, dict):
        return out

    if _s(tailored.get("tailored_headline")):
        out["headline"] = _s(tailored["tailored_headline"])
    if _s(tailored.get("tailored_summary")):
        out["summary"] = _s(tailored["tailored_summary"])

    for item in _list(tailored.get("tailored_experience")):
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        bullets = _str_list(item.get("bullets"))
        if 0 <= idx < len(out["experience"]) and bullets:
            out["experience"][idx]["bullets"] = bullets

    skills = []
    for group in _list(tailored.get("tailored_skills")):
        if isinstance(group, dict):
            items = _str_list(group.get("items"))
            if items:
                skills.append({"category": _s(group.get("category")), "items": items})
    if skills:
        out["skills"] = skills

    order = tailored.get("project_order")
    if isinstance(order, list) and out["projects"]:
        seen, reordered = set(), []
        for i in order:
            if isinstance(i, int) and 0 <= i < len(out["projects"]) and i not in seen:
                reordered.append(out["projects"][i])
                seen.add(i)
        reordered += [p for i, p in enumerate(out["projects"]) if i not in seen]
        out["projects"] = reordered

    return out


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

def _date_range(start: str, end: str) -> str:
    start, end = _s(start), _s(end)
    if start and end:
        return f"{start} – {end}"
    return start or end


def _contact_lines(contact: dict) -> list[str]:
    """Two lines of contact info; every value is plain text an ATS can regex."""
    line1 = [v for v in (contact.get("location"), contact.get("phone"), contact.get("email")) if v]
    line2 = [
        _strip_link(v) for v in (contact.get("linkedin"), contact.get("github"), contact.get("portfolio")) if v
    ]
    lines = []
    if line1:
        lines.append("  |  ".join(line1))
    if line2:
        lines.append("  |  ".join(line2))
    return lines


def _skills_line(group: dict) -> str:
    items = ", ".join(group["items"])
    return f"{group['category']}: {items}" if group.get("category") else items


def _project_heading(p: dict) -> str:
    head = p["name"]
    if p.get("url"):
        head += f"  |  {_strip_link(p['url'])}"
    return head


def _cert_line(c: dict) -> str:
    parts = [c["name"]]
    if c.get("issuer"):
        parts.append(c["issuer"])
    if c.get("year"):
        parts.append(c["year"])
    return " — ".join(p for p in parts if p)


def _present_sections(resume: dict) -> list[str]:
    return [s for s in SECTION_ORDER if resume.get(s)]


def safe_filename(resume: dict, suffix: str = "", ext: str = "pdf") -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", resume["contact"].get("name") or "Resume").strip("_")
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", suffix).strip("_")
    parts = [name, "Resume"] + ([suffix] if suffix else [])
    return "_".join(parts) + f".{ext}"


# ---------------------------------------------------------------------------
# Plain text (ATS view + preview)
# ---------------------------------------------------------------------------

def render_text(resume: dict) -> str:
    c = resume["contact"]
    out = [c["name"].upper() if c["name"] else ""]
    if resume.get("headline"):
        out.append(resume["headline"])
    out += _contact_lines(c)

    for section in _present_sections(resume):
        out += ["", SECTION_TITLES[section]]
        if section == "summary":
            out.append(resume["summary"])
        elif section == "skills":
            out += [_skills_line(g) for g in resume["skills"]]
        elif section == "experience":
            for e in resume["experience"]:
                out.append(f"{e['title']} | {e['company']}")
                meta = [x for x in (e["location"], _date_range(e["start"], e["end"])) if x]
                if meta:
                    out.append(" | ".join(meta))
                if e["tagline"]:
                    out.append(e["tagline"])
                out += [f"• {b}" for b in e["bullets"]]
                out.append("")
        elif section == "projects":
            for p in resume["projects"]:
                out.append(_project_heading(p))
                if p["description"]:
                    out.append(p["description"])
                if p["tech"]:
                    out.append("Technologies: " + ", ".join(p["tech"]))
                out += [f"• {b}" for b in p["bullets"]]
        elif section == "education":
            for e in resume["education"]:
                out.append(f"{e['degree']} | {e['school']}")
                meta = [x for x in (e["location"], _date_range(e["start"], e["end"])) if x]
                if meta:
                    out.append(" | ".join(meta))
                if e["details"]:
                    out.append(e["details"])
        elif section == "certifications":
            out += [_cert_line(cert) for cert in resume["certifications"]]
        elif section in ("awards", "languages"):
            out += [f"• {x}" for x in resume[section]]

    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def render_docx(resume: dict) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, Inches, RGBColor

    BODY_FONT = "Calibri"
    doc = Document()

    for section in doc.sections:
        section.top_margin = Inches(0.6)
        section.bottom_margin = Inches(0.6)
        section.left_margin = Inches(0.7)
        section.right_margin = Inches(0.7)

    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.line_spacing = 1.05

    def para(text="", *, bold=False, size=None, align=None, color=None, italic=False,
             space_before=0, space_after=0):
        p = doc.add_paragraph()
        if text:
            run = p.add_run(text)
            run.bold = bold
            run.italic = italic
            if size:
                run.font.size = Pt(size)
            if color:
                run.font.color.rgb = RGBColor(*color)
        if align == "center":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after = Pt(space_after)
        return p

    def heading(text):
        p = para(text, bold=True, size=11, space_before=9, space_after=3)
        # Bottom border via paragraph properties — a rule, not a table.
        pPr = p._p.get_or_add_pPr()
        pbdr = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), "444444")
        pbdr.append(bottom)
        pPr.append(pbdr)
        return p

    def two_part(left, right, *, bold_left=True, space_before=5):
        """'Title  ...  Dates' on one line using a right-aligned tab stop."""
        p = para(space_before=space_before)
        run = p.add_run(left)
        run.bold = bold_left
        if right:
            usable = doc.sections[0].page_width - doc.sections[0].left_margin - doc.sections[0].right_margin
            p.paragraph_format.tab_stops.add_tab_stop(usable, alignment=WD_TAB_ALIGNMENT.RIGHT)
            p.add_run("\t" + right)
        return p

    def bullet(text):
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(text)
        p.paragraph_format.space_after = Pt(1.5)
        p.paragraph_format.left_indent = Inches(0.22)
        return p

    c = resume["contact"]
    para(c["name"].upper(), bold=True, size=17, align="center", space_after=1)
    if resume.get("headline"):
        para(resume["headline"], size=11, align="center", color=(0x33, 0x33, 0x33), space_after=1)
    for line in _contact_lines(c):
        para(line, size=9.5, align="center", color=(0x33, 0x33, 0x33))

    for section in _present_sections(resume):
        heading(SECTION_TITLES[section])
        if section == "summary":
            para(resume["summary"], space_after=2)
        elif section == "skills":
            for g in resume["skills"]:
                p = para(space_after=1.5)
                if g["category"]:
                    p.add_run(g["category"] + ": ").bold = True
                p.add_run(", ".join(g["items"]))
        elif section == "experience":
            for e in resume["experience"]:
                two_part(e["title"], _date_range(e["start"], e["end"]))
                sub = " · ".join(x for x in (e["company"], e["location"]) if x)
                if e["tagline"]:
                    sub = f"{sub} — {e['tagline']}" if sub else e["tagline"]
                if sub:
                    para(sub, italic=True, color=(0x44, 0x44, 0x44), space_after=2)
                for b in e["bullets"]:
                    bullet(b)
        elif section == "projects":
            for p_ in resume["projects"]:
                two_part(p_["name"], _strip_link(p_["url"]) if p_["url"] else "")
                if p_["description"]:
                    para(p_["description"], space_after=1)
                if p_["tech"]:
                    para("Technologies: " + ", ".join(p_["tech"]), italic=True,
                         color=(0x44, 0x44, 0x44), space_after=1)
                for b in p_["bullets"]:
                    bullet(b)
        elif section == "education":
            for e in resume["education"]:
                two_part(e["degree"], _date_range(e["start"], e["end"]))
                sub = " · ".join(x for x in (e["school"], e["location"]) if x)
                if sub:
                    para(sub, italic=True, color=(0x44, 0x44, 0x44))
                if e["details"]:
                    para(e["details"])
        elif section == "certifications":
            for cert in resume["certifications"]:
                bullet(_cert_line(cert))
        elif section in ("awards", "languages"):
            for x in resume[section]:
                bullet(x)

    core = doc.core_properties
    core.author = c["name"]
    core.title = f"{c['name']} — Resume"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def render_pdf(resume: dict) -> bytes:
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    from xml.sax.saxutils import escape

    c = resume["contact"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.45 * inch, bottomMargin=0.45 * inch,
        title=f"{c['name']} — Resume", author=c["name"], subject="Resume",
    )
    W = letter[0] - doc.leftMargin - doc.rightMargin

    body = ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=11.6, spaceAfter=1)
    name_st = ParagraphStyle("name", parent=body, fontName="Helvetica-Bold", fontSize=16,
                             leading=19, alignment=TA_CENTER, spaceAfter=1)
    headline_st = ParagraphStyle("headline", parent=body, fontSize=10.5, leading=13,
                                 alignment=TA_CENTER, textColor="#333333")
    contact_st = ParagraphStyle("contact", parent=body, fontSize=9.2, leading=11.5,
                                alignment=TA_CENTER, textColor="#333333")
    h_st = ParagraphStyle("h", parent=body, fontName="Helvetica-Bold", fontSize=10.5,
                          leading=12.5, spaceBefore=6, spaceAfter=0)
    role_st = ParagraphStyle("role", parent=body, fontName="Helvetica-Bold", spaceBefore=3.5)
    date_st = ParagraphStyle("date", parent=body, alignment=2, spaceBefore=3.5)  # right
    sub_st = ParagraphStyle("sub", parent=body, fontName="Helvetica-Oblique",
                            textColor="#444444", spaceAfter=1.5)
    bullet_st = ParagraphStyle("bullet", parent=body, leftIndent=14, bulletIndent=4,
                               spaceAfter=1)

    E = escape
    story = []

    def heading(text):
        story.append(Paragraph(E(text), h_st))
        story.append(HRFlowable(width="100%", thickness=0.7, color="#444444",
                                spaceBefore=1, spaceAfter=2.5))

    def two_part(left, right):
        # A borderless two-cell row for title/date alignment. ATS text
        # extraction reads it left→right as one line; no visible grid.
        if not right:
            story.append(Paragraph(E(left), role_st))
            return
        t = Table([[Paragraph(E(left), role_st), Paragraph(E(right), date_st)]],
                  colWidths=[W * 0.72, W * 0.28])
        t.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ]))
        story.append(t)

    def bullets(items):
        for b in items:
            story.append(Paragraph(E(b), bullet_st, bulletText="•"))

    story.append(Paragraph(E(c["name"].upper()), name_st))
    if resume.get("headline"):
        story.append(Paragraph(E(resume["headline"]), headline_st))
    for line in _contact_lines(c):
        story.append(Paragraph(E(line), contact_st))
    story.append(Spacer(1, 2))

    for section in _present_sections(resume):
        heading(SECTION_TITLES[section])
        if section == "summary":
            story.append(Paragraph(E(resume["summary"]), body))
        elif section == "skills":
            for g in resume["skills"]:
                items = E(", ".join(g["items"]))
                text = f"<b>{E(g['category'])}:</b> {items}" if g["category"] else items
                story.append(Paragraph(text, body))
        elif section == "experience":
            for e in resume["experience"]:
                block_start = len(story)
                two_part(e["title"], _date_range(e["start"], e["end"]))
                sub = " · ".join(x for x in (e["company"], e["location"]) if x)
                if e["tagline"]:
                    sub = f"{sub} — {e['tagline']}" if sub else e["tagline"]
                if sub:
                    story.append(Paragraph(E(sub), sub_st))
                bullets(e["bullets"][:1])
                # Keep the role header with its first bullet across page breaks.
                head = story[block_start:]
                del story[block_start:]
                story.append(KeepTogether(head))
                bullets(e["bullets"][1:])
        elif section == "projects":
            for p in resume["projects"]:
                two_part(p["name"], _strip_link(p["url"]) if p["url"] else "")
                if p["description"]:
                    story.append(Paragraph(E(p["description"]), body))
                if p["tech"]:
                    story.append(Paragraph(E("Technologies: " + ", ".join(p["tech"])), sub_st))
                bullets(p["bullets"])
        elif section == "education":
            for e in resume["education"]:
                two_part(e["degree"], _date_range(e["start"], e["end"]))
                sub = " · ".join(x for x in (e["school"], e["location"]) if x)
                if sub:
                    story.append(Paragraph(E(sub), sub_st))
                if e["details"]:
                    story.append(Paragraph(E(e["details"]), body))
        elif section == "certifications":
            bullets([_cert_line(cert) for cert in resume["certifications"]])
        elif section in ("awards", "languages"):
            bullets(resume[section])

    doc.build(story)
    return buf.getvalue()


RENDERERS = {"pdf": render_pdf, "docx": render_docx, "txt": render_text}
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
}


def build_resume(profile: dict, fmt: str = "pdf", tailored: Optional[dict] = None,
                 suffix: str = "") -> tuple[bytes, str, str]:
    """
    Render the profile's resume (optionally tailored) in `fmt`.
    Returns (bytes, filename, mime).
    """
    fmt = (fmt or "pdf").lower()
    if fmt not in RENDERERS:
        raise ValueError(f"Unsupported format: {fmt} (use pdf, docx, or txt)")
    resume = apply_tailoring(resume_from_profile(profile), tailored)
    data = RENDERERS[fmt](resume)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return data, safe_filename(resume, suffix, fmt), MIME[fmt]


# ---------------------------------------------------------------------------
# Tailored PDF on disk — what the apply adapters upload
# ---------------------------------------------------------------------------

def tailored_dir() -> Path:
    from utils.usercontext import resumes_dir
    d = resumes_dir() / "tailored"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_tailored_pdf(profile: dict, job: dict, tailored: dict) -> Path:
    """Render the tailored resume for `job` to resumes/tailored/<job_id>.pdf."""
    company = re.sub(r"[^A-Za-z0-9]+", "_", job.get("company") or "").strip("_")[:40]
    data, _, _ = build_resume(profile, "pdf", tailored, suffix=company)
    path = tailored_dir() / f"{job['id']}.pdf"
    path.write_bytes(data)
    return path


def resume_path_for_job(profile: dict, job: dict, tailored: Optional[dict] = None) -> str:
    """
    Decide which resume file to attach/upload for this job:
      * the per-job tailored PDF when structured resume data exists, the job
        has structured tailoring, and `resume.use_tailored_when_applying`
        is not false — freshly rendered so edits are always reflected;
      * otherwise profile.resume_path (the uploaded PDF).
    Never raises; falls back to the uploaded file on any rendering problem.
    """
    base = profile.get("resume_path", "") or ""
    try:
        if not has_structured_resume(profile):
            return base
        if (profile.get("resume") or {}).get("use_tailored_when_applying", True) is False:
            return base
        if tailored is None:
            from utils.tracker import get_tailored_resume
            tailored = get_tailored_resume(job["id"])
        if not tailored or not (tailored.get("tailored_summary") or tailored.get("tailored_experience")):
            return base
        return str(write_tailored_pdf(profile, job, tailored))
    except Exception as exc:  # pragma: no cover — defensive
        print(f"  ⚠ Tailored resume render failed, using uploaded PDF: {exc}")
        return base


def profile_with_resume(profile: dict, path: str) -> dict:
    """Shallow copy of profile pointing resume_path at `path` (adapters read that key)."""
    if not path or path == profile.get("resume_path"):
        return profile
    p = dict(profile)
    p["resume_path"] = path
    return p
