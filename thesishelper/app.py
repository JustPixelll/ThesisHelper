"""Loopback-only HTTP application. Static UI and all user data are served locally."""
from __future__ import annotations

import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .assistant import ResearchAssistant
from .citations import bibliography_export, citation_details
from .config import MODELS, SettingsStore
from .documents import DocumentStore, RevisionConflict
from .gateway import APIError, OpenAIGateway
from .jobs import Jobs
from .journal import Journal
from .library import Library

ROOT = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 256 * 1024 * 1024
MAX_REQUEST = 1024 * 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsBody(Body):
    api_key: str | None = Field(default=None, max_length=1000)
    remember_key: bool | None = None
    model: str | None = Field(default=None, max_length=120)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    reasoning_mode: Literal["standard", "pro"] | None = None
    language: Literal["en", "de"] | None = None
    citation_style: Literal["apa", "harvard"] | None = None
    max_output_tokens: int | None = Field(default=None, ge=2000, le=32000)


class IndexBody(Body):
    semantic: bool = False
    ocr: bool = False
    force: bool = False


class NewDocument(Body):
    title: str = Field(default="Untitled thesis", min_length=1, max_length=200)


class SaveDocument(NewDocument):
    content: dict
    revision: int = Field(ge=1)


class RestoreDocument(Body):
    revision: int = Field(ge=1)
    current_revision: int = Field(ge=1)


class MetadataBody(Body):
    title: str | None = Field(default=None, max_length=2000)
    authors: str | None = Field(default=None, max_length=2000)
    year: str | None = Field(default=None, max_length=100)
    publisher: str | None = Field(default=None, max_length=2000)
    doi: str | None = Field(default=None, max_length=1000)
    edition: str | None = Field(default=None, max_length=200)
    citation_key: str | None = Field(default=None, max_length=80)
    page_offset: int | None = Field(default=None, ge=-100000, le=100000)
    verified: bool | None = None


class AssistantBody(Body):
    document_id: str = Field(min_length=1, max_length=100)
    action: Literal["ask", "rewrite", "evidence", "critique", "citations", "summarize"] = "ask"
    message: str = Field(default="", max_length=16000)
    selection: str = Field(default="", max_length=20000)
    context: str = Field(default="", max_length=50000)
    document_ids: list[str] = Field(default_factory=list, max_length=500)
    selection_revision: int | None = None


