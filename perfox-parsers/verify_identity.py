"""perfox-parsers/verify_identity.py
Checks the name, e-mail and phone number Perfox returned against the text of the document itself.

Perfox seems to read a PDF as a picture, so it can misread a letter ("Iyer" becomes "lyer"), change an e-mail by a character, or
tidy a phone number (dropping "+91", closing up a space). The text layer of the PDF has no such mistakes, so each value is compared
with it and replaced by the document's own spelling when they differ slightly. A value that matches nothing in the text is left alone:
nothing is ever invented.
"""
import re
from difflib import SequenceMatcher

from fix_normlization import EMAIL_RE, phone_numbers

MATCH_AT = 0.85   # how alike two spellings must be (0 to 1) before one is taken for a misreading of the other
TOP_LINES = 15    # a name is read from the top of the document only
# Letters a picture of text gets mixed up: l, I, 1 and | look alike, so do 0 and O. Used only to compare, never to change a value.
_LOOKALIKE = str.maketrans({"l": "i", "1": "i", "|": "i", "0": "o"})


def _canon(value: str) -> str:
    return " ".join(value.casefold().split()).translate(_LOOKALIKE)


def _fix_name(name, text: str) -> str | None:
    name = " ".join(str(name or "").split())
    if not name or name.casefold() in " ".join(text.split()).casefold():
        return None  # nothing to check, or the name is in the text already (whatever its capitals)
    words = len(name.split())
    best, best_score = None, 0.0
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()][:TOP_LINES]:
        tokens = line.split()
        for start in range(len(tokens) - words + 1):
            candidate = " ".join(tokens[start:start + words]).strip(" ,;:|-")
            if not candidate or any(ch.isdigit() or ch == "@" for ch in candidate):
                continue  # a name has no digits and no @: this is a contact line
            score = SequenceMatcher(None, _canon(name), _canon(candidate)).ratio()
            if score > best_score:
                best, best_score = candidate, score
    return best if best and best_score >= MATCH_AT and best != name else None


def _emails_in(text: str) -> list[str]:
    found: list[str] = []
    for match in EMAIL_RE.finditer(re.sub(r"\s*@\s*", "@", text)):  # "name @ site.com" and a break after the @ still count
        email = match.group(0).rstrip(".,;:)")
        if email and email not in found:
            found.append(email)
    return found


def _fix_email(email, text: str) -> str | None:
    email = str(email or "").strip()
    found = _emails_in(text)
    if not email or not found or email.casefold() in {e.casefold() for e in found}:
        return None  # nothing to check, nothing to compare with, or it is in the text already
    best = max(found, key=lambda e: SequenceMatcher(None, email.casefold(), e.casefold()).ratio())
    return best if SequenceMatcher(None, email.casefold(), best.casefold()).ratio() >= MATCH_AT else None


def _fix_phone(phone, text: str) -> str | None:
    phone = str(phone or "").strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 10:
        return None
    for candidate in phone_numbers(text):
        if re.sub(r"\D", "", candidate)[-10:] == digits[-10:]:  # the same subscriber number, however it was written
            return None if candidate == phone else candidate  # use the document's own spelling: "+91" and spacing as written
    return None


def verify_identity(parsed: dict, text: str) -> tuple[dict, list[str]]:
    """Returns (parsed, the names of the fields that were corrected). Never raises on odd input, and never reports a value."""
    if not isinstance(parsed, dict) or not isinstance(text, str) or not text.strip():
        return parsed, []
    corrected: list[str] = []
    for key, check in (("name", _fix_name), ("email", _fix_email), ("phoneNumber", _fix_phone)):
        try:
            new = check(parsed.get(key), text)
        except (TypeError, ValueError, AttributeError):
            new = None  # a check that cannot read a value leaves it as it was
        if new:
            parsed[key] = new
            corrected.append(key)
    return parsed, corrected
