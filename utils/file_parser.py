"""Extract plain text from uploaded CV files (PDF, DOCX) held in memory."""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PdfReadError

PDF_MAGIC = b"%PDF"
DOCX_MAGIC = b"PK\x03\x04"
ALLOWED_EXTENSIONS = {".pdf", ".docx"}

DOCX_HEADER_START = "--- DOCX_HEADER ---"
DOCX_HEADER_END = "--- END_HEADER ---"

LINKEDIN_DOMAIN_RE = re.compile(r"linkedin\.com", re.IGNORECASE)
# Anchor words that often hide a hyperlink target in PDF/DOCX exports.
PDF_LINKEDIN_ANCHOR_RE = re.compile(
    r"(?<!\[)"
    r"(LinkedIn(?:\s+Profile)?|linkedin|קישור(?:\s+לינקדאין)?|פרופיל)"
    r"(?!\s*\[)(?!\s*https?://)",
    re.IGNORECASE,
)


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.replace("\x00", "").strip()


def _detect_file_type(filename: str, file_bytes: bytes) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in ALLOWED_EXTENSIONS:
        return ext

    if file_bytes.startswith(PDF_MAGIC):
        return ".pdf"
    if file_bytes.startswith(DOCX_MAGIC):
        return ".docx"

    raise ValueError(
        f"Unsupported file type: {ext or 'unknown'}. Only PDF and DOCX are supported."
    )


def _decode_pdf_string(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_object"):
        value = value.get_object()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _format_inline_hyperlink(display_text: str, url: str) -> str:
    """
    Render visible label with destination URL for Claude parsing.

    Format: "LinkedIn [https://www.linkedin.com/in/...]" so anchor text and
    target stay paired when PDF/DOCX only show the label visually.
    """
    display = display_text.strip()
    url = url.strip()
    if not url or url.startswith("#"):
        return display
    if not display:
        if LINKEDIN_DOMAIN_RE.search(url):
            return f"LinkedIn [{url}]"
        return url
    if display == url or url.lower() in display.lower():
        return display if display.lower().startswith("http") else f"{display} [{url}]"
    if f"[{url}]" in display:
        return display
    return f"{display} [{url}]"


def _iter_pdf_page_annotations(page: Any) -> Iterable[Any]:
    """Yield resolved annotation dictionaries for a PDF page."""
    annotations = getattr(page, "annotations", None)
    if annotations:
        for annot in annotations:
            yield annot.get_object() if hasattr(annot, "get_object") else annot
        return

    raw_annots = page.get("/Annots")
    if not raw_annots:
        return
    for ref in raw_annots:
        yield ref.get_object() if hasattr(ref, "get_object") else ref


def _uri_from_link_annotation(annot: Any) -> str | None:
    """Extract external URI from a PDF /Link annotation, if present."""
    subtype = annot.get("/Subtype")
    if subtype and str(subtype) != "/Link":
        return None

    action = annot.get("/A")
    if action is None:
        return None
    if hasattr(action, "get_object"):
        action = action.get_object()

    action_type = action.get("/S")
    if action_type and str(action_type) != "/URI":
        return None

    uri = action.get("/URI")
    if uri is None:
        return None
    uri_str = _decode_pdf_string(uri).strip()
    return uri_str or None


def _extract_pdf_page_hyperlinks(page: Any) -> list[str]:
    """Collect unique external URIs from page link annotations."""
    seen: set[str] = set()
    uris: list[str] = []
    for annot in _iter_pdf_page_annotations(page):
        uri = _uri_from_link_annotation(annot)
        if uri and uri not in seen and not uri.startswith("#"):
            seen.add(uri)
            uris.append(uri)
    return uris


def _url_already_inlined(text: str, url: str) -> bool:
    return f"[{url}]" in text or url in text


def _inject_linkedin_url_after_anchor(text: str, url: str) -> tuple[str, bool]:
    """Try to append [url] immediately after a LinkedIn-like anchor word in page text."""
    for match in PDF_LINKEDIN_ANCHOR_RE.finditer(text):
        end = match.end()
        window = text[end : end + 80]
        if _url_already_inlined(window, url):
            continue
        return text[:end] + f" [{url}]" + text[end:], True
    return text, False


def _inject_pdf_hyperlinks_into_text(page_text: str, hyperlinks: list[str]) -> str:
    """
    Merge PDF link annotation URIs into visible text using Label [URL] format.

    LinkedIn URLs are paired with anchor words when present; otherwise a
    standalone "LinkedIn [url]" line is appended so Claude still sees the target.
    """
    text = page_text
    for url in hyperlinks:
        if _url_already_inlined(text, url):
            continue

        if LINKEDIN_DOMAIN_RE.search(url):
            text, injected = _inject_linkedin_url_after_anchor(text, url)
            if not injected:
                text = f"{text.rstrip()}\nLinkedIn [{url}]".strip()
        elif not _url_already_inlined(text, url):
            text = f"{text.rstrip()}\n[{url}]".strip()

    return text


def _format_pdf_page_text(page_text: str, hyperlinks: list[str]) -> str:
    if not hyperlinks:
        return page_text.strip()
    merged = _inject_pdf_hyperlinks_into_text(page_text, hyperlinks)
    return merged.strip()


def _extract_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(file_bytes))
    except PdfReadError as exc:
        raise ValueError(f"Could not read PDF: {exc}") from exc

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("PDF is encrypted and cannot be read without a password.")
        except PdfReadError as exc:
            raise ValueError(f"Could not decrypt PDF: {exc}") from exc

    parts: list[str] = []
    has_hyperlinks = False
    for page in reader.pages:
        text = page.extract_text() or ""
        hyperlinks = _extract_pdf_page_hyperlinks(page)
        if hyperlinks:
            has_hyperlinks = True
        page_block = _format_pdf_page_text(text, hyperlinks)
        if page_block.strip():
            parts.append(page_block)

    result = _normalize_text("\n\n".join(parts))
    if not result and not has_hyperlinks:
        raise ValueError(
            "No text could be extracted from the PDF. "
            "The file may be scanned or image-only."
        )
    return result