def safe_upload_name(name: str | None) -> str:
    # Both path separators matter when a Windows browser talks to a Linux test host.
    name = (name or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("Only PDF and DOCX files are supported.")
    stem = Path(name).stem[:160].strip(" .") or "source"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", stem):
        stem = "source_" + stem
    return stem + suffix


async def receive_file(upload: UploadFile, directory: Path, docx_only: bool = False) -> tuple[Path, str]:
    name = safe_upload_name(upload.filename)
    if docx_only and Path(name).suffix != ".docx":
        raise ValueError("Import a .docx Word document.")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / ("upload-" + uuid.uuid4().hex + ".part")
    total = 0
    try:
        with temporary.open("xb") as stream:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise ValueError("This file exceeds the 256 MB upload limit. Put large PDFs in ingest/ directly.")
                stream.write(chunk)
        if not total:
            raise ValueError("This file is empty.")
        if Path(name).suffix == ".pdf":
            with temporary.open("rb") as stream:
                if b"%PDF-" not in stream.read(1024):
                    raise ValueError("The file does not contain a PDF header.")
        else:
            try:
                with ZipFile(temporary) as archive:
                    if "word/document.xml" not in archive.namelist():
                        raise ValueError("This is not a DOCX Word document.")
                    if sum(item.file_size for item in archive.infolist()) > 500 * 1024 * 1024:
                        raise ValueError("This DOCX expands beyond 500 MB. Split it before importing.")
            except BadZipFile as error:
                raise ValueError("This DOCX file is damaged or has the wrong file type.") from error
        return temporary, name
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def create_app(root: Path | None = None) -> FastAPI:
    root = Path(root or os.environ.get("THESISHELPER_HOME", ROOT)).resolve()
    data = root / "data"
    settings = SettingsStore(data)
    library = Library(data / "library.sqlite3", root / "ingest")
    documents = DocumentStore(root)
    journal = Journal(data / "chat.sqlite3")
    jobs = Jobs()
    assistant = ResearchAssistant(library, journal)
    upload_lock = threading.Lock()
    if not documents.list_documents():
        documents.create("My thesis")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(title="ThesisHelper", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.library = library
    app.state.documents = documents
    app.state.journal = journal
    app.state.jobs = jobs
    app.state.assistant = assistant
    app.state.root = root

    @app.middleware("http")
    async def protect_local_server(request: Request, call_next):
        # Reject hostile Host headers even if DNS points an attacker's domain at 127.0.0.1.
        try:
            host = urlsplit("http://" + request.headers.get("host", ""))
            if host.hostname not in LOCAL_HOSTS or host.username or host.password:
                return JSONResponse({"detail": "ThesisHelper accepts localhost connections only."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers['host']}":
                return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Open ThesisHelper directly on localhost."}, status_code=403)
            content_length = int(request.headers.get("content-length", "0"))
        except (ValueError, KeyError):
            return JSONResponse({"detail": "Invalid request headers."}, status_code=400)
        if content_length > MAX_REQUEST:
            return JSONResponse({"detail": "Upload fewer files at once or use the ingest folder."}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-thesishelper-request") != "1":
                return JSONResponse({"detail": "Missing local application request header."}, status_code=403)
            if request.url.path.startswith("/api/") and content_length > 12 * 1024 * 1024 and "multipart/form-data" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "This request is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RevisionConflict)
    async def revision_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(KeyError)
    async def key_error(request, error):
        return JSONResponse({"detail": "The requested document or revision was not found."}, status_code=404)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, error):
        return JSONResponse({"detail": "The requested document, revision, or source file was not found."}, status_code=404)

    @app.exception_handler(APIError)
    async def api_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # FastAPI's default includes input values; an invalid Settings body could expose a key.
        parts = [".".join(str(p) for p in e["loc"] if p != "body") + ": " + e["msg"] for e in error.errors()]
        return JSONResponse({"detail": "; ".join(parts)[:1200]}, status_code=422)

    def get_document(doc_id: str) -> dict:
        document = documents.get(doc_id)
        if not document:
            raise HTTPException(404, "Document not found.")
        return document

    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "ThesisHelper", "version": __version__}

    @app.get("/api/state")
    def state():
        drafts = documents.list_documents()
        return {"settings": settings.public(), "library": library.stats(), "documents": drafts,
                "active_document_id": drafts[0]["id"] if drafts else None, "models": MODELS,
                "jobs": jobs.active(), "version": __version__}

    @app.get("/api/settings")
    def get_settings():
        return settings.public()

    @app.put("/api/settings")
    def update_settings(body: SettingsBody):
        return settings.update(body.model_dump(exclude_none=True))

    @app.post("/api/settings/test")
    def test_settings():
        config, key = settings.snapshot()
        gateway = OpenAIGateway(key)
        available = gateway.list_models()
        present = config["model"] in available
        return {"ok": present, "models": available,
                "message": (f"API key accepted. {config['model']} is listed for your project. "
                            "Model execution and billing limits will be checked on your first query.") if present else
                (f"API key accepted, but {config['model']} is not listed for your project. "
                 "Choose a model available to your API account or enter an available model ID.")}

    @app.get("/api/library")
    def get_library():
        return {"documents": library.list_documents(), "stats": library.stats()}

    @app.post("/api/library/upload")
    async def upload_sources(files: Annotated[list[UploadFile], File()]):
        if len(files) > 500:
            raise HTTPException(400, "Upload at most 500 files per batch, or use ingest/.")
        uploaded, errors = [], []
        for upload in files:
            filename = upload.filename or "untitled"
            try:
                temporary, name = await receive_file(upload, data / "tmp")
                with upload_lock:
                    destination = root / "ingest" / name
                    number = 2
                    while destination.exists():
                        destination = root / "ingest" / f"{Path(name).stem} ({number}){Path(name).suffix}"
                        number += 1
                    os.replace(temporary, destination)
                uploaded.append(destination.name)
            except (ValueError, OSError) as error:
                errors.append({"filename": filename, "error": str(error)[:400]})
        return {"uploaded": uploaded, "errors": errors}

    @app.post("/api/library/index")
    def index_sources(body: IndexBody):
        config, key = settings.snapshot()
        if body.semantic and not key:
            raise HTTPException(400, "Add an API key before semantic indexing, or choose local text indexing.")

        def task(progress, cancel):
            embedder = OpenAIGateway(key, cancel=cancel) if body.semantic else None
            result = library.scan(embedder=embedder, progress=progress, cancel=cancel,
                                  force=body.force, ocr=body.ocr)
            return {**result, "embedding_tokens": embedder.embedding_tokens if embedder else 0}
        return {"job_id": jobs.start("index", task)}

    @app.get("/api/library/search")
    def search_library(q: str = "", semantic: bool = False):
        if not q.strip():
            return {"results": [], "warnings": []}
        if len(q) > 3500:
            raise HTTPException(400, "Use a shorter search query (up to 3,500 characters).")
        config, key = settings.snapshot()
        embedder = OpenAIGateway(key) if semantic else None
        result = library.search(q, limit=20, embedder=embedder)
        return {"results": [{**row, **citation_details(row, config["citation_style"])} for row in result],
                "warnings": library.last_search_warnings}

    @app.get("/api/library/bibliography")
    def bibliography(format: Literal["bibtex", "ris"] = "bibtex"):
        result = bibliography_export(library.list_documents(), format)
        extension = "bib" if format == "bibtex" else "ris"
        return PlainTextResponse(result, headers={"Content-Disposition": f'attachment; filename="ThesisHelper-library.{extension}"'})

    @app.get("/api/library/chunks/{chunk_id}")
    def get_chunks(chunk_id: str):
        result = library.read_chunk(chunk_id, neighbors=1)
        if not result:
            raise HTTPException(404, "This evidence version is no longer active. Reindex or search the current source.")
        config, _ = settings.snapshot()
        return {"chunks": [{**row, **citation_details(row, config["citation_style"])} for row in result]}

    @app.patch("/api/library/{doc_id}")
    def update_metadata(doc_id: str, body: MetadataBody):
        # Explicit null means clear the page offset; other null fields are not meaningful.
        patch = body.model_dump(exclude_unset=True)
        for name in list(patch):
            if patch[name] is None and name != "page_offset":
                patch.pop(name)
        return library.update_metadata(doc_id, patch)

    @app.get("/api/library/{doc_id}/file")
    def source_file(doc_id: str):
        path = library.document_path(doc_id)
        if not path or not path.is_file():
            raise HTTPException(404, "Source file is unavailable. Check ingest/ and reindex.")
        media = "application/pdf" if path.suffix.lower() == ".pdf" else \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return FileResponse(path, filename=path.name, media_type=media,
                            content_disposition_type="inline" if path.suffix.lower() == ".pdf" else "attachment")

    @app.get("/api/documents")
    def list_documents():
        return {"documents": documents.list_documents()}

    @app.post("/api/documents")
    def new_document(body: NewDocument):
        return documents.create(body.title)

    @app.post("/api/documents/import")
    async def import_document(file: Annotated[UploadFile, File()]):
        temporary, name = await receive_file(file, data / "tmp", docx_only=True)
        # python-docx reads the ZIP content independently of the temporary suffix.
        try:
            return await run_in_threadpool(documents.import_docx, temporary, title=Path(name).stem)
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/documents/{doc_id}")
    def read_document(doc_id: str):
        return get_document(doc_id)

    @app.put("/api/documents/{doc_id}")
    def save_document(doc_id: str, body: SaveDocument):
        return documents.save(doc_id, body.title, body.content, body.revision)

    @app.get("/api/documents/{doc_id}/export")
    def export_document(doc_id: str):
        document = get_document(doc_id)
        path = documents.export_path(doc_id)
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", document["title"])[:120].strip(" .") or "thesis"
        return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            filename=filename + ".docx")

    @app.get("/api/documents/{doc_id}/history")
    def document_history(doc_id: str):
        get_document(doc_id)
        return {"versions": documents.history(doc_id)}

    @app.post("/api/documents/{doc_id}/restore")
    def restore_document(doc_id: str, body: RestoreDocument):
        return documents.restore(doc_id, body.revision, body.current_revision)

    @app.get("/api/chat")
    def chat(document_id: str):
        get_document(document_id)
        return {"messages": journal.list(document_id)}

    @app.post("/api/assistant")
    def ask_assistant(body: AssistantBody):
        document = get_document(body.document_id)
        config, key = settings.snapshot()
        if not key:
            raise HTTPException(400, "Add an OpenAI API key in Settings to use the assistant.")
        if not body.message.strip() and not body.selection.strip():
            raise HTTPException(400, "Ask a question or select text in your thesis first.")
        if body.selection_revision is not None and body.selection_revision != document["revision"]:
            raise HTTPException(409, "The document changed since that selection. Save and select the passage again.")
        ids = {doc["id"] for doc in library.list_documents()}
        if any(doc_id not in ids for doc_id in body.document_ids):
            raise HTTPException(400, "A selected source no longer exists. Refresh the Library.")
        payload = body.model_dump()

        def task(progress, cancel):
            self_text = body.message or f"{body.action.capitalize()}: {body.selection}"
            journal.add(document["id"], "user", self_text[:20000])
            return assistant.run(payload, document, config, key, progress, cancel)
        return {"job_id": jobs.start("assistant", task, key="assistant:" + document["id"])}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found. It may have ended before the app restarted.")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = jobs.cancel(job_id)
        if job is None:
            raise HTTPException(404, "Job not found.")
        return job

    static = Path(__file__).resolve().parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        index_path = static / "index.html"
        if not index_path.exists():
            return PlainTextResponse("The frontend bundle is missing. Download the complete repository ZIP.", status_code=503)
        return FileResponse(index_path)

    return app


# Launch through run.py for browser opening and Windows instance/port checks.
# No import-time instance: a second launcher must acquire the process lock before
# touching the database or creating a document.
