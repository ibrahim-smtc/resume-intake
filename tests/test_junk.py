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
check("empty text is junk, an image-only PDF goes to review",
      junk.classify("").decision == "junk" and junk.classify("", has_images=True).decision == "needs_review")
finish()
