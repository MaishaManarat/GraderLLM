"""PDF text extraction with markdown formatting (headings + tables), plus
optional vision-based description of diagrams/charts/flowcharts that plain
text extraction can't see at all.
"""

import logging
from pathlib import Path

import pdfplumber
from pypdf import PdfReader

import config
import providers

logger = logging.getLogger(__name__)

VISION_PROMPT = (
    "This is one page from a business/strategy proposal document. If this "
    "page contains a diagram, flowchart, chart, graph, org chart, or other "
    "visual figure, describe it in 2-4 concise sentences: what it shows, "
    "its overall structure or flow (e.g. 'Box A leads to Box B, which "
    "splits into C and D'), and any text labels visible in it. Focus only "
    "on the figure's actual content and meaning, not its visual style or "
    "colors. If this page is just body text/paragraphs with no meaningful "
    "diagram or chart, respond with exactly: No diagram."
)


def extract_text(pdf_path: str | Path) -> str:
    """Extract text from a PDF as markdown, trying pdfplumber first then pypdf.

    pdfplumber handles tables and is preferred; if it fails or yields nothing,
    we fall back to pypdf's raw text extraction. Raises ValueError if neither
    produces content.

    If config.PDF_IMAGE_HANDLING is "describe", pages that look like they
    contain a diagram/chart/photo (by embedded images or vector-graphic
    density - see _needs_vision_description) are rasterized and described by
    a vision model, with the description inserted inline. This is capped by
    config.PDF_IMAGE_MAX_PAGES_PER_DOC per document.

    As a side effect, writes the extracted markdown into config.MARKDOWN_DIR
    as <pdf_stem>.md.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    text = _extract_with_pdfplumber(pdf_path)
    if text:
        _save_output(pdf_path, text)
        return text

    logger.info("Falling back to pypdf extraction for %s", pdf_path)
    text = _extract_with_pypdf(pdf_path)
    if text:
        _save_output(pdf_path, text)
        return text

    raise ValueError(f"No extractable text or tables found in {pdf_path}")


def _extract_with_pdfplumber(pdf_path: Path) -> str:
    """Extract markdown from the PDF, including tables and (optionally)
    vision descriptions of any diagrams/charts found."""
    try:
        markdown_parts: list[str] = []
        vision_calls_used = 0
        fitz_doc = None  # opened lazily, only if a page actually needs it

        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                markdown_parts.append(f"\n\n## Page {page_num}\n")

                for table in page.extract_tables() or []:
                    markdown_parts.append("\n" + _table_to_markdown(table) + "\n")

                text = page.extract_text() or ""
                if text:
                    markdown_parts.append(text)

                if config.PDF_IMAGE_HANDLING == "off":
                    continue

                reason = _needs_vision_description(page, text)
                if not reason:
                    continue

                if vision_calls_used >= config.PDF_IMAGE_MAX_PAGES_PER_DOC:
                    logger.info(
                        "%s page %d looks like it has a diagram (%s), but the "
                        "per-document cap (PDF_IMAGE_MAX_PAGES_PER_DOC=%d) was "
                        "already reached - skipping description for this page",
                        pdf_path.name, page_num, reason, config.PDF_IMAGE_MAX_PAGES_PER_DOC,
                    )
                    continue

                if fitz_doc is None:
                    fitz_doc = _open_fitz(pdf_path)
                if fitz_doc is None:
                    continue  # PyMuPDF unavailable/failed; already logged

                description = _describe_page(fitz_doc, page_num - 1, reason, pdf_path.name)
                if description:
                    markdown_parts.append(f"\n[Figure on page {page_num}: {description}]\n")
                vision_calls_used += 1  # counts toward the cap whether or not it succeeded

        if fitz_doc is not None:
            fitz_doc.close()

        return "\n".join(markdown_parts).strip()
    except Exception as exc:
        logger.warning("pdfplumber extraction failed for %s: %s", pdf_path, exc)
        return ""


def _needs_vision_description(page, extracted_text: str) -> str | None:
    """Return a short reason string if this page looks like it contains a
    diagram/chart/photo worth describing, else None.

    Catches two cases plain text extraction misses entirely: embedded
    raster images (photos, screenshots, exported PNGs), and vector-drawn
    diagrams (PowerPoint SmartArt, Excel charts) which have no image object
    at all - they're page content operators, so image-object extraction
    alone (e.g. pdfimages) would miss them.
    """
    if page.images:
        return f"{len(page.images)} embedded image(s)"

    vector_shapes = len(page.rects) + len(page.lines) + len(page.curves)
    if vector_shapes >= config.PDF_VECTOR_SHAPE_THRESHOLD:
        return f"{vector_shapes} vector shapes (likely a chart/flowchart)"

    if vector_shapes > 0 and len(extracted_text.strip()) < config.PDF_SPARSE_TEXT_CHARS:
        return f"sparse text ({len(extracted_text.strip())} chars) with {vector_shapes} vector shapes"

    return None


def _open_fitz(pdf_path: Path):
    try:
        import pymupdf
        return pymupdf.open(pdf_path)
    except Exception as exc:
        logger.warning(
            "Could not open %s with PyMuPDF for page rasterization (%s); "
            "diagrams/charts in this PDF will not be described. Is the "
            "'pymupdf' package installed?",
            pdf_path, exc,
        )
        return None


def _describe_page(fitz_doc, page_index: int, reason: str, pdf_name: str) -> str:
    """Rasterize one page and ask the vision model to describe any diagram
    on it. Never raises - on any failure, logs a warning and returns ''."""
    try:
        import pymupdf

        page = fitz_doc[page_index]
        zoom = config.PDF_IMAGE_DPI / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        png_bytes = pix.tobytes("png")
    except Exception as exc:
        logger.warning(
            "%s page %d: could not rasterize for vision description (%s): %s",
            pdf_name, page_index + 1, reason, exc,
        )
        return ""

    try:
        return _call_vision_model(png_bytes)
    except Exception as exc:
        logger.warning(
            "%s page %d: vision description call failed (%s): %s",
            pdf_name, page_index + 1, reason, exc,
        )
        return ""


def _call_vision_model(png_bytes: bytes) -> str:
    text = providers.chat_vision(config.VISION_MODEL, VISION_PROMPT, png_bytes, max_tokens=300)
    if text.lower().startswith("no diagram"):
        return ""
    return text


def _extract_with_pypdf(pdf_path: Path) -> str:
    try:
        reader = PdfReader(pdf_path)
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip()
    except Exception as exc:
        logger.warning("pypdf fallback failed for %s: %s", pdf_path, exc)
        return ""


def _table_to_markdown(table: list[list]) -> str:
    """Render a pdfplumber table (list of rows) as a GitHub-style markdown table."""
    lines: list[str] = []
    for row_idx, row in enumerate(table):
        cells = [str(c).replace("\n", " ").strip() if c else "" for c in row]
        lines.append("| " + " | ".join(cells) + " |")
        if row_idx == 0:
            lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
    return "\n".join(lines)


def _save_output(pdf_path: Path, content: str) -> Path:
    """Write the extracted markdown into config.MARKDOWN_DIR as <pdf_stem>.md."""
    markdown_dir = Path(config.MARKDOWN_DIR)
    markdown_dir.mkdir(parents=True, exist_ok=True)

    output_file = markdown_dir / f"{pdf_path.stem}.md"
    try:
        output_file.write_text(content, encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not save markdown for %s: %s", pdf_path, exc)
    return output_file
