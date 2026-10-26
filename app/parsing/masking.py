"""perfox-parsers/masking.py
Masks a resume PDF the way Questlight's parsing-ms POST /parsing/mask does: the ORIGINAL PDF is edited in place. The page looks the same,
the text stays searchable, and each contact detail is replaced by blank space. No AI is involved, so it costs no tokens.

What is removed (measured on Questlight's real service with invented PDFs):
  - every e-mail address, with any ending and wherever it is (header, a referee, inside a sentence);
  - every phone number of 10 or more digits in any usual form, and an extension such as "ext 22" right after it;
  - LinkedIn PROFILE links (linkedin.com/in/...) and GitHub links (github.com/...), whole;
  - every clickable link on every page.
What is kept: the name, the address, the date of birth, LinkedIn company pages and every other link's text, numbers under 10 digits, year
ranges, and the PDF's metadata (the author and title fields are left as they are, as in Questlight's mask).
This is stricter than Questlight's mask in one way: an e-mail whose pieces the PDF draws as separate words is masked too.

How: PDFium (pypdfium2, permissive licence) gives every piece of text on the page and lets one be rewritten. A PDF often draws each word as
its own piece, so the pieces are first grouped into lines and the contact details are searched for in the whole line, then blanked piece by piece.
"""
import ctypes
import io
import re
import threading

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from app.parsing.fix_normlization import phone_spans

LINK_ANNOTATION = 2  # PDFium's annotation type number for a link (this library has no name for it)
SAME_LINE = 3.0      # points: pieces whose tops differ by less than this are on one line
TOUCHING = 1.5       # points: pieces closer together than this are joined with no space

