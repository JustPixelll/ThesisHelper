"""Local PDF/DOCX text extraction with honest, reproducible locations.

PDF pages are physical (one based); embedded PDF page labels are not assumed to
be printed page numbers. DOCX has no stable page layout, so it uses paragraphs
and tables. OCR is optional and runs only through a local Tesseract executable.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


class ExtractionCancelled(Exception):
    """The caller cancelled a local extraction."""


@dataclass
class Extraction:
    units: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    incomplete: bool = False


def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t \u00a0]+", " ", text)
    text = re.sub(r"\n[ ]+", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ExtractionCancelled("Indexing cancelled.")


def _short_metadata(value: Any) -> str:
    return clean_text(str(value or ""))[:1000]


def _ocr_support() -> tuple[Any, Any, str | None]:
    try:
        import pypdfium2
        import pytesseract
    except ImportError:
        return None, None, "OCR is unavailable. Install requirements-ocr.txt and local Tesseract to read scanned pages."
    executable = os.environ.get("THESISHELPER_TESSERACT") or shutil.which("tesseract")
    if not executable and os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidate = Path(base) / "Tesseract-OCR" / "tesseract.exe"
                if candidate.is_file():
                    executable = str(candidate)
                    break
    if not executable:
        return None, None, "OCR is unavailable: local Tesseract was not found. Set THESISHELPER_TESSERACT to tesseract.exe."
    pytesseract.pytesseract.tesseract_cmd = executable
    return pypdfium2, pytesseract, None


def _extract_pdf(path: Path, ocr: bool, cancel: Event | None) -> Extraction:
    result = Extraction()
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Password-protected PDF. Save an unlocked copy in the ingest folder.")
    result.pages = len(reader.pages)
    if reader.metadata:
        result.metadata = {
            "title": _short_metadata(reader.metadata.title),
            "authors": _short_metadata(reader.metadata.author),
        }
    labels: list[str] | None = None
    try:
        if "/PageLabels" in reader.trailer["/Root"]:
            labels = reader.page_labels
    except Exception:
        result.warnings.append("Embedded PDF page labels could not be read; physical page numbers remain available.")
    pdfium = pytesseract = rendered = None
    if ocr:
        pdfium, pytesseract, warning = _ocr_support()
        if warning:
            result.warnings.append(warning)
    sparse_pages: list[int] = []
    failed_pages: list[int] = []
    ocr_pages: list[int] = []
    ocr_failed: list[int] = []
    try:
        for index, page in enumerate(reader.pages):
            _check_cancel(cancel)
            try:
                text = clean_text(page.extract_text() or "")
            except Exception:
                text = ""
                failed_pages.append(index + 1)
            if len(text) < 40 and pdfium is not None:
                try:
                    if rendered is None:
                        rendered = pdfium.PdfDocument(str(path))
                    rendered_page = rendered[index]
                    bitmap = None
                    try:
                        bitmap = rendered_page.render(scale=3)
                        picture = bitmap.to_pil()
                        try:
                            ocr_text = clean_text(pytesseract.image_to_string(
                                picture, lang=os.environ.get("THESISHELPER_OCR_LANG", "eng"), timeout=90,
                            ))
                        finally:
                            picture.close()
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        rendered_page.close()
                    if len(ocr_text) > len(text):
                        text = ocr_text
                        ocr_pages.append(index + 1)
                except Exception:
                    ocr_failed.append(index + 1)
            if len(text) < 40:
                sparse_pages.append(index + 1)
            if text:
                result.units.append({
                    "text": text, "kind": "pdf", "page": index + 1,
                    "page_label": str(labels[index]) if labels and index < len(labels) else None,
                    "paragraph_start": None, "paragraph_end": None,
                })
    finally:
        if rendered is not None:
            rendered.close()
    for pages, message in (
        (sparse_pages, "Pages with little or no extractable text (possibly blank or scanned)"),
        (failed_pages, "PDF text extraction failed on physical pages"),
        (ocr_failed, "Local OCR failed on physical pages"),
    ):
        if pages:
            result.incomplete = True
            sample = ", ".join(map(str, pages[:30])) + (" …" if len(pages) > 30 else "")
            result.warnings.append(f"{message}: {sample} ({len(pages)} total).")
    if ocr_pages:
        result.warnings.append(f"Local OCR supplied text on {len(ocr_pages)} page(s). Check quotations against the page images.")
    if not any(len(unit["text"]) >= 40 for unit in result.units):
        result.incomplete = True
        result.warnings.append("No substantial searchable text was found. This document may require OCR.")
    result.warnings.append("PDF tables, equations and multi-column reading order may need visual verification.")
    return result


def _extract_docx(path: Path, cancel: Event | None) -> Extraction:
    result = Extraction()
    with ZipFile(path) as archive:
        # A deliberately bounded local parser: no external relationships fetched.
        if sum(info.file_size for info in archive.infolist()) > 500 * 1024 * 1024:
            raise ValueError("DOCX expands beyond the 500 MB safety limit. Split this document first.")
        for name, label in (("word/footnotes.xml", "Footnotes"), ("word/endnotes.xml", "Endnotes")):
            if name in archive.namelist() and re.search(rb"<w:t[ >]", archive.read(name)):
                result.incomplete = True
                result.warnings.append(f"{label} are not included in this DOCX text index. Use a PDF export to include their page context.")
    document = Document(str(path))
    result.metadata = {
        "title": _short_metadata(document.core_properties.title),
        "authors": _short_metadata(document.core_properties.author),
    }
    paragraph_number = table_number = 0
    heading = ""
    for element in document.element.body.iterchildren():
        _check_cancel(cancel)
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph_number += 1
            paragraph = Paragraph(element, document)
            text = clean_text(paragraph.text)
            if not text:
                continue
            if paragraph.style and paragraph.style.name.lower().startswith("heading"):
                heading = text[:200]
            result.units.append({
                "text": text, "kind": "paragraph", "page": None, "page_label": None,
                "paragraph_start": paragraph_number, "paragraph_end": paragraph_number,
                "heading": heading,
            })
        elif tag == "tbl":
            table_number += 1
            table = Table(element, document)
            for row_number, row in enumerate(table.rows, 1):
                _check_cancel(cancel)
                # Merged cells can repeat; retain table order but do not duplicate the same XML cell.
                seen: set[int] = set()
                values: list[str] = []
                for cell in row.cells:
                    identity = id(cell._tc)
                    if identity not in seen:
                        seen.add(identity)
                        values.append(clean_text(cell.text))
                text = " | ".join(values).strip(" |")
                if text:
                    result.units.append({
                        "text": text, "kind": "table", "page": None, "page_label": None,
                        "paragraph_start": None, "paragraph_end": None,
                        "table": table_number, "row_start": row_number, "row_end": row_number,
                        "heading": heading,
                    })
    if document.inline_shapes:
        result.incomplete = True
        result.warnings.append("Embedded images are not indexed from DOCX. Use a searchable PDF or local OCR for image text.")
    result.warnings.append("DOCX locations are body paragraph/table numbers, not Word page numbers. Headers, text boxes and equations are not indexed.")
    if not result.units:
        result.incomplete = True
        result.warnings.append("No searchable body text was found in this DOCX.")
    return result


def extract_document(path: Path, *, ocr: bool = False, cancel: Event | None = None) -> Extraction:
    """Extract one supported file. Exceptions are handled per file by Library.scan."""
    _check_cancel(cancel)
    if path.suffix.lower() == ".pdf":
        return _extract_pdf(path, ocr, cancel)
    if path.suffix.lower() == ".docx":
        return _extract_docx(path, cancel)
    raise ValueError("Only PDF and DOCX files are supported.")


def split_text(text: str, target: int = 3000, overlap: int = 300) -> list[str]:
    """Bound each embedding input; prefer sentence/word boundaries without losing text."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + target, len(text))
        # Any UTF-8 byte is an upper bound of one token for byte-based tokenizers.
        # This bounds even unusual Unicode passages without downloading a tokenizer.
        bounded = text[start:end].encode("utf-8")[:7500].decode("utf-8", errors="ignore")
        end = start + len(bounded)
        if end < len(text):
            lower = start + (end - start) // 2
            boundary = max(text.rfind("\n", lower, end), text.rfind(". ", lower, end), text.rfind(" ", lower, end))
            if boundary > lower:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        # Overlap begins at a word boundary where one is available.
        space = text.find(" ", next_start, end)
        start = space + 1 if space >= 0 else next_start
    return chunks