def _text_from_xml_nodes(parent: Any) -> str:
    """Concatenate all w:t text nodes under an XML element."""
    chunks: list[str] = []
    for node in parent.iter(qn("w:t")):
        if node.text:
            chunks.append(node.text)
    return "".join(chunks)


def _resolve_docx_hyperlink_url(hyperlink_elem: Any, part: Any) -> str | None:
    """Resolve w:hyperlink r:id (or anchor) to a URL string."""
    rel_id = hyperlink_elem.get(qn("r:id"))
    if rel_id and rel_id in part.rels:
        rel = part.rels[rel_id]
        if rel.is_external:
            return rel.target_ref
    anchor = hyperlink_elem.get(qn("w:anchor"))
    if anchor:
        return f"#{anchor}"
    return None


def _paragraph_text_with_hyperlinks(paragraph: Paragraph) -> str:
    """Extract paragraph text, injecting hyperlink targets as Label [URL]."""
    part = paragraph.part
    pieces: list[str] = []
    for child in paragraph._element:
        tag = child.tag
        if tag == qn("w:hyperlink"):
            link_text = _text_from_xml_nodes(child)
            url = _resolve_docx_hyperlink_url(child, part)
            if url:
                pieces.append(_format_inline_hyperlink(link_text, url))
            elif link_text:
                pieces.append(link_text)
        elif tag == qn("w:r"):
            run_text = _text_from_xml_nodes(child)
            if run_text:
                pieces.append(run_text)
        elif tag in {qn("w:ins"), qn("w:smartTag"), qn("w:sdt")}:
            nested = _text_from_xml_nodes(child)
            if nested:
                pieces.append(nested)
    return "".join(pieces)


def _docx_paragraphs_text(paragraphs: Iterable[Paragraph]) -> list[str]:
    lines: list[str] = []
    for para in paragraphs:
        text = _paragraph_text_with_hyperlinks(para)
        if text.strip():
            lines.append(text)
    return lines


def _docx_table_cell_lines(table: Table) -> list[str]:
    """Extract text from table rows/cells, preserving cell block order."""
    lines: list[str] = []
    for row in table.rows:
        for cell in row.cells:
            lines.extend(_docx_block_container_lines(cell))
    return lines


