from __future__ import annotations

import io
import json
import time

import pytest
from docx import Document
from fastapi.testclient import TestClient

from thesishelper.app import create_app

HEADERS = {"X-ThesisHelper-Request": "1"}


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return create_app(tmp_path)


@pytest.fixture
def client(local_app):
    with TestClient(local_app, base_url="http://127.0.0.1:8765", headers=HEADERS) as client:
        yield client


def docx_bytes(text="Employee inclusion can improve strategy dialogue."):
    document = Document()
    document.add_heading("Open strategy", level=1)
    document.add_paragraph(text)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def wait_job(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("Background job did not finish.")


def test_loopback_host_origin_and_mutation_header(local_app):
    with TestClient(local_app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/settings", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/api/settings", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/settings", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post("/api/documents", json={"title": "forged"}).status_code == 403
        assert client.post("/api/documents", headers=HEADERS, json={"title": "allowed"}).status_code == 200


def test_settings_never_echo_or_write_api_key(client, local_app):
    secret = "sk-test-only-not-a-real-key"
    response = client.put("/api/settings", json={"api_key": secret, "remember_key": False})
    assert response.status_code == 200
    assert response.json()["api_key_set"]
    for url in ("/api/settings", "/api/state"):
        assert secret not in client.get(url).text
    assert secret not in (local_app.state.root / "data" / "settings.json").read_text()
    # Invalid fields must not make Pydantic echo the key-containing request.
    response = client.put("/api/settings", json={"api_key": secret, "max_output_tokens": secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert client.put("/api/settings", json={"api_key": ""}).json()["api_key_set"] is False


def test_document_save_conflict_import_export_and_history(client):
    document = client.post("/api/documents", json={"title": "Digital inclusion"}).json()
    url = f"/api/documents/{document['id']}"
    content = {"type": "doc", "content": [{"type": "heading", "attrs": {"level": 1},
                  "content": [{"type": "text", "text": "Introduction"}]},
                 {"type": "paragraph", "content": [{"type": "text", "text": "A careful claim."}]}]}
    saved = client.put(url, json={"title": "Digital inclusion", "content": content, "revision": 1})
    assert saved.status_code == 200
    assert saved.json()["revision"] == 2
    assert client.put(url, json={"title": "old", "content": content, "revision": 1}).status_code == 409
    export = client.get(url + "/export")
    assert export.status_code == 200
    assert "Introduction" in [p.text for p in Document(io.BytesIO(export.content)).paragraphs]
    assert len(client.get(url + "/history").json()["versions"]) == 2
    restored = client.post(url + "/restore", json={"revision": 1, "current_revision": 2})
    assert restored.json()["revision"] == 3
    imported = client.post("/api/documents/import", files={"file": ("Änderung.docx", docx_bytes())})
    assert imported.status_code == 200
    assert imported.json()["title"] == "Änderung"
    assert imported.json()["warnings"]
    assert client.get("/api/documents/" + "f" * 32).status_code == 404


def test_upload_index_search_metadata_and_source_file(client, local_app):
    payload = docx_bytes()
    result = client.post("/api/library/upload", files=[("files", ("../book.docx", payload)),
                         ("files", ("wrong.pdf", b"not a PDF"))])
    assert result.status_code == 200
    assert result.json()["uploaded"] == ["book.docx"]
    assert len(result.json()["errors"]) == 1
    assert (local_app.state.root / "ingest" / "book.docx").is_file()
    again = client.post("/api/library/upload", files=[("files", ("book.docx", payload))])
    assert again.json()["uploaded"] == ["book (2).docx"]
    start = client.post("/api/library/index", json={"semantic": False})
    job = wait_job(client, start.json()["job_id"])
    assert job["status"] == "completed", job
    result = client.get("/api/library/search", params={"q": "employee inclusion"}).json()
    assert result["results"]
    evidence = result["results"][0]
    assert evidence["page"] is None
    assert not evidence["citation_ready"]
    url = f"/api/library/{evidence['document_id']}"
    meta = {"authors": "Smith, A.", "year": "2024", "publisher": "Example Press", "verified": True}
    assert client.patch(url, json=meta).status_code == 200
    assert client.get(url + "/file").content == payload
    assert client.get("/api/library/chunks/" + evidence["chunk_id"]).json()["chunks"]
    assert "@book{" in client.get("/api/library/bibliography?format=bibtex").text
    assert "TY  - BOOK" in client.get("/api/library/bibliography?format=ris").text


def test_assistant_missing_key_and_stale_selection(client, local_app):
    document = client.get("/api/state").json()["documents"][0]
    body = {"document_id": document["id"], "action": "evidence", "selection": "This is a claim."}
    assert client.post("/api/assistant", json=body).status_code == 400
    client.put("/api/settings", json={"api_key": "sk-test-only", "remember_key": False})
    body["selection_revision"] = document["revision"] + 1
    assert client.post("/api/assistant", json=body).status_code == 409


def test_background_assistant_contract_without_paid_api(client, local_app):
    document = client.get("/api/state").json()["documents"][0]
    client.put("/api/settings", json={"api_key": "sk-test-only", "remember_key": False})

    def fake_run(request, document, settings, key, progress, cancel):
        assert key == "sk-test-only"
        result = {"answer": "No indexed evidence yet.", "suggestion": "", "citations": [],
                  "warnings": [], "usage": {}, "model": settings["model"], "trace": []}
        local_app.state.journal.add(document["id"], "assistant", result["answer"], result)
        return result
    local_app.state.assistant.run = fake_run
    body = {"document_id": document["id"], "action": "ask", "message": "What is inclusion?"}
    start = client.post("/api/assistant", json=body)
    assert start.status_code == 200
    job = wait_job(client, start.json()["job_id"])
    assert job["status"] == "completed"
    assert job["result"]["answer"] == "No indexed evidence yet."
    history = client.get("/api/chat", params={"document_id": document["id"]}).json()["messages"]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert "sk-test-only" not in json.dumps(history)
