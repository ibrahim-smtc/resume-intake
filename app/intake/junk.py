"""The "Junk?" block: decides whether an uploaded file is a resume, using hardcoded rules on the file's own text.

No API calls and no model. It runs before parsing, so spam never costs a parser call and a resume that is
merely missing contact details is not mistaken for junk.

Three outcomes, each with a reason (BRD FR-2):
  junk          not a resume, drop it and log the reason
  accepted      looks like a resume, go on to parse it
  needs_review  unclear, goes to the recruiter review queue (BRD FR-8)

model_decide() is the empty slot for a decision model (Jev, Kev...) that would settle the unclear cases.
"""
import re
from collections import namedtuple

Decision = namedtuple("Decision", "decision reason signals")

MIN_WORDS = 40  # a resume has more words than this

# Resume signals
SECTIONS = {  # heading name -> words that show it as a heading line
    "experience": r"(work |professional |employment )?(experience|history)|employment",
    "education": r"education|academic|qualifications?",
    "skills": r"(technical |key |core )?skills|competenc(y|ies)|technologies",
    "summary": r"(professional |career )?(summary|profile|objective)|about me",
    "projects": r"projects?",
    "certifications": r"certifications?|courses|training",
    "achievements": r"achievements?|awards?|publications?",
}
SECTION_RES = {k: re.compile(rf"^\W*(?:{v})\W*$", re.I) for k, v in SECTIONS.items()}

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_RANGE_RE = re.compile(
    rf"(?:{MONTH}\s+)?(?:19|20)\d{{2}}\s*(?:-|–|—|to)\s*(?:(?:{MONTH}\s+)?(?:19|20)\d{{2}}|present|current|till date|now)",
    re.I)
DEGREE_RE = re.compile(
    r"\b(b\.?\s?tech|m\.?\s?tech|b\.?\s?e\b|b\.?\s?sc|m\.?\s?sc|b\.?\s?com|m\.?\s?com|b\.?\s?a\b|m\.?\s?a\b|bca|mca|mba|"
    r"ph\.?d|bachelor|master|diploma|b\.?\s?s\b|m\.?\s?s\b|hsc|ssc|cgpa|gpa)", re.I)
RESUME_WORD_RE = re.compile(r"\b(resume|résumé|curriculum vitae|\bcv\b)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{8,}\d)")

# Junk signals
SPAM_RE = re.compile(
    r"unsubscribe|click here|limited[- ]time|% off|\bfree trial\b|buy now|act now|you('ve| have) won|\bwinner\b|lottery|"
    r"special offer|exclusive deal|discount code|promo code|newsletter|view in browser|congratulations!|"
    r"earn \$|make money|crypto|investment opportunity|webinar|register now", re.I)
INVOICE_RE = re.compile(
    r"\binvoice\b|bill to|amount due|total due|\bsubtotal\b|\bgstin\b|purchase order|\breceipt\b|payment due|"
    r"due date|\bqty\b|unit price|tax invoice|order (no|number|#)", re.I)
JOB_AD_RE = re.compile(
    r"we are (hiring|looking for)|job description|apply now|send your (resume|cv)|about the role|"
    r"what you('ll| will) do|who you are|equal opportunity employer|how to apply", re.I)
COVER_LETTER_RE = re.compile(
    r"\bdear (sir|madam|hiring|recruiter|team|mr|ms|mrs)|i am writing to (apply|express)|"
    r"please find (my|the) (attached|enclosed)|yours (sincerely|faithfully)|\bsincerely\b|kind regards|"
    r"i would like to apply", re.I)


def _signals(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    headings = sorted(name for name, rx in SECTION_RES.items() if any(len(ln) <= 40 and rx.match(ln) for ln in lines))
    return {
        "words": len(text.split()),
        "headings": headings,
        "date_ranges": len(DATE_RANGE_RE.findall(text)),
        "degrees": len(DEGREE_RE.findall(text)),
        "resume_word": bool(RESUME_WORD_RE.search(text)),
        "email": bool(EMAIL_RE.search(text)),
        "phone": bool(PHONE_RE.search(text)),
        "spam": len(set(m.group(0).lower() for m in SPAM_RE.finditer(text))),
        "invoice": len(set(m.group(0).lower() for m in INVOICE_RE.finditer(text))),
        "job_ad": len(set(m.group(0).lower() for m in JOB_AD_RE.finditer(text))),
        "cover_letter": len(set(m.group(0).lower() for m in COVER_LETTER_RE.finditer(text))),
    }


def _resume_score(s: dict) -> int:
    """How many independent resume signals are present."""
    return (len(s["headings"])
            + (1 if s["date_ranges"] else 0)
            + (1 if s["degrees"] else 0)
            + (1 if s["resume_word"] else 0)
            + (1 if s["email"] and s["phone"] else 0))


def model_decide(text: str, signals: dict):
    """Slot for a decision model (Jev, Kev...) to settle the unclear cases. Should return an
    (accepted | junk, probability) pair, or None to leave the file in the review queue. Not wired up yet."""
    return None


def classify(text: str, has_images: bool = False) -> Decision:
    s = _signals(text)

    if s["words"] == 0:
        if has_images:
            return Decision("needs_review", "no readable text, looks like a scanned image of a document", s)
        return Decision("junk", "blank document, there is no text in the file", s)
    score = _resume_score(s)
    junk_hits = s["spam"] + s["invoice"] + s["job_ad"]

    # A short file only counts as junk when it has no resume signals: a one-line CV is still a CV.
    if s["words"] < MIN_WORDS and score < 3:
        return Decision("junk", f"almost empty, only {s['words']} words", s)

    if s["invoice"] >= 2 and score < 4:
        return Decision("junk", f"looks like an invoice or receipt ({s['invoice']} billing terms)", s)
    if s["spam"] >= 2 and score < 4:
        return Decision("junk", f"looks like spam or a promotion ({s['spam']} promotional phrases)", s)
    if s["job_ad"] >= 2 and score < 4:
        return Decision("junk", "looks like a job advertisement, not a candidate's resume", s)

    if score >= 3 and junk_hits < score:
        return Decision("accepted", f"resume signals found: {score} (sections: {', '.join(s['headings']) or 'none'})", s)

    if s["cover_letter"] and score < 3:
        return Decision("needs_review", "looks like a cover letter with no resume content", s)

    if score == 0 and junk_hits == 0:
        return Decision("junk", "no resume content found: no sections, dates or education", s)

    decided = model_decide(text, s)
    if decided:
        label, prob = decided
        return Decision(label, f"decision model said {label} ({prob:.2f})", s)
    return Decision("needs_review", f"unclear: {score} resume signal(s), {junk_hits} junk signal(s)", s)