def _docx_block_container_lines(container: Any) -> list[str]:
    """Yield paragraph and table lines from a document, header, footer, or cell."""
    lines: list[str] = []
    for block in container.iter_inner_content():
        if isinstance(block, Paragraph):
            text = _paragraph_text_with_hyperlinks(block)
            if text.strip():
                lines.append(text)
        elif isinstance(block, Table):
            lines.extend(_docx_table_cell_lines(block))
    return lines


def _docx_section_marginal_lines(doc: Document) -> list[str]:
    """Collect unique lines from section headers, first-page headers, and footers."""
    lines: list[str] = []
    seen: set[str] = set()
    for section in doc.sections:
        containers: list[Any] = [section.header]
        if section.different_first_page_header_footer:
            containers.append(section.first_page_header)
        containers.append(section.footer)
        for container in containers:
            for line in _docx_block_container_lines(container):
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
    return lines


def _extract_docx(file_bytes: bytes) -> str:
    try:
        doc = Document(BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError(f"Could not read DOCX: {exc}") from exc

    parts: list[str] = []
    marginal_lines = _docx_section_marginal_lines(doc)
    if marginal_lines:
        parts.append(DOCX_HEADER_START)
        parts.extend(marginal_lines)
        parts.append(DOCX_HEADER_END)

    parts.extend(_docx_block_container_lines(doc))

    result = _normalize_text("\n".join(parts))
    if not result:
        raise ValueError("No text could be extracted from the DOCX file.")
    return result


def validate_cv_attachment_bytes(filename: str, file_bytes: bytes) -> str | None:
    """Return ``.pdf`` / ``.docx`` when magic bytes match, else ``None``."""
    if not file_bytes:
        return None
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return None
    if file_bytes.startswith(PDF_MAGIC):
        return ".pdf"
    if file_bytes.startswith(DOCX_MAGIC):
        return ".docx"
    return None


_CV_SIGNAL_KEYWORDS_RE = re.compile(
    r"(קורות\s*חיים|ניסיון|השכלה|תעסוקה|מיומנויות|"
    r"education|experience|skills|employment|resume|curriculum\s*vitae|"
    r"work\s*history|professional)",
    re.IGNORECASE,
)

_BOILERPLATE_LINE_RE = re.compile(
    r"(unsubscribe|opt[\s-]?out|confidential|privacy\s*policy|"
    r"כל\s*הזכויות|הודעה\s*זו\s*נשלחה|להסרה\s*מהרשימה)",
    re.IGNORECASE,
)


def is_plausible_cv_text(text: str) -> bool:
    """Heuristic check that extracted text resembles a CV, not a signature or footer."""
    normalized = _normalize_text(text)
    if not normalized:
        return False

    if not _CV_SIGNAL_KEYWORDS_RE.search(normalized):
        return False

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return False

    boilerplate_lines = sum(1 for line in lines if _BOILERPLATE_LINE_RE.search(line))
    if boilerplate_lines / len(lines) > 0.6:
        return False

    return True


def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """
    Extract plain text from an uploaded file held in memory.

    Hyperlink destinations are preserved for downstream Claude parsing:
    - PDF: /Link annotation URIs are inlined as "Anchor [https://...]" or
      "LinkedIn [https://...]" when no anchor text is detectable on the page.
    - DOCX: w:hyperlink relationship targets are inlined as "Label [URL]".

    Args:
        file_bytes: Raw file content from an API upload.
        filename: Original filename (used to detect PDF vs DOCX).

    Returns:
        Normalized plain-text string.

    Raises:
        ValueError: Empty file, unsupported type, or extraction failure.
    """
    if not file_bytes:
        raise ValueError("Empty file.")

    file_type = _detect_file_type(filename, file_bytes)

    if file_type == ".pdf":
        return _extract_pdf(file_bytes)
    if file_type == ".docx":
        return _extract_docx(file_bytes)

    raise ValueError(f"Unsupported file type: {file_type}")


async def download_cv_from_url(url: str, *, timeout: float = 60.0) -> bytes:
    """
    Download CV file bytes from a Monday.com asset public_url or any HTTP(S) URL.

    Raises:
        ValueError: On HTTP error or empty response body.
    """
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        response = await client.get(url)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"Failed to download CV from URL (HTTP {exc.response.status_code})"
            ) from exc

    if not response.content:
        raise ValueError("Downloaded CV file is empty.")
    return response.content
