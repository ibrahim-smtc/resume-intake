"""Reading text out of an uploaded PDF or DOCX, locally (no API calls)."""
import html
import io
import re
import zipfile


def docx_text(data: bytes) -> str:
    """The text of a DOCX, paragraph by paragraph. Raises zipfile.BadZipFile or KeyError when it isn't a real DOCX."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    return html.unescape(re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n").replace("<w:tab/>", " ")))


def extract_text(data: bytes, ext: str):
    """Reads the text out of the file. Returns (text, has_images, error).

    has_images is only meaningful for a PDF: it tells a scanned resume (text is in a picture) from a blank page."""
    try:
        if ext == ".docx":
            return docx_text(data), False, None
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