# An e-mail. Up to two spaces around the @ are allowed because a PDF can draw "meera.nair " and "@example.com" as two pieces. The ending must be
# letters (2 or more), so "call me @ 5.30pm" is not taken for one.
EMAIL_RE = re.compile(r"[\w.+-]+[ \t]{0,2}@[ \t]{0,2}[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
EXTENSION_RE = re.compile(r"[ \t]*(?:ext\.?|extension)[ \t]*\d{1,6}", re.I)
# linkedin.com/in/<handle> and github.com/<anything>, with or without https:// and a sub-domain such as www. Not a company page, not "mygithub.com".
LINK_RE = re.compile(r"(?<![\w-])(?:https?://)?(?:[\w-]+\.)*(?:linkedin\.com/in/|github\.com/)\S+", re.I)
TRAILING = ".,;:)]}'\""

_pdfium_lock = threading.Lock()  # PDFium is not thread-safe: one file is masked at a time (it takes well under a second)


class CannotOpen(Exception):
    """The bytes are not a PDF that can be opened (damaged, or protected by a password)."""


def find_sensitive(text: str) -> list[tuple[int, int]]:
    """(start, end) of every piece of contact detail in the text, overlapping ones merged. Nothing else is ever marked."""
    spans: list[tuple[int, int]] = [match.span() for match in EMAIL_RE.finditer(text)]
    for match in LINK_RE.finditer(text):
        spans.append((match.start(), match.start() + len(match.group(0).rstrip(TRAILING))))
    for start, end in phone_spans(text):
        extension = EXTENSION_RE.match(text, end)
        spans.append((start, extension.end() if extension else end))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


# ---------- the text on a page ----------

class _Piece:
    """One piece of text the PDF draws, with where it sits on the page."""
    __slots__ = ("obj", "text", "left", "right", "top")

    def __init__(self, obj, text: str, left: float, right: float, top: float):
        self.obj, self.text, self.left, self.right, self.top = obj, text, left, right, top


def _pieces(page, textpage) -> list[_Piece]:
    pieces = []
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_TEXT]):
        size = raw.FPDFTextObj_GetText(obj.raw, textpage.raw, None, 0)  # the byte length, counting the end mark
        if size <= 2:
            continue
        buffer = (ctypes.c_ushort * (size // 2))()
        raw.FPDFTextObj_GetText(obj.raw, textpage.raw, ctypes.cast(buffer, raw.FPDF_WIDESTRING), size)
        text = bytes(buffer).decode("utf-16-le", errors="ignore").rstrip("\x00")
        if not text.strip():
            continue
        left, bottom, right, top = (ctypes.c_float() for _ in range(4))
        raw.FPDFPageObj_GetBounds(obj.raw, left, bottom, right, top)
        pieces.append(_Piece(obj, text, left.value, right.value, top.value))
    return pieces


def _lines(pieces: list[_Piece]) -> list[list[_Piece]]:
    """Pieces on the same line (the same height, give or take a little), each line from left to right."""
    lines: list[list[_Piece]] = []
    for piece in sorted(pieces, key=lambda p: -p.top):
        if lines and abs(lines[-1][0].top - piece.top) < SAME_LINE:
            lines[-1].append(piece)
        else:
            lines.append([piece])
    for line in lines:
        line.sort(key=lambda p: p.left)
    return lines


def _join(line: list[_Piece]) -> tuple[str, list[int]]:
    """The text of a line, and where each piece starts in it. A space is put between two pieces unless they touch."""
    text, starts = "", []
    for index, piece in enumerate(line):
        if index and piece.left - line[index - 1].right >= TOUCHING:
            text += " "
        starts.append(len(text))
        text += piece.text
    return text, starts


def _set_text(piece: _Piece, text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    buffer = (ctypes.c_ushort * (len(data) // 2)).from_buffer_copy(data)
    raw.FPDFText_SetText(piece.obj.raw, ctypes.cast(buffer, raw.FPDF_WIDESTRING))


def _blank_line(line: list[_Piece]) -> int:
    """Blanks every contact detail on the line (each removed character becomes a space, so nothing moves). Returns how many were found."""
    text, starts = _join(line)
    spans = find_sensitive(text)
    if not spans:
        return 0
    covered = {i for start, end in spans for i in range(start, end)}
    for piece, start in zip(line, starts):
        chars = list(piece.text)
        changed = False
        for offset, char in enumerate(chars):
            if start + offset in covered and not char.isspace():
                chars[offset] = " "
                changed = True
        if changed:
            _set_text(piece, "".join(chars))
    return len(spans)


def _remove_links(page) -> None:
    """Removes every link annotation (mailto:, tel:, web links). Other annotations, such as form fields, are left alone."""
    for index in range(raw.FPDFPage_GetAnnotCount(page.raw) - 1, -1, -1):
        annotation = raw.FPDFPage_GetAnnot(page.raw, index)
        if not annotation:
            continue
        kind = raw.FPDFAnnot_GetSubtype(annotation)
        raw.FPDFPage_CloseAnnot(annotation)
        if kind == LINK_ANNOTATION:
            raw.FPDFPage_RemoveAnnot(page.raw, index)


def _has_leftovers(masked: bytes) -> bool:
    """Reads the finished PDF again and says whether any contact detail can still be found in its text."""
    doc = pdfium.PdfDocument(masked)
    try:
        for index in range(len(doc)):
            page = doc[index]
            textpage = page.get_textpage()
            try:
                pieces = _pieces(page, textpage)
            finally:
                textpage.close()
            if any(find_sensitive(_join(line)[0]) for line in _lines(pieces)):
                return True
        return False
    finally:
        doc.close()


def mask_pdf(data: bytes) -> tuple[bytes, int, list[str]]:
    """Returns (the masked PDF, how many contact details were blanked, warnings). Warnings never stop the file being returned:
    "no text layer" (a scan: nothing could be found to mask) or "leftovers" (a contact detail could still be found afterwards).
    Raises CannotOpen when the bytes are not a PDF that can be opened."""
    with _pdfium_lock:
        try:
            doc = pdfium.PdfDocument(data)
        except Exception as exc:  # PDFium raises its own error for damaged or password-protected files
            raise CannotOpen(type(exc).__name__) from exc
        count, any_text = 0, False
        try:
            for index in range(len(doc)):
                page = doc[index]
                textpage = page.get_textpage()
                try:
                    pieces = _pieces(page, textpage)
                finally:
                    textpage.close()
                any_text = any_text or bool(pieces)
                for line in _lines(pieces):
                    count += _blank_line(line)
                _remove_links(page)
                page.gen_content()
            out = io.BytesIO()
            doc.save(out)
        finally:
            doc.close()
        masked = out.getvalue()
        warnings: list[str] = []
        if not any_text:
            warnings.append("no text layer")
        elif _has_leftovers(masked):
            warnings.append("leftovers")
        return masked, count, warnings
