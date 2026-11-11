"""A local, incremental book library with lexical and semantic retrieval.

No full-library prompt uploads and no network retrieval occur here. Embeddings
are supplied by an explicit provider; vectors and extracted text stay in SQLite.
Semantic scans read bounded batches rather than loading an entire library into
RAM. Search results carry their source version and reproducible locations.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

import numpy as np

from .extraction import ExtractionCancelled, chunk_units, extract_document


class EmbeddingProvider(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


_STOPWORDS = set("a an and are as at be by for from how i in is it of on or that the this to was what when where which who why with you your ein eine einer eines einem einen und oder der die das den dem des ist sind im in am an zu zur zum mit von vom für als auf nach wie was welche welcher welches wird werden wurden hat haben auch bei sich nicht aber can could should would does do into about through please find".split())
_ACTIVE = ("indexed", "partial")
_EXTRACTOR_VERSION = "text-v2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _safe_error(error: Exception) -> str:
    # Provider exceptions may embed API headers/keys; never persist their raw text.
    if error.__class__.__module__.startswith(("openai", "httpx", "httpcore")):
        return f"{error.__class__.__name__}: API embedding request failed. Check the key, account limits and connection in Settings."
    message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted API key]", str(error))
    message = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9_.-]+", r"\1[redacted]", message)
    return message[:500] or error.__class__.__name__


class Library:
    def __init__(self, db_path: Path, ingest_dir: Path):
        self.db_path = Path(db_path).resolve()
        self.ingest_dir = Path(ingest_dir).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ingest_dir.mkdir(parents=True, exist_ok=True)
        self._scan_lock = threading.Lock()
        self._query_cache: OrderedDict[tuple[str, int, str], np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._local = threading.local()
        self._initialize()

    @property
    def last_search_warnings(self) -> list[str]:
        return list(getattr(self._local, "search_warnings", []))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL UNIQUE,
                    source_hash TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL DEFAULT '',
                    authors TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    publisher TEXT NOT NULL DEFAULT '',
                    doi TEXT NOT NULL DEFAULT '',
                    edition TEXT NOT NULL DEFAULT '',
                    citation_key TEXT NOT NULL DEFAULT '',
                    page_offset INTEGER,
                    verified INTEGER NOT NULL DEFAULT 0,
                    metadata_edited INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    pages INTEGER NOT NULL DEFAULT 0,
                    warnings TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    extraction_signature TEXT NOT NULL DEFAULT '',
                    extraction_incomplete INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    text TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    UNIQUE(document_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id, ordinal);
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    PRIMARY KEY(chunk_id, model, dimensions)
                );
                CREATE INDEX IF NOT EXISTS embeddings_model ON embeddings(model, dimensions);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED, document_id UNINDEXED, text, title, authors,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(chunk_id, document_id, text, title, authors)
                    SELECT new.id, new.document_id, new.text, title, authors FROM documents WHERE id=new.document_id;
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
                    DELETE FROM chunks_fts WHERE chunk_id=old.id;
                END;
                PRAGMA user_version=2;
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
            if "extraction_incomplete" not in columns:
                connection.execute("ALTER TABLE documents ADD COLUMN extraction_incomplete INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE documents SET extraction_incomplete=1 WHERE status='partial'")
            # A stopped process can leave an in-progress file. It is safe to retry.
            connection.execute("UPDATE documents SET status='pending', error='Previous indexing run stopped before this file completed.' WHERE status='processing'")
            connection.commit()

    def _path(self, filename: str) -> Path | None:
        candidate = (self.ingest_dir / filename).resolve()
        if not candidate.is_relative_to(self.ingest_dir):
            return None
        return candidate

    def _refresh_file_status(self) -> None:
        with self._connect() as connection:
            documents = connection.execute("SELECT id,filename,file_size,mtime_ns,status FROM documents WHERE status IN ('indexed','partial','stale','missing')").fetchall()
            changes = []
            for document in documents:
                path = self._path(document["filename"])
                try:
                    stat = path.stat() if path else None
                    if stat is None or not path.is_file():
                        status = "missing"
                    elif stat.st_size != document["file_size"] or stat.st_mtime_ns != document["mtime_ns"]:
                        status = "stale"
                    else:
                        continue
                except OSError:
                    status = "missing"
                if status != document["status"]:
                    changes.append((status, _now(), document["id"]))
            if changes:
                connection.executemany("UPDATE documents SET status=?,updated_at=? WHERE id=?", changes)
                connection.commit()

    def _document(self, row: sqlite3.Row) -> dict[str, Any]:
        document = dict(row)
        document["verified"] = bool(document["verified"])
        document["metadata_verified"] = document["verified"]
        document["warnings"] = json.loads(document["warnings"])
        document.pop("metadata_edited", None)
        document.pop("extraction_signature", None)
        document.pop("extraction_incomplete", None)
        return document

    def list_documents(self) -> list[dict[str, Any]]:
        self._refresh_file_status()
        with self._connect() as connection:
            rows = connection.execute("""
                SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS chunks,
                (SELECT COUNT(DISTINCT e.chunk_id) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id WHERE c.document_id=d.id) AS embedded_chunks
                FROM documents d ORDER BY lower(d.title), d.filename
            """).fetchall()
        return [self._document(row) for row in rows]

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        self._refresh_file_status()
        with self._connect() as connection:
            row = connection.execute("""
                SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS chunks,
                (SELECT COUNT(DISTINCT e.chunk_id) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id WHERE c.document_id=d.id) AS embedded_chunks
                FROM documents d WHERE d.id=?
            """, (doc_id,)).fetchone()
        return self._document(row) if row else None

    def update_metadata(self, doc_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        allowed = {"title", "authors", "year", "publisher", "doi", "edition", "citation_key", "page_offset", "verified"}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValueError("Unknown metadata fields.")
        values: dict[str, Any] = {}
        for key, value in patch.items():
            if key == "page_offset":
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or abs(value) > 100000):
                    raise ValueError("Page offset must be an integer or null.")
            elif key == "verified":
                if not isinstance(value, bool):
                    raise ValueError("Verified must be true or false.")
                value = int(value)
            else:
                if not isinstance(value, str) or len(value) > 2000:
                    raise ValueError(f"{key} must be text of at most 2,000 characters.")
                value = value.strip()
                if key == "title" and not value:
                    raise ValueError("Title cannot be empty.")
                if key == "citation_key" and value and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
                    raise ValueError("Citation key may use letters, digits, underscores, dots, colons and hyphens (up to 80 characters).")
            values[key] = value
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if existing is None:
                raise KeyError("Document not found.")
            if values:
                if "verified" not in values and any(existing[key] != value for key, value in values.items()):
                    values["verified"] = 0
                values["metadata_edited"] = 1
                values["updated_at"] = _now()
                columns = ",".join(f"{column}=?" for column in values)
                connection.execute(f"UPDATE documents SET {columns} WHERE id=?", (*values.values(), doc_id))
                connection.execute("""UPDATE chunks_fts SET
                    title=(SELECT title FROM documents WHERE id=?),
                    authors=(SELECT authors FROM documents WHERE id=?) WHERE document_id=?""", (doc_id, doc_id, doc_id))
                connection.commit()
        return self.get_document(doc_id)  # type: ignore[return-value]

    def stats(self) -> dict[str, Any]:
        self._refresh_file_status()
        with self._connect() as connection:
            documents = connection.execute("SELECT COUNT(*) FROM documents WHERE status!='excluded'").fetchone()[0]
            indexed = connection.execute("SELECT COUNT(*) FROM documents d WHERE status IN ('indexed','partial') AND EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id)").fetchone()[0]
            chunks = connection.execute("SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.status IN ('indexed','partial')").fetchone()[0]
            embedded = connection.execute("SELECT COUNT(DISTINCT e.chunk_id) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id JOIN documents d ON d.id=c.document_id WHERE d.status IN ('indexed','partial')").fetchone()[0]
            models = connection.execute("SELECT model,dimensions,COUNT(*) AS chunks FROM embeddings GROUP BY model,dimensions").fetchall()
        return {"documents": documents, "indexed_documents": indexed, "chunks": chunks, "embedded_chunks": embedded, "embedding_models": [dict(row) for row in models]}

    def document_path(self, doc_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute("SELECT filename FROM documents WHERE id=?", (doc_id,)).fetchone()
        path = self._path(row["filename"]) if row else None
        return path if path and path.is_file() else None

    def remove_document(self, doc_id: str) -> None:
        """Exclude a document from retrieval without deleting the user's file.

        A force rescan makes excluded files eligible again.
        """
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            connection.execute("UPDATE documents SET status='excluded',updated_at=? WHERE id=?", (_now(), doc_id))
            connection.commit()

    @staticmethod
    def _file_hash(path: Path, cancel: threading.Event | None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                if cancel is not None and cancel.is_set():
                    raise ExtractionCancelled()
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _vector(value: Any, dimensions: int) -> np.ndarray:
        vector = np.asarray(value, dtype=np.float32)
        if vector.shape != (dimensions,) or not np.isfinite(vector).all():
            raise ValueError("Embedding provider returned an invalid vector or an unexpected dimension.")
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("Embedding provider returned a zero or invalid vector.")
        return vector / norm

    def _embed_document(self, doc_id: str, embedder: EmbeddingProvider, cancel: threading.Event | None, progress: Callable[[dict[str, Any]], None] | None, filename: str) -> int:
        model, dimensions = str(embedder.model), int(embedder.dimensions)
        if not model or not 1 <= dimensions <= 16384:
            raise ValueError("Invalid embedding model/dimensions configuration.")
        processed = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise ExtractionCancelled()
            with self._connect() as connection:
                batch = connection.execute("""
                    SELECT c.id,c.text FROM chunks c LEFT JOIN embeddings e
                    ON e.chunk_id=c.id AND e.model=? AND e.dimensions=?
                    WHERE c.document_id=? AND e.chunk_id IS NULL ORDER BY c.ordinal LIMIT 32
                """, (model, dimensions, doc_id)).fetchall()
            if not batch:
                break
            vectors = embedder.embed([row["text"] for row in batch])
            if len(vectors) != len(batch):
                raise ValueError("Embedding provider returned an unexpected number of vectors.")
            records = [(row["id"], model, dimensions, self._vector(vector, dimensions).tobytes()) for row, vector in zip(batch, vectors)]
            with self._connect() as connection:
                connection.executemany("INSERT OR REPLACE INTO embeddings(chunk_id,model,dimensions,vector) VALUES (?,?,?,?)", records)
                connection.commit()
            processed += len(records)
            if progress:
                progress({"phase": "embedding", "filename": filename, "embedded_in_file": processed, "model": model})
        return processed

    def scan(self, embedder: EmbeddingProvider | None = None, progress: Callable[[dict[str, Any]], None] | None = None, cancel: threading.Event | None = None, force: bool = False, ocr: bool = False) -> dict[str, Any]:
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError("A library scan is already running.")
        summary: dict[str, Any] = {"total": 0, "indexed": 0, "unchanged": 0, "failed": 0, "embedded": 0, "cancelled": False, "errors": []}
        try:
            files = sorted((path for path in self.ingest_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".pdf", ".docx"} and not path.name.startswith("~$") and path.resolve().is_relative_to(self.ingest_dir)), key=lambda path: path.as_posix().lower())
            summary["total"] = len(files)
            seen = {path.relative_to(self.ingest_dir).as_posix() for path in files}
            with self._connect() as connection:
                known = connection.execute("SELECT id,filename,status FROM documents").fetchall()
                connection.executemany("UPDATE documents SET status='missing',updated_at=? WHERE id=?", [(_now(), row["id"]) for row in known if row["filename"] not in seen and row["status"] != "excluded"])
                connection.commit()
            for position, path in enumerate(files, 1):
                if cancel is not None and cancel.is_set():
                    summary["cancelled"] = True
                    break
                filename = path.relative_to(self.ingest_dir).as_posix()
                doc_id = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:24]
                if progress:
                    progress({"phase": "extracting", "filename": filename, "current": position, "total": len(files)})
                with self._connect() as connection:
                    existing = connection.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
                    if existing and existing["status"] == "excluded" and not force:
                        summary["unchanged"] += 1
                        continue
                    if not existing:
                        connection.execute("INSERT INTO documents(id,filename,title,citation_key,updated_at) VALUES (?,?,?,?,?)", (doc_id, filename, path.stem, "source_" + doc_id[:8], _now()))
                        connection.commit()
                try:
                    stat = path.stat()
                    identical_stat = existing is not None and existing["file_size"] == stat.st_size and existing["mtime_ns"] == stat.st_mtime_ns
                    source_hash = existing["source_hash"] if identical_stat and existing["source_hash"] and not force else self._file_hash(path, cancel)
                    signature = _EXTRACTOR_VERSION + ("+ocr" if ocr else "")
                    # Unchecking OCR means do not run it on this scan; it must not
                    # discard useful OCR text/vectors already saved for this version.
                    if (existing is not None and not force and not ocr
                            and source_hash == existing["source_hash"]
                            and existing["status"] in ("indexed", "partial", "stale", "missing")
                            and existing["extraction_signature"] == _EXTRACTOR_VERSION + "+ocr"):
                        signature = existing["extraction_signature"]
                    # If OCR was unavailable or failed, explicitly enabling it again
                    # must retry after installation/language fixes without a hidden force flag.
                    retry_ocr = bool(ocr and existing is not None and any(
                        warning.startswith(("OCR is unavailable", "Local OCR failed"))
                        for warning in json.loads(existing["warnings"])
                    ))
                    unchanged = existing is not None and source_hash == existing["source_hash"] and existing["extraction_signature"] == signature and existing["status"] in ("indexed", "partial", "stale", "missing") and not force and not retry_ocr
                    if unchanged:
                        with self._connect() as connection:
                            connection.execute("UPDATE documents SET file_size=?,mtime_ns=?,status=?,updated_at=? WHERE id=?", (stat.st_size, stat.st_mtime_ns, "partial" if existing["extraction_incomplete"] or existing["error"] else "indexed", _now(), doc_id))
                            connection.commit()
                        summary["unchanged"] += 1
                    else:
                        # Never allow the prior source version to remain searchable while replacing it.
                        with self._connect() as connection:
                            connection.execute("UPDATE documents SET status='processing',error=NULL,updated_at=? WHERE id=?", (_now(), doc_id))
                            connection.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
                            connection.commit()
                        extraction = extract_document(path, ocr=ocr, cancel=cancel)
                        units = chunk_units(extraction.units)
                        final_stat = path.stat()
                        if final_stat.st_size != stat.st_size or final_stat.st_mtime_ns != stat.st_mtime_ns:
                            raise ValueError("The file changed during indexing. Save/close it and scan again.")
                        records = []
                        for ordinal, unit in enumerate(units):
                            text = unit.pop("text")
                            chunk_id = hashlib.sha256(f"{doc_id}:{source_hash}:{signature}:{ordinal}:{text}".encode("utf-8")).hexdigest()[:32]
                            records.append((chunk_id, doc_id, ordinal, source_hash, text, _json(unit)))
                        with self._connect() as connection:
                            if not existing or not existing["metadata_edited"]:
                                title = extraction.metadata.get("title") or path.stem
                                authors = extraction.metadata.get("authors", "")
                                connection.execute("UPDATE documents SET title=?,authors=? WHERE id=? AND metadata_edited=0", (title, authors, doc_id))
                            if existing and existing["source_hash"] and existing["source_hash"] != source_hash:
                                extraction.warnings.append("Source content changed; review the citation metadata and page mapping for this version.")
                                connection.execute("UPDATE documents SET verified=0,page_offset=NULL WHERE id=?", (doc_id,))
                            status = "partial" if extraction.incomplete or not records else "indexed"
                            connection.execute("""UPDATE documents SET source_hash=?,file_size=?,mtime_ns=?,status=?,pages=?,warnings=?,error=NULL,extraction_signature=?,extraction_incomplete=?,indexed_at=?,updated_at=? WHERE id=?""", (source_hash, stat.st_size, stat.st_mtime_ns, status, extraction.pages, _json(extraction.warnings), signature, int(extraction.incomplete or not records), _now(), _now(), doc_id))
                            connection.executemany("INSERT INTO chunks(id,document_id,ordinal,source_hash,text,locator) VALUES (?,?,?,?,?,?)", records)
                            connection.commit()
                        summary["indexed"] += 1
                    if embedder is not None:
                        try:
                            summary["embedded"] += self._embed_document(doc_id, embedder, cancel, progress, filename)
                            with self._connect() as connection:
                                connection.execute("UPDATE documents SET error=NULL,status=CASE WHEN extraction_incomplete=1 THEN 'partial' ELSE 'indexed' END WHERE id=?", (doc_id,))
                                connection.commit()
                        except ExtractionCancelled:
                            raise
                        except Exception as error:
                            if cancel is not None and cancel.is_set():
                                raise ExtractionCancelled() from error
                            message = "Semantic indexing incomplete; lexical search remains available. " + _safe_error(error)
                            summary["errors"].append({"filename": filename, "error": message})
                            with self._connect() as connection:
                                connection.execute("UPDATE documents SET error=?,status='partial' WHERE id=?", (message, doc_id))
                                connection.commit()
                            if getattr(error, "abort_indexing", False):
                                # A rejected key/model or exhausted billing cannot be
                                # repaired by retrying every remaining book in the corpus.
                                raise
                    if progress:
                        progress({"phase": "file_complete", "filename": filename, "current": position, "total": len(files), "summary": dict(summary)})
                except ExtractionCancelled:
                    summary["cancelled"] = True
                    with self._connect() as connection:
                        connection.execute("UPDATE documents SET status='pending',error='Indexing was cancelled; scan again to resume.' WHERE id=? AND status='processing'", (doc_id,))
                        connection.commit()
                    break
                except Exception as error:
                    if getattr(error, "abort_indexing", False):
                        # Leave the just-extracted source searchable and let the
                        # background job expose the permanent provider failure.
                        raise
                    message = _safe_error(error)
                    summary["failed"] += 1
                    summary["errors"].append({"filename": filename, "error": message})
                    with self._connect() as connection:
                        connection.execute("UPDATE documents SET status='error',error=?,updated_at=? WHERE id=?", (message, _now(), doc_id))
                        connection.commit()
            if progress:
                progress({"phase": "cancelled" if summary["cancelled"] else "complete", "summary": summary})
            return summary
        finally:
            self._scan_lock.release()

    @staticmethod
    def _terms(query: str) -> list[str]:
        words = re.findall(r"[^\W_]+(?:['’\-][^\W_]+)*", query, flags=re.UNICODE)
        filtered = [word for word in words if word.casefold() not in _STOPWORDS and len(word) <= 80]
        return list(dict.fromkeys(filtered or words))[:32]

    def _query_vector(self, query: str, embedder: EmbeddingProvider) -> np.ndarray:
        key = (str(embedder.model), int(embedder.dimensions), query)
        with self._cache_lock:
            if key in self._query_cache:
                self._query_cache.move_to_end(key)
                return self._query_cache[key]
        bounded_query = query.encode("utf-8")[:7500].decode("utf-8", errors="ignore")
        values = embedder.embed([bounded_query])
        if len(values) != 1:
            raise ValueError("Embedding provider returned an unexpected query vector count.")
        vector = self._vector(values[0], int(embedder.dimensions))
        with self._cache_lock:
            self._query_cache[key] = vector
            self._query_cache.move_to_end(key)
            while len(self._query_cache) > 64:
                self._query_cache.popitem(last=False)
        return vector

    def _semantic(self, query: str, embedder: EmbeddingProvider, candidate_count: int, document_ids: list[str] | None) -> list[tuple[str, float]]:
        filters = ""
        parameters: list[Any] = [str(embedder.model), int(embedder.dimensions)]
        if document_ids is not None:
            filters = " AND c.document_id IN (" + ",".join("?" for _ in document_ids) + ")"
            parameters.extend(document_ids)
        sql = """SELECT e.chunk_id,e.vector FROM embeddings e JOIN chunks c ON c.id=e.chunk_id JOIN documents d ON d.id=c.document_id
            WHERE e.model=? AND e.dimensions=? AND d.status IN ('indexed','partial')""" + filters
        with self._connect() as connection:
            cursor = connection.execute(sql, parameters)
            first_batch = cursor.fetchmany(256)
            if not first_batch:
                self._local.search_warnings.append("No semantic vectors for the selected embedding model are indexed yet; keyword search was used.")
                return []
            vector = self._query_vector(query, embedder)
            best: list[tuple[float, str]] = []
            batch = first_batch
            while batch:
                matrix = np.vstack([np.frombuffer(row["vector"], dtype=np.float32) for row in batch])
                scores = matrix @ vector
                keep = min(candidate_count, len(batch))
                positions = np.argpartition(scores, len(scores) - keep)[-keep:]
                for position in positions:
                    item = (float(scores[position]), batch[int(position)]["chunk_id"])
                    if len(best) < candidate_count:
                        heapq.heappush(best, item)
                    elif item > best[0]:
                        heapq.heapreplace(best, item)
                batch = cursor.fetchmany(256)
        return [(chunk_id, score) for score, chunk_id in sorted(best, reverse=True)]

    @staticmethod
    def _evidence(row: sqlite3.Row) -> dict[str, Any]:
        locator = json.loads(row["locator"])
        page = locator.get("page")
        printed_page = page + row["page_offset"] if page is not None and row["page_offset"] is not None else None
        if printed_page is not None and printed_page < 1:
            printed_page = None
        if page is not None:
            location = f"PDF page {page}"
            if locator.get("page_label"):
                location += f" (PDF label {locator['page_label']})"
            if printed_page is not None:
                location += f"; printed page {printed_page} (user-mapped)"
        elif locator.get("kind") == "table":
            start, end = locator.get("row_start"), locator.get("row_end")
            location = f"DOCX table {locator.get('table')}, " + (f"row {start}" if start == end else f"rows {start}–{end}")
        else:
            start, end = locator.get("paragraph_start"), locator.get("paragraph_end")
            location = "DOCX " + (f"paragraph {start}" if start == end else f"paragraphs {start}–{end}")
        if locator.get("heading"):
            location += f" — {locator['heading']}"
        return {
            "chunk_id": row["id"], "document_id": row["document_id"], "title": row["title"],
            "authors": row["authors"], "year": row["year"], "publisher": row["publisher"],
            "doi": row["doi"], "edition": row["edition"], "citation_key": row["citation_key"],
            "metadata_verified": bool(row["verified"]), "text": row["text"], "location": location,
            "page": page, "page_label": locator.get("page_label"), "printed_page": printed_page,
            "paragraph_start": locator.get("paragraph_start"), "paragraph_end": locator.get("paragraph_end"),
            "filename": row["filename"], "source_hash": row["source_hash"], "ordinal": row["ordinal"],
        }

    def search(self, query: str, limit: int = 12, embedder: EmbeddingProvider | None = None, document_ids: list[str] | None = None) -> list[dict[str, Any]]:
        self._local.search_warnings = []
        query = str(query).strip()[:12000]
        limit = max(1, min(40, int(limit)))
        if not query or document_ids == []:
            return []
        if document_ids is not None:
            document_ids = list(dict.fromkeys(map(str, document_ids)))[:500]
        self._refresh_file_status()
        terms = self._terms(query)
        if not terms:
            return []
        candidate_count = max(60, limit * 5)
        lexical: list[tuple[str, float]] = []
        filters = ""
        filter_parameters: list[str] = []
        if document_ids is not None:
            filters = " AND c.document_id IN (" + ",".join("?" for _ in document_ids) + ")"
            filter_parameters = document_ids
        if terms:
            # Never pass user-supplied MATCH syntax: quote independently tokenized terms.
            match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            with self._connect() as connection:
                rows = connection.execute("""
                    SELECT c.id,bm25(chunks_fts,0,0,1,3,2) AS rank FROM chunks_fts
                    JOIN chunks c ON c.id=chunks_fts.chunk_id JOIN documents d ON d.id=c.document_id
                    WHERE chunks_fts MATCH ? AND d.status IN ('indexed','partial')
                """ + filters + " ORDER BY rank LIMIT ?", [match, *filter_parameters, candidate_count]).fetchall()
                lexical = [(row["id"], float(row["rank"])) for row in rows]
        semantic: list[tuple[str, float]] = []
        if embedder is not None:
            try:
                semantic = self._semantic(query, embedder, candidate_count, document_ids)
            except Exception as error:
                self._local.search_warnings.append("Semantic search unavailable; keyword results are shown. " + _safe_error(error))
        scores: dict[str, float] = {}
        lexical_scores = dict(lexical)
        semantic_scores = dict(semantic)
        for ranking in (lexical, semantic):
            for rank, (chunk_id, _) in enumerate(ranking, 1):
                scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (60 + rank)
        if not scores:
            return []
        ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:candidate_count]
        placeholders = ",".join("?" for _ in ordered)
        with self._connect() as connection:
            rows = connection.execute("""SELECT c.*,d.title,d.authors,d.year,d.publisher,d.doi,d.edition,d.citation_key,d.verified,d.page_offset,d.filename
                FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id IN (""" + placeholders + ") AND d.status IN ('indexed','partial')", ordered).fetchall()
        by_id = {row["id"]: row for row in rows}
        evidence: list[dict[str, Any]] = []
        # Prefer breadth at the top, then fill from relevant passages in the same source.
        document_counts: dict[str, int] = {}
        deferred: list[str] = []
        for chunk_id in ordered:
            row = by_id.get(chunk_id)
            if row is None:
                continue
            doc_id = row["document_id"]
            if document_counts.get(doc_id, 0) >= 3:
                deferred.append(chunk_id)
                continue
            document_counts[doc_id] = document_counts.get(doc_id, 0) + 1
            evidence.append(self._evidence(row))
            if len(evidence) >= limit:
                break
        if len(evidence) < limit:
            evidence.extend(self._evidence(by_id[chunk_id]) for chunk_id in deferred[:limit - len(evidence)])
        for item in evidence:
            item["score"] = scores[item["chunk_id"]]
            item["lexical_score"] = lexical_scores.get(item["chunk_id"])
            item["semantic_score"] = semantic_scores.get(item["chunk_id"])
            item["retrieval_warnings"] = self.last_search_warnings
        return evidence

    def read_chunk(self, chunk_id: str, neighbors: int = 1) -> list[dict[str, Any]]:
        self._refresh_file_status()
        neighbors = max(0, min(4, int(neighbors)))
        with self._connect() as connection:
            chunk = connection.execute("SELECT document_id,ordinal,source_hash FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if chunk is None:
                return []
            rows = connection.execute("""SELECT c.*,d.title,d.authors,d.year,d.publisher,d.doi,d.edition,d.citation_key,d.verified,d.page_offset,d.filename
                FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE c.document_id=? AND c.source_hash=? AND c.ordinal BETWEEN ? AND ? AND d.status IN ('indexed','partial')
                ORDER BY c.ordinal""", (chunk["document_id"], chunk["source_hash"], chunk["ordinal"] - neighbors, chunk["ordinal"] + neighbors)).fetchall()
        return [self._evidence(row) for row in rows]
