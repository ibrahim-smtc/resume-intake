"""The "Junk?" block: decides what an uploaded file is, using hardcoded rules on the file's own text.

No API calls and no model. It runs before parsing, so spam never costs a parser call and a resume that is
merely missing contact details is not mistaken for junk.

Four outcomes, each with a reason (BRD FR-2):
  accepted         looks like a resume, go on to parse it
  job_description  looks like a job description (a JD or job ad): it goes to the JD intake (app/job_pipeline.py)
  junk             neither, drop it and log the reason
  needs_review     unclear, goes to the recruiter review queue (BRD FR-8)
signals["rule"] names the rule that decided, so a caller can tell hard junk (blank, invoice, spam) from a thin document.

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

# Job description signals: phrases an employer writes, and the section headings of a JD
JD_RE = re.compile(
    r"we('re| are) (hiring|looking for|seeking)|looking for an? |the (ideal )?candidate (will|should|must)|\byou will\b|\byou'll\b|"
    r"job (title|type|location|code|summary|description|role)\s*[:\-]|position\s*:|employment type|"
    r"(no\.? of |number of )?(openings|positions|vacanc(y|ies))\s*:|"
    r"\d+\s*(\+|-|to)?\s*\d*\s*(years|yrs)\.? (of )?(relevant |hands-on |total )?experience|experience\s*(required)?\s*:\s*\d|"
    r"must[- ]have|good[- ]to[- ]have|nice[- ]to[- ]have|\bctc\b|\blpa\b|salary|compensation|notice period|"
    r"apply now|how to apply|send your (resume|cv)|equal opportunity employer|reporting to|work mode|"
    r"(hybrid|remote|on-?site)( role| position| work)?\b|immediate joiners?|what we offer|benefits|perks", re.I)
JD_HEADINGS_RE = re.compile(
    r"^\W*((key |main |primary |job |roles? (and|&) )?responsibilities|(job |key |technical |minimum |basic )?requirements|"
    r"(required|preferred|minimum|basic|desired) (qualifications|skills)|skills required|"
    r"about (the |this )?(role|job|position|company|us)|job (description|summary|overview)|role (overview|summary)|"
    r"what you('ll| will) (do|bring)|what we('re| are) looking for|who you are|what we offer|benefits|perks|"
    r"(must|good|nice)[- ]to[- ]have|eligibility( criteria)?)\W*$", re.I)
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
        "jd": len(set(" ".join(m.group(0).lower().split()) for m in JD_RE.finditer(text)))
              + sum(1 for ln in lines if len(ln) <= 50 and JD_HEADINGS_RE.match(ln)),
        "cover_letter": len(set(m.group(0).lower() for m in COVER_LETTER_RE.finditer(text))),
    }


def _resume_score(s: dict) -> int:
    """How many independent resume signals are present."""
    return (len(s["headings"])
            + (1 if s["date_ranges"] else 0)
            + (1 if s["degrees"] else 0)
            + (1 if s["resume_word"] else 0)
            + (1 if s["email"] and s["phone"] else 0))


def looks_like_jd(s: dict) -> bool:
    """A job description: several employer phrases or JD headings, and more of them than resume signals. A file with no
    work-history dates (a resume nearly always has some) needs one signal more than the resume side."""
    return s["jd"] >= 3 and (s["jd"] > _resume_score(s) or (not s["date_ranges"] and s["jd"] >= 4))


HARD_JUNK = {"blank", "invoice", "spam"}  # rules that say "this is no document of ours", whatever the recruiter called it


def _decision(label: str, reason: str, s: dict, rule: str) -> Decision:
    s["rule"] = rule
    return Decision(label, reason, s)


def model_decide(text: str, signals: dict):
    """Slot for a decision model (Jev, Kev...) to settle the unclear cases. Should return an
    (accepted | junk, probability) pair, or None to leave the file in the review queue. Not wired up yet."""
    return None


def classify(text: str, has_images: bool = False) -> Decision:
    s = _signals(text)

    if s["words"] == 0:
        if has_images:
            return _decision("needs_review", "no readable text, looks like a scanned image of a document", s, "scanned")
        return _decision("junk", "blank document, there is no text in the file", s, "blank")
    score = _resume_score(s)
    junk_hits = s["spam"] + s["invoice"]

    if s["invoice"] >= 2 and score < 4:
        return _decision("junk", f"looks like an invoice or receipt ({s['invoice']} billing terms)", s, "invoice")
    if s["spam"] >= 2 and score < 4:
        return _decision("junk", f"looks like spam or a promotion ({s['spam']} promotional phrases)", s, "spam")

    # Before the length check: a short JD pasted from a chat is still a JD.
    if looks_like_jd(s):
        return _decision("job_description", f"looks like a job description ({s['jd']} job-description signals, "
                         f"{score} resume signals)", s, "jd")

    # A short file only counts as junk when it has no resume signals: a one-line CV is still a CV.
    if s["words"] < MIN_WORDS and score < 3:
        return _decision("junk", f"almost empty, only {s['words']} words", s, "tiny")

    if score >= 3 and junk_hits < score:
        return _decision("accepted", f"resume signals found: {score} (sections: {', '.join(s['headings']) or 'none'})", s, "resume")

    if s["cover_letter"] and score < 3:
        return _decision("needs_review", "looks like a cover letter with no resume content", s, "cover_letter")

    if score == 0 and junk_hits == 0:
        return _decision("junk", "no resume content found: no sections, dates or education", s, "no_content")

    decided = model_decide(text, s)
    if decided:
        label, prob = decided
        return _decision(label, f"decision model said {label} ({prob:.2f})", s, "model")
    return _decision("needs_review", f"unclear: {score} resume signal(s), {s['jd']} job-description signal(s), "
                     f"{junk_hits} junk signal(s)", s, "unclear")