def chunk_units(units: list[dict[str, Any]], target: int = 3000) -> list[dict[str, Any]]:
    """Keep PDF pages separate; pack adjacent DOCX paragraphs with honest ranges."""
    result: list[dict[str, Any]] = []
    group: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal group
        if not group:
            return
        first, last = group[0], group[-1]
        combined = dict(first)
        combined["paragraph_end"] = last.get("paragraph_end")
        if first["kind"] == "table":
            combined["row_end"] = last.get("row_end")
        combined["text"] = "\n\n".join(part["text"] for part in group)
        for text in split_text(combined["text"], target=target):
            result.append({**combined, "text": text})
        group = []

    for unit in units:
        if unit["kind"] == "pdf":
            flush()
            for text in split_text(unit["text"], target=target):
                result.append({**unit, "text": text})
            continue
        if group:
            same_location = (unit["kind"] == group[0]["kind"] and unit.get("table") == group[0].get("table") and unit.get("heading") == group[0].get("heading"))
            if not same_location or sum(len(item["text"]) + 2 for item in group) + len(unit["text"]) > target:
                previous = group[-1]
                flush()
                # Repeating one short preceding paragraph avoids losing boundary context.
                if same_location and len(previous["text"]) <= 300 and len(unit["text"]) + len(previous["text"]) < target:
                    group.append(previous)
        group.append(unit)
        if len(unit["text"]) > target:
            flush()
    flush()
    return result
