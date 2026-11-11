from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from docx import Document

import thesishelper.documents as module
from thesishelper.documents import DocumentStore, RevisionConflict, read_docx, validate_content


def text(value, marks=None):
    result = {"type": "text", "text": value}
    if marks:
        result["marks"] = marks
    return result


def paragraph(value):
    return {"type": "paragraph", "content": [text(value)]}


def content(*nodes):
    return {"type": "doc", "content": list(nodes)}


def plain(node):
    return node.get("text", "") + "".join(plain(child) for child in node.get("content", []))


def test_saves_word_mirror_and_non_bmp_text(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Research 🧪")
    saved = store.save(first["id"], first["title"], content(paragraph("Überblick — science 🧪 <a>")), 1)
    directory = tmp_path / "workspace" / "documents" / first["id"]
    assert saved["revision"] == 2
    assert Document(directory / "current.docx").paragraphs[0].text == "Überblick — science 🧪 <a>"
    assert json.loads((directory / "document.json").read_text("utf-8"))["content"] == saved["content"]
    assert store.list_documents()[0]["title"] == "Research 🧪"
    assert store.save(first["id"], saved["title"], saved["content"], 2)["revision"] == 2


def test_revision_conflict_does_not_overwrite_text_or_history(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Draft")
    store.save(first["id"], "Draft", content(paragraph("Newest")), 1)
    with pytest.raises(RevisionConflict) as error:
        store.save(first["id"], "Draft", content(paragraph("Stale tab")), 1)
    assert error.value.current_revision == 2
    assert plain(store.get(first["id"])["content"]) == "Newest"
    assert [item["revision"] for item in store.history(first["id"])] == [2, 1]


def test_restore_is_a_new_revision_and_preserves_both_versions(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Draft")
    second = store.save(first["id"], "Chapter", content(paragraph("A finding")), 1)
    restored = store.restore(first["id"], 1, second["revision"])
    assert restored["revision"] == 3
    assert restored["title"] == "Draft"
    assert plain(restored["content"]) == ""
    assert len(store.history(first["id"])) == 3
    with pytest.raises(RevisionConflict):
        store.restore(first["id"], 2, 2)


def test_two_concurrent_saves_cannot_both_accept_the_same_revision(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Draft")

    def save_word(word):
        try:
            return store.save(first["id"], "Draft", content(paragraph(word)), 1)["revision"]
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save_word, ("first", "second")))
    assert set(results) == {2, "conflict"}


def test_word_lock_cannot_lose_an_editor_save(tmp_path, monkeypatch):
    store = DocumentStore(tmp_path)
    first = store.create("Draft")
    writer = module.write_docx

    def locked(*args, **kwargs):
        raise PermissionError("Word has the file open")

    monkeypatch.setattr(module, "write_docx", locked)
    saved = store.save(first["id"], "Draft", content(paragraph("Keep this text")), 1)
    assert plain(store.get(first["id"])["content"]) == "Keep this text"
    assert saved["revision"] == 2
    assert "text is saved" in saved["warnings"][0]
    monkeypatch.setattr(module, "write_docx", writer)
    exported = store.export_path(first["id"])
    assert exported.name == "thesis-revision-2.docx"
    assert Document(exported).paragraphs[0].text == "Keep this text"


@pytest.mark.parametrize("document_id", ["../secret", "..\\secret", "/etc/passwd", "C:\\secret", "a" * 33, ""])
def test_document_paths_are_opaque_ids(tmp_path, document_id):
    with pytest.raises(ValueError):
        DocumentStore(tmp_path).get(document_id)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,evil", "file:///C:/secret", "https://example.com\nattack"])
def test_unsafe_links_are_rejected_without_creating_a_revision(tmp_path, url):
    store = DocumentStore(tmp_path)
    first = store.create("Draft")
    unsafe = content({"type": "paragraph", "content": [text("link", [{"type": "link", "attrs": {"href": url}}])]})
    with pytest.raises(ValueError):
        store.save(first["id"], "Draft", unsafe, 1)
    assert store.get(first["id"])["revision"] == 1


def test_word_roundtrip_preserves_body_order_marks_lists_and_table(tmp_path):
    source = tmp_path / "source.docx"
    document = Document()
    document.core_properties.title = "Imported thesis"
    document.add_heading("Research question", 1)
    p = document.add_paragraph()
    p.add_run("Important ").bold = True
    p.add_run("finding").italic = True
    document.add_paragraph("First item", style="List Number")
    document.add_paragraph("Second item", style="List Number")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Method"
    table.cell(0, 1).text = "Sample"
    table.cell(1, 0).text = "Interview"
    table.cell(1, 1).text = "12"
    document.add_paragraph("After table")
    document.save(source)
    original = source.read_bytes()
    store = DocumentStore(tmp_path)
    imported = store.import_docx(source)
    blocks = imported["content"]["content"]
    assert [node["type"] for node in blocks] == ["heading", "paragraph", "orderedList", "table", "paragraph"]
    assert blocks[1]["content"][0]["marks"] == [{"type": "bold"}]
    assert blocks[1]["content"][1]["marks"] == [{"type": "italic"}]
    assert len(blocks[2]["content"]) == 2
    assert "Interview" in plain(blocks[3])
    assert imported["title"] == "Imported thesis"
    assert imported["warnings"]
    assert (store._directory(imported["id"]) / "original.docx").read_bytes() == original
    assert source.read_bytes() == original
    exported_content, _, _ = read_docx(store.export_path(imported["id"]))
    assert [node["type"] for node in exported_content["content"]] == [node["type"] for node in blocks]
    assert plain(exported_content) == plain(imported["content"])


def test_export_real_numbered_lists_start_value_and_nested_items(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Lists")
    nested = {"type": "bulletList", "content": [{"type": "listItem", "content": [paragraph("Nested")]}]}
    listing = {"type": "orderedList", "attrs": {"start": 4}, "content": [
        {"type": "listItem", "content": [paragraph("First"), nested]},
        {"type": "listItem", "content": [paragraph("Second")]},
    ]}
    store.save(first["id"], "Lists", content(listing), 1)
    path = store.export_path(first["id"])
    word = Document(path)
    assert word.paragraphs[0]._p.pPr.numPr.numId.val > 0
    imported, _, _ = read_docx(path)
    top_list = imported["content"][0]
    assert top_list["attrs"]["start"] == 4
    assert top_list["content"][0]["content"][1]["type"] == "bulletList"
    assert plain(imported) == "FirstNestedSecond"


def test_links_and_superscripts_survive_word_export_import(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Marks")
    nodes = content({"type": "paragraph", "content": [
        text("Link", [{"type": "link", "attrs": {"href": "https://example.org/paper"}}, {"type": "bold"}]),
        text("2", [{"type": "superscript"}]),
        text("x", [{"type": "subscript"}]),
    ]})
    saved = store.save(first["id"], "Marks", nodes, 1)
    imported, _, _ = read_docx(store.export_path(saved["id"]))
    children = imported["content"][0]["content"]
    assert {"type": "link", "attrs": {"href": "https://example.org/paper"}} in children[0]["marks"]
    assert {"type": "superscript"} in children[1]["marks"]
    assert {"type": "subscript"} in children[2]["marks"]


def test_merged_cells_roundtrip_without_duplicating_text(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Table")
    table = {"type": "table", "content": [
        {"type": "tableRow", "content": [{"type": "tableHeader", "attrs": {"colspan": 2, "rowspan": 1}, "content": [paragraph("Across")]}]},
        {"type": "tableRow", "content": [
            {"type": "tableCell", "attrs": {"colspan": 1, "rowspan": 2}, "content": [paragraph("Down")]},
            {"type": "tableCell", "content": [paragraph("Top right")]},
        ]},
        {"type": "tableRow", "content": [{"type": "tableCell", "content": [paragraph("Bottom right")]}]},
    ]}
    store.save(first["id"], "Table", content(table), 1)
    imported, _, _ = read_docx(store.export_path(first["id"]))
    assert plain(imported) == "AcrossDownTop rightBottom right"
    rows = imported["content"][0]["content"]
    assert rows[0]["content"][0]["attrs"]["colspan"] == 2
    assert rows[1]["content"][0]["attrs"]["rowspan"] == 2


def test_invalid_table_layout_is_rejected_before_saving(tmp_path):
    store = DocumentStore(tmp_path)
    first = store.create("Table")
    invalid = content({"type": "table", "content": [{"type": "tableRow", "content": [
        {"type": "tableCell", "attrs": {"rowspan": 5}, "content": [paragraph("Invalid")]}
    ]}]})
    with pytest.raises(ValueError, match="merged-cell"):
        store.save(first["id"], "Table", invalid, 1)
    assert store.get(first["id"])["revision"] == 1


def test_plain_text_is_not_accepted_as_arbitrary_html():
    with pytest.raises(ValueError, match="Unsupported"):
        validate_content(content({"type": "script", "content": []}))
    with pytest.raises(ValueError, match="invalid text"):
        validate_content(content(paragraph("Text\x00here")))


def test_invalid_word_file_is_readable_error(tmp_path):
    bad = tmp_path / "broken.docx"
    bad.write_text("not a ZIP", encoding="utf-8")
    with pytest.raises(ValueError, match="valid .docx"):
        DocumentStore(tmp_path).import_docx(bad)
