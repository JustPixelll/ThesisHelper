from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from thesishelper.extraction import Extraction, chunk_units, split_text
from thesishelper.library import Library


def write_pdf(path: Path, pages: list[str], labels: bool = False) -> None:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 50 750 Td ({safe}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if labels:
        writer.set_page_label(0, len(pages) - 1, style="/r", start=1)
    with path.open("wb") as handle:
        writer.write(handle)


def write_docx(path: Path, paragraphs: list[str], table: bool = False) -> None:
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        cells = document.add_table(rows=1, cols=2).rows[0].cells
        cells[0].text = "Mechanism"
        cells[1].text = "Employee inclusion"
    document.save(path)


class FakeEmbedder:
    model = "test-embedding"
    dimensions = 3

    def __init__(self, fail_call: int | None = None):
        self.calls: list[list[str]] = []
        self.fail_call = fail_call

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if len(self.calls) == self.fail_call:
            raise RuntimeError("Simulated provider interruption")
        result = []
        for text in texts:
            low = text.lower()
            result.append([1.0 if "strategy" in low else 0.1, 1.0 if "recipe" in low else 0.1, 0.2])
        return result


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ingest = self.root / "ingest"
        self.library = Library(self.root / "data" / "library.db", self.ingest)

    def tearDown(self):
        self.temp.cleanup()

    def test_pdf_pages_and_labels_do_not_become_printed_pages(self):
        write_pdf(self.ingest / "book.pdf", ["Open strategy includes employee participation in strategic decisions.", "Corporate governance defines independent institutional accountability."], labels=True)
        self.library.scan()
        evidence = self.library.search("governance")[0]
        self.assertEqual(evidence["page"], 2)
        self.assertEqual(evidence["page_label"], "ii")
        self.assertIsNone(evidence["printed_page"])
        self.assertNotIn("strategy", evidence["text"].lower())
        self.library.update_metadata(evidence["document_id"], {"page_offset": 10, "authors": "Muster, A.", "year": "2020", "verified": True})
        updated = self.library.read_chunk(evidence["chunk_id"], neighbors=0)[0]
        self.assertEqual(updated["printed_page"], 12)
        self.assertTrue(updated["metadata_verified"])
        self.assertIn("user-mapped", updated["location"])
        self.library.update_metadata(evidence["document_id"], {"year": "2021"})
        self.assertFalse(self.library.get_document(evidence["document_id"])["verified"])
        self.library.update_metadata(evidence["document_id"], {"page_offset": -12, "verified": True})
        front_matter = self.library.read_chunk(evidence["chunk_id"], neighbors=0)[0]
        self.assertIsNone(front_matter["printed_page"])
        self.assertNotIn("printed page -", front_matter["location"])
        neighbors = self.library.read_chunk(evidence["chunk_id"], neighbors=1)
        self.assertEqual([item["page"] for item in neighbors], [1, 2])

    def test_docx_locations_and_tables_have_no_fake_pages(self):
        write_docx(self.ingest / "book.docx", ["Open strategy values inclusion.", "Employees contribute contextual knowledge."], table=True)
        self.library.scan()
        result = self.library.search("contextual")[0]
        self.assertIsNone(result["page"])
        self.assertEqual((result["paragraph_start"], result["paragraph_end"]), (1, 2))
        self.assertIn("paragraphs 1–2", result["location"])
        table_result = self.library.search("Mechanism")[0]
        self.assertIn("DOCX table 1, row 1", table_result["location"])
        self.assertIn("Employee inclusion", table_result["text"])

    def test_incremental_updates_remove_old_chunks_and_missing_files(self):
        path = self.ingest / "book.docx"
        write_docx(path, ["Uniqueoldkeyword belongs to the first document version."])
        first_scan = self.library.scan()
        old = self.library.search("Uniqueoldkeyword")[0]
        self.assertEqual(first_scan["indexed"], 1)
        self.assertEqual(self.library.scan()["unchanged"], 1)
        self.library.update_metadata(old["document_id"], {"authors": "Verified Author", "verified": True})
        write_docx(path, ["Uniquenewkeyword belongs to the replacement document content."])
        # Before a reindex, stale content must not be presented as current evidence.
        self.assertEqual(self.library.search("Uniqueoldkeyword"), [])
        self.assertEqual(self.library.get_document(old["document_id"])["status"], "stale")
        self.library.scan()
        self.assertEqual(self.library.search("Uniqueoldkeyword"), [])
        new = self.library.search("Uniquenewkeyword")[0]
        self.assertEqual(old["document_id"], new["document_id"])
        self.assertNotEqual(old["chunk_id"], new["chunk_id"])
        self.assertNotEqual(old["source_hash"], new["source_hash"])
        self.assertFalse(new["metadata_verified"])
        self.assertEqual(self.library.read_chunk(old["chunk_id"]), [])
        path.unlink()
        self.assertEqual(self.library.search("Uniquenewkeyword"), [])
        self.assertEqual(self.library.stats()["indexed_documents"], 0)
        self.assertEqual(self.library.get_document(old["document_id"])["status"], "missing")

    def test_saved_ocr_survives_normal_and_semantic_scans_until_forced(self):
        path = self.ingest / "scan.pdf"
        write_pdf(path, [""])
        extracted = Extraction(units=[{
            "text": "Recovered OCR text about employee participation in open strategy.",
            "kind": "pdf", "page": 1, "page_label": None,
            "paragraph_start": None, "paragraph_end": None,
        }], pages=1)
        with patch("thesishelper.library.extract_document", return_value=extracted) as extractor:
            self.library.scan(ocr=True, embedder=FakeEmbedder())
            original = self.library.search("participation")[0]
            first_vectors = self.library.stats()["embedded_chunks"]
            self.assertEqual(extractor.call_count, 1)
            plain = self.library.scan()
            semantic = self.library.scan(embedder=FakeEmbedder())
            self.assertEqual(plain["unchanged"], 1)
            self.assertEqual(semantic["embedded"], 0)
            self.assertEqual(extractor.call_count, 1)
            self.assertEqual(self.library.stats()["embedded_chunks"], first_vectors)
            self.assertEqual(self.library.search("participation")[0]["chunk_id"], original["chunk_id"])
        # An explicit force rescan honors the OCR-off setting and re-extracts the blank layer.
        self.library.scan(force=True, ocr=False)
        self.assertEqual(self.library.search("participation"), [])
        self.assertEqual(self.library.stats()["embedded_chunks"], 0)
        unavailable = Extraction(pages=1, incomplete=True, warnings=["OCR is unavailable: local Tesseract was not found."])
        with patch("thesishelper.library.extract_document", return_value=unavailable):
            self.library.scan(ocr=True)
        with patch("thesishelper.library.extract_document", return_value=extracted) as extractor:
            self.library.scan(ocr=True)
            self.assertEqual(extractor.call_count, 1)
            self.assertTrue(self.library.search("participation"))
        write_pdf(path, ["Replacement source text discusses corporate governance and accountability."])
        self.library.scan(ocr=False)
        self.assertEqual(self.library.search("participation"), [])
        replacement = self.library.search("accountability")[0]
        self.assertNotEqual(replacement["source_hash"], original["source_hash"])

    def test_embeddings_resume_by_batch_and_semantic_query_is_cached(self):
        # Every long paragraph creates one chunk, giving three provider batches.
        write_docx(self.ingest / "long.docx", [f"Open strategy section {i}. " + "evidence " * 270 for i in range(70)])
        failing = FakeEmbedder(fail_call=2)
        scan = self.library.scan(embedder=failing)
        self.assertEqual(len(scan["errors"]), 1)
        self.assertEqual(self.library.stats()["embedded_chunks"], 32)
        resumed = FakeEmbedder()
        self.library.scan(embedder=resumed)
        self.assertEqual(sum(map(len, resumed.calls)), self.library.stats()["chunks"] - 32)
        query = FakeEmbedder()
        first = self.library.search("open strategy", embedder=query)
        second = self.library.search("open strategy", embedder=query)
        self.assertTrue(first)
        self.assertEqual(len(query.calls), 1)
        self.assertEqual([item["chunk_id"] for item in first], [item["chunk_id"] for item in second])
        self.assertIsNotNone(first[0]["semantic_score"])

    def test_permanent_embedding_failure_stops_the_corpus_and_keeps_progress(self):
        write_docx(self.ingest / "a-long.docx", [f"Open strategy section {i}. " + "evidence " * 270 for i in range(70)])
        write_docx(self.ingest / "z-unvisited.docx", ["This later document should not make another doomed API call."])
        class FatalError(RuntimeError):
            abort_indexing = True
        class FatalProvider(FakeEmbedder):
            def embed(self, texts):
                if self.calls:
                    self.calls.append(texts)
                    raise FatalError("Synthetic account limit; update the API account before retrying")
                return super().embed(texts)
        provider = FatalProvider()
        with self.assertRaises(FatalError):
            self.library.scan(embedder=provider)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(self.library.stats()["embedded_chunks"], 32)
        documents = self.library.list_documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["status"], "partial")
        self.assertTrue(self.library.search("strategy"))
        self.library.scan(embedder=FakeEmbedder())
        self.assertEqual(self.library.stats()["documents"], 2)
        self.assertEqual(self.library.stats()["chunks"], self.library.stats()["embedded_chunks"])

    def test_semantic_failure_falls_back_to_lexical(self):
        write_docx(self.ingest / "book.docx", ["Open strategy gives employees participation in decisions."])
        self.library.scan(embedder=FakeEmbedder())
        results = self.library.search("strategy", embedder=FakeEmbedder(fail_call=1))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["retrieval_warnings"])
        self.assertIsNone(results[0]["semantic_score"])
        self.assertTrue(self.library.last_search_warnings)

    def test_search_syntax_and_document_filters_are_safe(self):
        write_docx(self.ingest / "one.docx", ["Employee participation supports open strategy."])
        write_docx(self.ingest / "two.docx", ["Recipe design includes thermal processing."])
        self.library.scan()
        for query in ['" OR *', "strategy'); DROP TABLE documents; --", "NEAR(strategy) OR title:recipe", "\x00", "(((", "[^"]:
            self.library.search(query)
        self.assertEqual(self.library.stats()["documents"], 2)
        result = self.library.search("strategy")[0]
        self.assertEqual(self.library.search("strategy", document_ids=[]), [])
        self.assertEqual(self.library.search("strategy", document_ids=["'); DROP TABLE documents; --"]), [])
        self.assertEqual(len(self.library.search("strategy", document_ids=[result["document_id"]])), 1)

    def test_error_isolated_metadata_not_inferred_from_creation_date(self):
        (self.ingest / "broken.pdf").write_bytes(b"not a PDF")
        write_docx(self.ingest / "valid.docx", ["Open strategy remains searchable despite an invalid neighboring file."])
        summary = self.library.scan()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["indexed"], 1)
        document = next(document for document in self.library.list_documents() if document["filename"] == "valid.docx")
        self.assertEqual(document["year"], "")
        self.assertFalse(document["verified"])
        self.assertTrue(self.library.search("strategy"))
        with self.assertRaises(ValueError):
            self.library.update_metadata(document["id"], {"filename": "../../outside.docx"})
        with self.assertRaises(ValueError):
            self.library.update_metadata(document["id"], {"page_offset": "-12"})

    def test_cancel_and_exclusion_preserve_original_files(self):
        write_docx(self.ingest / "book.docx", ["Open strategy and organizational inclusion."])
        cancellation = threading.Event()
        cancellation.set()
        self.assertTrue(self.library.scan(cancel=cancellation)["cancelled"])
        self.library.scan()
        document = self.library.list_documents()[0]
        self.library.remove_document(document["id"])
        self.assertTrue((self.ingest / "book.docx").exists())
        self.assertEqual(self.library.search("strategy"), [])
        self.library.scan()
        self.assertEqual(self.library.search("strategy"), [])
        self.library.scan(force=True)
        self.assertTrue(self.library.search("strategy"))

    def test_large_chunks_overlap_and_preserve_page_boundaries(self):
        text = " ".join(f"term{i}" for i in range(3000))
        parts = split_text(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 3000 for part in parts))
        self.assertTrue(set(parts[0].split()) & set(parts[1].split()))
        indexed_words = {word for part in parts for word in part.split()}
        self.assertTrue(set(text.split()).issubset(indexed_words))
        unusual_unicode = split_text("🧬" * 8000)
        self.assertGreater(len(unusual_unicode), 1)
        self.assertTrue(all(len(part.encode("utf-8")) <= 7500 for part in unusual_unicode))
        units = [{"text": text, "kind": "pdf", "page": 1}, {"text": "unique page two", "kind": "pdf", "page": 2}]
        chunks = chunk_units(units)
        self.assertEqual(chunks[-1]["page"], 2)
        self.assertNotIn("unique page two", " ".join(item["text"] for item in chunks if item["page"] == 1))

    @unittest.skipIf(os.name == "nt", "Symlink privileges vary on Windows")
    def test_symlinks_cannot_ingest_outside_folder(self):
        outside = self.root / "outside.docx"
        write_docx(outside, ["Secret external source should not be ingested."])
        (self.ingest / "linked.docx").symlink_to(outside)
        self.assertEqual(self.library.scan()["total"], 0)
        self.assertEqual(self.library.search("Secret"), [])


if __name__ == "__main__":
    unittest.main()
