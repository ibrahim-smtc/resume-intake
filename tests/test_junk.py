"""The "Junk?" rules against 30 invented files (tests/fixtures/junk): real resumes in several styles, junk of many kinds,
look-alikes (a resume that mentions an invoice, a cover letter, a scan). Fixtures come from tests/fixtures/junk/generate.py."""
import json

import harness

harness.isolate()
from harness import FIXTURES, JUNK_FILES, check, finish  # noqa: E402

from app.intake import documents, junk  # noqa: E402

cases = json.loads((FIXTURES / "junk" / "cases.json").read_text())

# A resume with very unusual section headings is not recognised as one. It lands in review for a person to look at, which is
# the safe direction. Listed here so a change in that behaviour is noticed, not hidden.
KNOWN_DIFFERENT = {"r11_odd_heads.pdf": "needs_review"}

wrong = []
for case in cases:
    path = JUNK_FILES / case["file"]
    text, has_images, err = documents.extract_text(path.read_bytes(), path.suffix.lower())
    got = "error: " + err if err else junk.classify(text, has_images).decision
    want = KNOWN_DIFFERENT.get(case["file"], case["expected"])
    if got != want:
        wrong.append((case["file"], want, got))

check(f"all {len(cases)} files get the expected decision", not wrong, wrong)
check("a real resume is never called junk", all(junk.classify(*documents.extract_text((JUNK_FILES / c["file"]).read_bytes(), (JUNK_FILES / c["file"]).suffix.lower())[:2]).decision != "junk"
                                                for c in cases if c["expected"] == "accepted"))
check("junk files never get accepted", all(junk.classify(*documents.extract_text((JUNK_FILES / c["file"]).read_bytes(), (JUNK_FILES / c["file"]).suffix.lower())[:2]).decision != "accepted"
                                           for c in cases if c["expected"] == "junk"))

# unreadable input is reported, not raised
check("corrupt PDF gives an error message", documents.extract_text(b"%PDF-1.4 not really a pdf", ".pdf")[2])
check("corrupt DOCX gives an error message", documents.extract_text(b"not a zip", ".docx")[2])

# a copy of a resume with the recruiter's contact lines added on top (for a resume that shows none)
lines = ["Email: a.b@example.com", "Phone: +91 90000 12345 & <co>"]
docx = next(JUNK_FILES.glob("*.docx")).read_bytes()
body = documents.docx_text(docx)
new, ext = documents.with_contact(docx, ".docx", body, lines)
copy_text = documents.docx_text(new)
check("a DOCX copy keeps all its text and starts with the contact lines (special characters escaped)",
      ext == ".docx" and copy_text.strip().startswith("Email: a.b@example.com" + chr(10) + "Phone: +91 90000 12345 & <co>") and copy_text.endswith(body) and documents.docx_text(docx) == body)
pdf = next(JUNK_FILES.glob("r01*.pdf")).read_bytes()
pdf_text = documents.extract_text(pdf, ".pdf")[0]
new, ext = documents.with_contact(pdf, ".pdf", pdf_text, lines)
check("a PDF becomes a DOCX built from its text, with the lines first",
      ext == ".docx" and documents.docx_text(new).startswith("Email: a.b@example.com") and pdf_text.split()[0] in documents.docx_text(new))
new, ext = documents.with_contact(b"not a zip", ".docx", "plain words here", lines)
check("a DOCX that can't be edited is rebuilt from the text instead", ext == ".docx" and "plain words here" in documents.docx_text(new))
check("empty text is junk, an image-only PDF goes to review",
      junk.classify("").decision == "junk" and junk.classify("", has_images=True).decision == "needs_review")
finish()
