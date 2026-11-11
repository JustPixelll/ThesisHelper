"""Local thesis documents, revision history, and conservative Word interchange.

The editor JSON is authoritative. A Word mirror is generated on each save, but a
Word file lock must never discard a successful text edit. Imported originals are
kept separately and are never modified.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.table import Table
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run


MAX_CHARACTERS = 2_000_000
MAX_NODES = 100_000
MAX_DEPTH = 40
MAX_IMPORT_BYTES = 100 * 1024 * 1024
_ID = re.compile(r"^[a-f0-9]{32}$")
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_BLOCKS = {
    "paragraph", "heading", "bulletList", "orderedList", "blockquote", "table",
    "horizontalRule", "codeBlock",
}
_INLINE = {"text", "hardBreak"}
_MARKS = {"bold", "italic", "underline", "strike", "superscript", "subscript", "link", "code"}
_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


class RevisionConflict(ValueError):
    """Another tab or request has already saved a newer document revision."""

    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__(
            f"This document changed elsewhere (current revision {current_revision}). "
            "Reload the document before saving; copy any unsaved text first."
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _title(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("The document title must be text.")
    value = value.strip() or "Untitled thesis"
    if len(value) > 240 or _INVALID_XML.search(value):
        raise ValueError("Use a document title of at most 240 printable characters.")
    return value


def _safe_link(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme.lower() == "mailto" and parsed.path:
        return value
    if value.startswith("#") and len(value) > 1:
        return value
    return None


def validate_content(value: Any) -> dict[str, Any]:
    """Validate the supported editor schema, without accepting active HTML."""
    nodes = 0
    characters = 0

    def visit(node: Any, depth: int, parent: str | None) -> dict[str, Any]:
        nonlocal nodes, characters
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("The document is too complex to save safely.")
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise ValueError("Invalid editor document: every node needs a type.")
        kind = node["type"]
        permitted = {
            None: {"doc"},
            "doc": _BLOCKS,
            "paragraph": _INLINE,
            "heading": _INLINE,
            "codeBlock": {"text"},
            "bulletList": {"listItem"},
            "orderedList": {"listItem"},
            "listItem": _BLOCKS,
            "blockquote": _BLOCKS,
            "table": {"tableRow"},
            "tableRow": {"tableCell", "tableHeader"},
            "tableCell": _BLOCKS,
            "tableHeader": _BLOCKS,
        }.get(parent, set())
        if kind not in permitted:
            raise ValueError(f"Unsupported editor element: {kind}.")
        result: dict[str, Any] = {"type": kind}
        attrs = node.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise ValueError("Invalid editor element attributes.")
        kept: dict[str, Any] = {}
        if kind == "heading":
            level = attrs.get("level", 1)
            if type(level) is not int or not 1 <= level <= 6:
                raise ValueError("Heading level must be between 1 and 6.")
            kept["level"] = level
        if kind in {"paragraph", "heading"} and attrs.get("textAlign") in _ALIGN:
            kept["textAlign"] = attrs["textAlign"]
        if kind == "orderedList":
            start = attrs.get("start", 1)
            if type(start) is not int or not 1 <= start <= 1_000_000:
                raise ValueError("Invalid list start value.")
            kept["start"] = start
        if kind in {"tableCell", "tableHeader"}:
            for name in ("colspan", "rowspan"):
                span = attrs.get(name, 1)
                if type(span) is not int or not 1 <= span <= 64:
                    raise ValueError("Table spans must be between 1 and 64.")
                kept[name] = span
        if kept:
            result["attrs"] = kept
        if kind == "text":
            text = node.get("text")
            if not isinstance(text, str) or _INVALID_XML.search(text):
                raise ValueError("The document contains invalid text characters.")
            characters += len(text)
            if characters > MAX_CHARACTERS:
                raise ValueError("The document exceeds the 2 million character limit.")
            result["text"] = text
            marks = node.get("marks") or []
            if not isinstance(marks, list) or len(marks) > 12:
                raise ValueError("Invalid text formatting.")
            normalized = []
            for mark in marks:
                if not isinstance(mark, dict) or mark.get("type") not in _MARKS:
                    raise ValueError("Unsupported text formatting.")
                entry = {"type": mark["type"]}
                if mark["type"] == "link":
                    href = _safe_link((mark.get("attrs") or {}).get("href"))
                    if not href:
                        raise ValueError("Links must use http, https, mailto, or a document anchor.")
                    entry["attrs"] = {"href": href}
                if entry not in normalized:
                    normalized.append(entry)
            if normalized:
                result["marks"] = normalized
        else:
            children = node.get("content", [])
            if not isinstance(children, list):
                raise ValueError("Invalid editor element content.")
            if kind in {"hardBreak", "horizontalRule"} and children:
                raise ValueError("A break cannot contain document content.")
            parsed = [visit(child, depth + 1, kind) for child in children]
            if kind in {"doc", "listItem", "blockquote", "tableCell", "tableHeader"} and not parsed:
                parsed = [{"type": "paragraph"}]
            if kind in {"bulletList", "orderedList", "table"} and not parsed:
                raise ValueError("An empty list or table cannot be saved.")
            if parsed:
                result["content"] = parsed
        return result

    return visit(value, 0, None)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".save-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply_marks(run: Run, marks: list[dict[str, Any]]) -> None:
    types = {mark["type"] for mark in marks}
    run.bold = "bold" in types
    run.italic = "italic" in types
    run.underline = "underline" in types
    run.font.strike = "strike" in types
    run.font.superscript = "superscript" in types
    run.font.subscript = "subscript" in types
    if "code" in types:
        run.font.name = "Consolas"
        run.font.size = Pt(10)


def _add_inline(paragraph: Paragraph, node: dict[str, Any]) -> None:
    for child in node.get("content", []):
        if child["type"] == "hardBreak":
            paragraph.add_run().add_break()
            continue
        run = paragraph.add_run(child.get("text", ""))
        marks = child.get("marks", [])
        _apply_marks(run, marks)
        link = next((mark for mark in marks if mark["type"] == "link"), None)
        if link:
            href = link["attrs"]["href"]
            hyperlink = OxmlElement("w:hyperlink")
            if href.startswith("#"):
                hyperlink.set(qn("w:anchor"), href[1:])
            else:
                relation = paragraph.part.relate_to(
                    href,
                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                    is_external=True,
                )
                hyperlink.set(qn("r:id"), relation)
            hyperlink.append(run._r)
            paragraph._p.append(hyperlink)


def _new_numbering(document: Any, ordered: bool, start: int) -> int:
    """Create a real, independent Word list; never fake its numbers as text."""
    numbering = document.part.numbering_part.element
    existing_abstract = [int(e.get(qn("w:abstractNumId"))) for e in numbering.findall(qn("w:abstractNum"))]
    abstract_id = max(existing_abstract, default=-1) + 1
    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    multi = OxmlElement("w:multiLevelType")
    multi.set(qn("w:val"), "multilevel")
    abstract.append(multi)
    for level in range(9):
        lvl = OxmlElement("w:lvl")
        lvl.set(qn("w:ilvl"), str(level))
        for name, val in (("start", str(start)), ("numFmt", "decimal" if ordered else "bullet"),
                          ("lvlText", f"%{level + 1}." if ordered else "•"), ("lvlJc", "left")):
            element = OxmlElement(f"w:{name}")
            element.set(qn("w:val"), val)
            lvl.append(element)
        properties = OxmlElement("w:pPr")
        indent = OxmlElement("w:ind")
        indent.set(qn("w:left"), str(720 + level * 360))
        indent.set(qn("w:hanging"), "360")
        properties.append(indent)
        lvl.append(properties)
        abstract.append(lvl)
    numbering.append(abstract)
    number = numbering.add_num(abstract_id)
    return number.numId


def _set_numbering(paragraph: Paragraph, number_id: int, level: int) -> None:
    properties = paragraph._p.get_or_add_pPr()
    num = properties.get_or_add_numPr()
    num.get_or_add_ilvl().val = min(level, 8)
    num.get_or_add_numId().val = number_id


def _table_layout(node: dict[str, Any]) -> tuple[int, list[tuple[int, int, dict[str, Any]]]]:
    rows = node.get("content", [])
    occupied: set[tuple[int, int]] = set()
    placements = []
    columns = 0
    for row_number, row in enumerate(rows):
        column = 0
        for cell in row.get("content", []):
            while (row_number, column) in occupied:
                column += 1
            attrs = cell.get("attrs", {})
            colspan, rowspan = attrs.get("colspan", 1), attrs.get("rowspan", 1)
            if row_number + rowspan > len(rows) or column + colspan > 64:
                raise ValueError("The table has an invalid merged-cell layout.")
            for row_offset in range(rowspan):
                for col_offset in range(colspan):
                    position = (row_number + row_offset, column + col_offset)
                    if position in occupied:
                        raise ValueError("The table has overlapping merged cells.")
                    occupied.add(position)
            placements.append((row_number, column, cell))
            column += colspan
            columns = max(columns, column)
    if not columns or len(rows) * columns > 10000:
        raise ValueError("The table is empty or too large.")
    return columns, placements


def _write_blocks(container: Any, nodes: list[dict[str, Any]], document: Any,
                  list_depth: int = 0, quote: bool = False) -> None:
    for node in nodes:
        kind = node["type"]
        if kind in {"paragraph", "heading", "codeBlock"}:
            style = f"Heading {node.get('attrs', {}).get('level', 1)}" if kind == "heading" else "Normal"
            paragraph = container.add_paragraph(style=style)
            _add_inline(paragraph, node)
            alignment = node.get("attrs", {}).get("textAlign")
            if alignment in _ALIGN:
                paragraph.alignment = _ALIGN[alignment]
            if quote:
                paragraph.style = document.styles["Quote"]
                paragraph.paragraph_format.left_indent = Cm(1)
            if kind == "codeBlock":
                for run in paragraph.runs:
                    run.font.name = "Consolas"
                    run.font.size = Pt(10)
        elif kind in {"orderedList", "bulletList"}:
            number_id = _new_numbering(document, kind == "orderedList", node.get("attrs", {}).get("start", 1))
            for item in node.get("content", []):
                children = item.get("content", [])
                first = children[0] if children else {"type": "paragraph"}
                if first["type"] in {"paragraph", "heading"}:
                    paragraph = container.add_paragraph()
                    _add_inline(paragraph, first)
                    _set_numbering(paragraph, number_id, list_depth)
                    _write_blocks(container, children[1:], document, list_depth + 1, quote)
                else:
                    paragraph = container.add_paragraph()
                    _set_numbering(paragraph, number_id, list_depth)
                    _write_blocks(container, children, document, list_depth + 1, quote)
        elif kind == "blockquote":
            _write_blocks(container, node.get("content", []), document, list_depth, True)
        elif kind == "horizontalRule":
            paragraph = container.add_paragraph()
            border = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            for key, val in (("val", "single"), ("sz", "6"), ("color", "999999"), ("space", "1")):
                bottom.set(qn(f"w:{key}"), val)
            border.append(bottom)
            paragraph._p.get_or_add_pPr().append(border)
        elif kind == "table":
            rows = node.get("content", [])
            columns, placements = _table_layout(node)
            table = container.add_table(rows=len(rows), cols=columns)
            table.style = "Table Grid"
            for r, c, cell_node in placements:
                attrs = cell_node.get("attrs", {})
                colspan, rowspan = attrs.get("colspan", 1), attrs.get("rowspan", 1)
                cell = table.cell(r, c)
                if colspan > 1 or rowspan > 1:
                    cell = cell.merge(table.cell(r + rowspan - 1, c + colspan - 1))
                cell._tc.clear_content()
                _write_blocks(cell, cell_node.get("content", []), document)
                if not cell.paragraphs or cell._tc[-1].tag != qn("w:p"):
                    cell.add_paragraph()
                if cell_node["type"] == "tableHeader":
                    shade = OxmlElement("w:shd")
                    shade.set(qn("w:fill"), "E8EEEE")
                    cell._tc.get_or_add_tcPr().append(shade)
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
            if all(cell["type"] == "tableHeader" for cell in rows[0].get("content", [])):
                repeat = OxmlElement("w:tblHeader")
                table.rows[0]._tr.get_or_add_trPr().append(repeat)


def write_docx(path: Path, title: str, content: dict[str, Any]) -> None:
    """Write to a temporary file, then replace, so a failure leaves the old file."""
    document = Document()
    document.core_properties.title = title
    document.core_properties.author = ""
    document.core_properties.subject = "ThesisHelper working document"
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.5)
    section.left_margin = section.right_margin = Cm(2.5)
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(6)
    for level in range(1, 7):
        style = document.styles[f"Heading {level}"]
        style.font.name = "Times New Roman"
        style.font.size = Pt(max(12, 18 - level))
        style.font.bold = True
    _write_blocks(document, content.get("content", []), document)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".word-", suffix=".docx", dir=path.parent)
    os.close(descriptor)
    try:
        document.save(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_nodes(run: Run, href: str | None = None) -> list[dict[str, Any]]:
    marks = []
    for kind, value in (("bold", run.bold), ("italic", run.italic), ("underline", run.underline),
                        ("strike", run.font.strike), ("superscript", run.font.superscript),
                        ("subscript", run.font.subscript)):
        if value:
            marks.append({"type": kind})
    if href and _safe_link(href):
        marks.append({"type": "link", "attrs": {"href": href}})
    parts = run.text.split("\n")
    nodes = []
    for index, part in enumerate(parts):
        if index:
            nodes.append({"type": "hardBreak"})
        if part:
            node: dict[str, Any] = {"type": "text", "text": _INVALID_XML.sub("", part)}
            if marks:
                node["marks"] = copy.deepcopy(marks)
            nodes.append(node)
    return nodes


def _paragraph_node(paragraph: Paragraph) -> dict[str, Any]:
    style = paragraph.style.name if paragraph.style else ""
    match = re.match(r"(?:Heading|Überschrift)\s*([1-6])$", style, re.IGNORECASE)
    node: dict[str, Any] = {"type": "heading" if match else "paragraph"}
    attrs: dict[str, Any] = {"level": int(match.group(1))} if match else {}
    alignment = next((key for key, val in _ALIGN.items() if val == paragraph.alignment), None)
    if alignment:
        attrs["textAlign"] = alignment
    if attrs:
        node["attrs"] = attrs
    children = []
    for item in paragraph.iter_inner_content():
        if isinstance(item, Hyperlink):
            href = item.url or (f"#{item.fragment}" if item.fragment else None)
            for run in item.runs:
                children.extend(_run_nodes(run, href))
        else:
            children.extend(_run_nodes(item))
    if children:
        node["content"] = children
    return node


def _list_info(paragraph: Paragraph) -> tuple[str, str, int, int] | None:
    """Return list kind, identity, nesting level, and first value."""
    properties = paragraph._p.pPr
    num = properties.numPr if properties is not None else None
    style = paragraph.style
    if num is None and style is not None and style.element.pPr is not None:
        num = style.element.pPr.numPr
    if num is not None and num.numId is not None:
        number_id = num.numId.val
        if number_id == 0:
            return None
        level = num.ilvl.val if num.ilvl is not None else 0
        try:
            numbering = paragraph.part.numbering_part.element
            number = numbering.num_having_numId(number_id)
            abstract_id = number.abstractNumId.val
            abstract = next(a for a in numbering.findall(qn("w:abstractNum"))
                            if int(a.get(qn("w:abstractNumId"))) == abstract_id)
            levels = abstract.findall(qn("w:lvl"))
            definition = next((v for v in levels if int(v.get(qn("w:ilvl"))) == level), levels[0])
            fmt = definition.find(qn("w:numFmt"))
            start_el = definition.find(qn("w:start"))
            start = int(start_el.get(qn("w:val"))) if start_el is not None else 1
            for override in number.findall(qn("w:lvlOverride")):
                override_start = override.find(qn("w:startOverride"))
                if int(override.get(qn("w:ilvl"), "0")) == level and override_start is not None:
                    start = int(override_start.get(qn("w:val")))
            kind = "bulletList" if fmt is not None and fmt.get(qn("w:val")) == "bullet" else "orderedList"
            return kind, str(number_id), min(level, 8), max(1, start)
        except (AttributeError, KeyError, ValueError, StopIteration, IndexError):
            return "orderedList", str(number_id), min(level, 8), 1
    name = style.name.lower() if style else ""
    if name.startswith("list bullet") or name.startswith("list number"):
        suffix = re.search(r"(\d+)$", name)
        level = max(0, int(suffix.group(1)) - 1) if suffix else 0
        return ("bulletList" if "bullet" in name else "orderedList"), name, level, 1
    return None


def _read_blocks(container: Any, warnings: list[str]) -> list[dict[str, Any]]:
    result = []
    # Each stack entry belongs to one active Word list level.
    list_stack: list[tuple[int, str, str, dict[str, Any]]] = []
    for item in container.iter_inner_content():
        if isinstance(item, Table):
            list_stack.clear()
            result.append(_read_table(item, warnings))
            continue
        paragraph = _paragraph_node(item)
        info = _list_info(item)
        if not info:
            list_stack.clear()
            if item.style is not None and item.style.name.lower() in {"quote", "intense quote"}:
                result.append({"type": "blockquote", "content": [paragraph]})
            elif item._p.pPr is not None and item._p.pPr.find(qn("w:pBdr")) is not None and not item.text:
                result.append({"type": "horizontalRule"})
            else:
                result.append(paragraph)
            continue
        kind, identity, level, start = info
        while list_stack and (list_stack[-1][0] > level or (
                list_stack[-1][0] == level and list_stack[-1][1:3] != (identity, kind))):
            list_stack.pop()
        if not list_stack or list_stack[-1][0] != level:
            listing: dict[str, Any] = {"type": kind, "content": []}
            if kind == "orderedList":
                listing["attrs"] = {"start": start}
            if list_stack and list_stack[-1][3]["content"]:
                list_stack[-1][3]["content"][-1]["content"].append(listing)
            else:
                result.append(listing)
            list_stack.append((level, identity, kind, listing))
        list_stack[-1][3]["content"].append({"type": "listItem", "content": [paragraph]})
    return result or [{"type": "paragraph"}]


def _read_table(table: Table, warnings: list[str]) -> dict[str, Any]:
    rows = []
    cells_seen: dict[Any, tuple[int, dict[str, Any]]] = {}
    for row_number, row in enumerate(table.rows):
        children = []
        repeated_this_row: set[Any] = set()
        header = row._tr.trPr is not None and row._tr.trPr.find(qn("w:tblHeader")) is not None
        for cell in row.cells:
            element = cell._tc
            if element in repeated_this_row:
                if cells_seen[element][0] == row_number:
                    cells_seen[element][1]["attrs"]["colspan"] += 1
                continue
            repeated_this_row.add(element)
            if element in cells_seen:
                cells_seen[element][1]["attrs"]["rowspan"] += 1
                continue
            node = {"type": "tableHeader" if header else "tableCell",
                    "attrs": {"colspan": 1, "rowspan": 1},
                    "content": _read_blocks(cell, warnings)}
            cells_seen[element] = (row_number, node)
            children.append(node)
        rows.append({"type": "tableRow", "content": children})
    return {"type": "table", "content": rows}


def read_docx(path: Path) -> tuple[dict[str, Any], str, list[str]]:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("The Word document exceeds the 100 MB import limit.")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 20_000 or sum(info.file_size for info in infos) > 512 * 1024 * 1024:
                raise ValueError("The Word document expands beyond the safe import size.")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("This file is not a valid .docx document.")
            main_xml = archive.read("word/document.xml")
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        raise ValueError("This file is not a valid .docx document.") from exc
    document = Document(path)
    warnings = [
        "Imported a working copy. The original .docx is preserved separately. "
        "Word page layout, headers, footers, fields, references, and advanced formatting do not round-trip."
    ]
    indicators = {
        "Images and drawings are not included in the editor copy.": [b"<w:drawing", b"<w:pict"],
        "Tracked changes require review in the original Word file; inserted/deleted text may be omitted.": [b"<w:ins", b"<w:del"],
        "Text boxes are not included in the editor copy.": [b"<w:txbxContent"],
        "Word citation, bibliography, and other field codes become plain text where available.": [b"<w:fldChar", b"<w:fldSimple"],
        "Content controls may contain text that is not included in the editor copy.": [b"<w:sdt"],
    }
    for warning, markers in indicators.items():
        if any(marker in main_xml for marker in markers):
            warnings.append(warning)
    if any(name in names for name in ("word/footnotes.xml", "word/endnotes.xml")):
        warnings.append("Footnotes and endnotes are not included in the editor copy; review the preserved original.")
    if "word/comments.xml" in names:
        warnings.append("Word comments are not included in the editor copy.")
    content = validate_content({"type": "doc", "content": _read_blocks(document, warnings)})
    return content, document.core_properties.title or path.stem, warnings


class DocumentStore:
    def __init__(self, root_dir: Path):
        self.root = Path(root_dir).resolve()
        self.base = self.root / "workspace" / "documents"
        self.base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _directory(self, document_id: str) -> Path:
        if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
            raise ValueError("Invalid document ID.")
        path = self.base / document_id
        if not path.resolve().is_relative_to(self.base.resolve()):
            raise ValueError("Invalid document path.")
        return path

    def list_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            documents = []
            for path in self.base.glob("*/document.json"):
                if not _ID.fullmatch(path.parent.name):
                    continue
                try:
                    value = self.get(path.parent.name)
                    if value:
                        documents.append({key: value[key] for key in ("id", "title", "revision", "updated_at")})
                except (ValueError, OSError, KeyError):
                    continue
            return sorted(documents, key=lambda doc: doc["updated_at"], reverse=True)

    def create(self, title: str) -> dict[str, Any]:
        with self._lock:
            document_id = uuid.uuid4().hex
            value = {"id": document_id, "title": _title(title), "revision": 1,
                     "updated_at": _now(), "content": {"type": "doc", "content": [{"type": "paragraph"}]},
                     "warnings": []}
            self._persist(value)
            return copy.deepcopy(value)

    def get(self, document_id: str) -> dict[str, Any] | None:
        with self._lock:
            path = self._directory(document_id) / "document.json"
            if not path.exists():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("This document cannot be read. Its revision history may contain a recoverable copy.") from exc
            return value

    def _persist(self, value: dict[str, Any]) -> None:
        directory = self._directory(value["id"])
        _atomic_json(directory / "history" / f"{value['revision']:08d}.json", value)
        _atomic_json(directory / "document.json", value)
        self._mirror(value)

    def _mirror(self, value: dict[str, Any]) -> None:
        prefix = "Word mirror could not be updated:"
        warnings = [w for w in value.get("warnings", []) if not w.startswith(prefix)]
        try:
            write_docx(self._directory(value["id"]) / "current.docx", value["title"], value["content"])
        except (OSError, ValueError) as exc:
            warnings.append(
                f"{prefix} your text is saved. Close current.docx in Word and export again. "
                f"({type(exc).__name__})"
            )
        value["warnings"] = warnings
        _atomic_json(self._directory(value["id"]) / "document.json", value)

    def save(self, document_id: str, title: str, content: dict[str, Any], revision: int) -> dict[str, Any]:
        title = _title(title)
        normalized = validate_content(content)
        # Validate merged tables before committing JSON so export errors are not hidden.
        def check_tables(node: dict[str, Any]) -> None:
            if node["type"] == "table":
                _table_layout(node)
            for child in node.get("content", []):
                check_tables(child)
        check_tables(normalized)
        with self._lock:
            current = self.get(document_id)
            if current is None:
                raise FileNotFoundError("Document not found.")
            if type(revision) is not int or revision != current["revision"]:
                raise RevisionConflict(current["revision"])
            if title == current["title"] and normalized == current["content"]:
                self._mirror(current)
                return current
            value = {**current, "title": title, "content": normalized,
                     "revision": current["revision"] + 1, "updated_at": _now()}
            self._persist(value)
            return copy.deepcopy(value)

    def import_docx(self, path: Path, title: str | None = None) -> dict[str, Any]:
        path = Path(path)
        content, suggested_title, warnings = read_docx(path)
        with self._lock:
            value = {"id": uuid.uuid4().hex, "title": _title(title or suggested_title),
                     "content": content, "revision": 1, "updated_at": _now(), "warnings": warnings}
            directory = self._directory(value["id"])
            directory.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(path, directory / "original.docx")
            self._persist(value)
            return copy.deepcopy(value)

    def export_path(self, document_id: str) -> Path:
        with self._lock:
            value = self.get(document_id)
            if value is None:
                raise FileNotFoundError("Document not found.")
            # A separate export prevents a user-opened mirror from blocking download.
            path = self._directory(document_id) / "exports" / f"thesis-revision-{value['revision']}.docx"
            write_docx(path, value["title"], value["content"])
            return path

    def history(self, document_id: str) -> list[dict[str, Any]]:
        with self._lock:
            if self.get(document_id) is None:
                raise FileNotFoundError("Document not found.")
            versions = []
            for path in (self._directory(document_id) / "history").glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    versions.append({key: value[key] for key in ("revision", "title", "updated_at")})
                except (ValueError, OSError, KeyError):
                    continue
            return sorted(versions, key=lambda entry: entry["revision"], reverse=True)

    def restore(self, document_id: str, revision: int, current_revision: int) -> dict[str, Any]:
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid revision number.")
        with self._lock:
            current = self.get(document_id)
            if current is None:
                raise FileNotFoundError("Document not found.")
            if type(current_revision) is not int or current_revision != current["revision"]:
                raise RevisionConflict(current["revision"])
            path = self._directory(document_id) / "history" / f"{revision:08d}.json"
            if not path.exists():
                raise FileNotFoundError("Revision not found.")
            previous = json.loads(path.read_text(encoding="utf-8"))
            return self.save(document_id, previous["title"], previous["content"], current_revision)
