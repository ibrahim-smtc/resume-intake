"""Reading text out of an uploaded file, locally (no API calls): PDF, DOCX, plain text, and old Word .doc (best effort)."""
import html
import io
import re
import zipfile

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # the container old Word .doc files (and old Excel/PowerPoint) use
DOC_MIN_WORDS = 20  # a .doc that yields fewer words than this is treated as unreadable


def docx_text(data: bytes) -> str:
    """The text of a DOCX, paragraph by paragraph. Raises zipfile.BadZipFile or KeyError when it isn't a real DOCX."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    return html.unescape(re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n").replace("<w:tab/>", " ")))


def txt_text(data: bytes) -> str:
    """A plain-text file: UTF-8 (with or without a BOM), UTF-16 when it starts with a BOM, else Latin-1, which never fails."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "ignore")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def doc_text(data: bytes) -> str:
    """Best-effort text of an old Word 97-2003 .doc. Word stores the body as runs of 8-bit or UTF-16 characters inside a
    binary container; this keeps the readable runs and drops the rest. Good enough to recognise and parse a JD or a resume,
    not a faithful copy (tables and odd encodings can come out jumbled)."""
    runs = []
    for m in re.finditer(rb"(?:[\x20-\x7e\t\r\n][\x00]){6,}", data):  # UTF-16LE text
        runs.append(m.group(0).decode("utf-16le", "ignore"))
    if sum(len(r.split()) for r in runs) < DOC_MIN_WORDS:  # older files keep the text as plain 8-bit characters
        runs = [m.group(0).decode("latin-1") for m in re.finditer(rb"[\x20-\x7e\t\r\n\x91-\x97]{6,}", data)]
    keep = [r for r in (" ".join(x.split()) for x in "\n".join(runs).replace("\r", "\n").split("\n"))
            if len(r) > 2 and sum(c.isalpha() for c in r) >= len(r) * 0.5]   # skip binary noise that happens to be printable
    return "\n".join(keep)


def extract_text(data: bytes, ext: str):
    """Reads the text out of the file. Returns (text, has_images, error).

    has_images is only meaningful for a PDF: it tells a scanned document (text is in a picture) from a blank page."""
    try:
        if ext == ".docx":
            return docx_text(data), False, None
        if ext == ".txt":
            return txt_text(data), False, None
        if ext == ".doc":
            if not data.startswith(OLE_MAGIC):
                return "", False, "this .doc file isn't a Word document (corrupt, or renamed)"
            text = doc_text(data)
            if len(text.split()) < DOC_MIN_WORDS:
                return "", False, "couldn't read the text of this old Word .doc file: please send it as PDF or DOCX"
            return text, False, None
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return "", False, "this PDF is password-protected"
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        has_images = any(len(page.images) for page in reader.pages)
        return text, has_images, None
    except (zipfile.BadZipFile, KeyError):
        return "", False, "couldn't open this DOCX file (corrupt?)"
    except Exception as exc:  # pypdf raises many different errors on damaged PDFs
        return "", False, f"couldn't read this {ext.lstrip('.').upper()} file ({type(exc).__name__})"
