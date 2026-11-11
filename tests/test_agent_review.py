"""Regression checks for trust boundaries and bounded local research workflows."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

import thesishelper.config as config_module
import thesishelper.journal as journal_module
from thesishelper.assistant import ResearchAssistant
from thesishelper.citations import finalize_answer, quote_in_source
from thesishelper.config import DEFAULTS, SettingsStore
from thesishelper.gateway import APIError, Cancelled, OpenAIGateway
from thesishelper.journal import Journal


def source(chunk="chunk-a", document="book-a", body="Participation can improve knowledge sharing."):
    return {"chunk_id": chunk, "document_id": document, "text": body,
            "title": "A local book", "authors": "Smith, A.", "year": "2024",
            "publisher": "University Press", "metadata_verified": True,
            "page": 12, "printed_page": 10, "page_label": "12", "location": "PDF page 12",
            "filename": "book.pdf", "source_hash": "hash-a"}


def final_response(answer="Supported finding [S1]", suggestion=""):
    result = {"answer": answer, "suggestion": suggestion,
              "citations": [{"source_id": "S1", "quote": "Participation can improve knowledge sharing.", "note": "Direct support"}],
              "warnings": []}
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "output_tokens_details": {"reasoning_tokens": 4}}}


def tool_response(name, arguments, call_id="call-1"):
    return {"status": "completed", "output": [
        {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "encrypted-reasoning"},
        {"type": "function_call", "id": "fc-1", "call_id": call_id,
         "name": name, "arguments": json.dumps(arguments)},
    ], "usage": {"input_tokens": 20, "output_tokens": 10}}


class FakeLibrary:
    def __init__(self, sources=None):
        self.sources = sources if sources is not None else [source()]
        self.last_search_warnings = []
        self.queries = []
        self.reads = []

    def stats(self):
        return {"embedded_chunks": 0}

    def search(self, query, **kwargs):
        self.queries.append((query, kwargs))
        return copy.deepcopy(self.sources)

    def read_chunk(self, chunk_id, **kwargs):
        self.reads.append(chunk_id)
        return copy.deepcopy(self.sources)

    def list_documents(self):
        return [{"id": "book-a", "title": "A local book", "authors": "Smith, A.", "filename": "book.pdf"}]


class FakeJournal:
    def __init__(self):
        self.entries = []

    def list(self, *args, **kwargs):
        return []

    def add(self, *args):
        self.entries.append(args)


class FakeGateway:
    embedding_tokens = 0

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def respond(self, settings, instructions, inputs, **kwargs):
        self.calls.append({"inputs": copy.deepcopy(inputs), **kwargs})
        return copy.deepcopy(self.responses[len(self.calls) - 1])


def run_agent(library, gateway, cancel=None):
    journal = FakeJournal()
    assistant = ResearchAssistant(library, journal, gateway_factory=lambda *args, **kwargs: gateway)
    result = assistant.run(
        {"action": "ask", "message": "Participation", "selection": "", "document_ids": ["book-a"]},
        {"id": "draft", "title": "Draft", "content": {"type": "doc", "content": []}},
        DEFAULTS, "test-key", lambda value: None, cancel or threading.Event(),
    )
    return result, journal


def test_unknown_passage_request_never_reads_an_arbitrary_chunk():
    library = FakeLibrary()
    gateway = FakeGateway([tool_response("read_passages", {"chunk_id": "not-discovered"}), final_response()])
    result, _ = run_agent(library, gateway)
    assert library.reads == []
    tool_outputs = [item for item in gateway.calls[1]["inputs"] if item.get("type") == "function_call_output"]
    assert "already returned by search" in tool_outputs[0]["output"]
    assert result["usage"]["model_calls"] == 2


def test_selected_source_scope_is_enforced_again_when_packing_evidence():
    library = FakeLibrary([source(), source("chunk-outside", "book-outside", "Unselected secret")])
    gateway = FakeGateway([final_response()])
    result, _ = run_agent(library, gateway)
    assert result["evidence_count"] == 1
    assert "Unselected secret" not in gateway.calls[0]["inputs"][0]["content"]
    assert library.queries[0][1]["document_ids"] == ["book-a"]


def test_agent_forces_final_response_at_bounded_round_limit():
    gateway = FakeGateway([
        tool_response("search_library", {"query": "first"}, "call-1"),
        tool_response("search_library", {"query": "second"}, "call-2"),
        tool_response("search_library", {"query": "third"}, "call-3"),
        final_response(),
    ])
    result, _ = run_agent(FakeLibrary(), gateway)
    assert len(gateway.calls) == 4
    assert [call["force_final"] for call in gateway.calls] == [False, False, False, True]
    assert result["usage"]["input_tokens"] == 160
    assert result["usage"]["output_tokens"] == 50


def test_unsupported_tool_is_not_executed_and_stateless_reasoning_is_preserved():
    gateway = FakeGateway([
        tool_response("execute_shell", {"command": "remove every document"}), final_response(),
    ])
    run_agent(FakeLibrary(), gateway)
    replay = gateway.calls[1]["inputs"]
    assert replay[1]["encrypted_content"] == "encrypted-reasoning"
    assert replay[2]["call_id"] == "call-1"
    assert "Unsupported tool" in replay[3]["output"]


def test_pre_cancelled_agent_never_retrieves_or_calls_model():
    event = threading.Event()
    event.set()
    library = FakeLibrary()
    gateway = FakeGateway([])
    with pytest.raises(Cancelled):
        run_agent(library, gateway, event)
    assert not gateway.calls
    assert not library.queries


def test_settings_session_secret_is_absent_from_public_and_persisted_json(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = SettingsStore(tmp_path)
    secret = "sk-test-private-example"
    settings.update({"api_key": secret, "remember_key": False, "model": "gpt-6-astra"})
    assert secret not in json.dumps(settings.public())
    assert secret not in (tmp_path / "settings.json").read_text("utf-8")
    assert settings.snapshot()[1] == secret
    assert SettingsStore(tmp_path).snapshot()[1] == ""


def test_windows_protected_key_can_be_reloaded_and_forgotten(tmp_path, monkeypatch):
    # Simulate only the persistence boundary; never invoke platform crypto on Linux.
    fake_os = SimpleNamespace(name="nt", environ={}, replace=os.replace, fsync=os.fsync)
    monkeypatch.setattr(config_module, "os", fake_os)
    def protect(value, decrypt=False):
        return value[::-1] if decrypt else value[::-1]
    monkeypatch.setattr(config_module, "_protect_windows", protect)
    secret = "sk-example-to-protect"
    settings = SettingsStore(tmp_path)
    settings.update({"api_key": secret, "remember_key": True})
    assert (tmp_path / "key.dpapi").read_bytes() != secret.encode()
    assert SettingsStore(tmp_path).snapshot()[1] == secret
    settings.update({"remember_key": False})
    assert not (tmp_path / "key.dpapi").exists()
    assert settings.snapshot()[1] == secret
    assert settings.public()["key_storage"] == "session"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DPAPI roundtrip")
def test_native_windows_dpapi_encrypts_and_decrypts_without_plaintext_storage():
    # This is synthetic text, never an API credential or a request to any API.
    plaintext = b"thesishelper-synthetic-test-secret"
    protected = config_module._protect_windows(plaintext)
    assert protected != plaintext
    assert plaintext not in protected
    assert config_module._protect_windows(protected, decrypt=True) == plaintext


def test_upstream_error_never_echoes_api_key():
    secret = "sk-should-not-escape"
    transport = httpx.MockTransport(lambda request: httpx.Response(
        400, json={"error": {"message": f"Invalid token {secret}"}}))
    gateway = OpenAIGateway(secret, transport=transport)
    with pytest.raises(APIError) as error:
        gateway.list_models()
    assert secret not in str(error.value)
    assert "[redacted]" in str(error.value)


def test_timed_out_generation_is_not_automatically_replayed():
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out", request=request)
    gateway = OpenAIGateway("test-key", transport=httpx.MockTransport(timeout))
    with pytest.raises(APIError, match="No automatic"):
        gateway.respond(DEFAULTS, "instructions", [{"role": "user", "content": "question"}])
    assert len(calls) == 1


def test_responses_request_contains_only_authorized_local_tools_and_no_remote_state():
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=final_response())
    gateway = OpenAIGateway("test-key", transport=httpx.MockTransport(respond))
    gateway.respond(DEFAULTS, "instructions", [{"role": "user", "content": "question"}],
                    tools=[{"type": "function", "name": "search_library", "parameters": {}}], force_final=True)
    request = sent[0]
    assert request["store"] is False
    assert request["tool_choice"] == "none"
    assert request["parallel_tool_calls"] is False
    assert "temperature" not in request
    assert request["tools"][0]["name"] == "search_library"


def test_unrecognized_source_ids_cannot_become_valid_suggestion_citations():
    result = finalize_answer({"answer": "Claim [S99]", "suggestion": "Claim [S99]",
                              "citations": [{"source_id": "S99", "quote": "Made up"}], "warnings": []}, {}, "apa")
    assert result["citations"] == []
    assert "[S99]" not in result["suggestion"]
    assert result["warnings"]


def test_unverified_printed_pages_keep_an_explicit_review_marker():
    evidence = source()
    evidence["printed_page"] = None
    result = finalize_answer({"answer": "Claim [S1]", "suggestion": "Claim [S1]",
                              "citations": [{"source_id": "S1", "quote": ""}], "warnings": []}, {"S1": evidence}, "apa")
    assert "verify citation" in result["suggestion"]
    assert "p. 12" not in result["suggestion"]
    assert result["citations"][0]["citation_ready"] is False


def test_scientific_superscripts_are_not_verified_as_different_quoted_text():
    assert quote_in_source("participation  improves", "participation\n improves")
    assert not quote_in_source("x2", "The expression x² has a squared term.")
    assert not quote_in_source("H2O", "H₂O is shown with a subscript.")


def test_failed_verbatim_quote_cannot_remain_a_ready_to_insert_citation():
    result = finalize_answer({"answer": "The quote is useful [S1]",
                              "suggestion": '"Invented quotation" [S1]',
                              "citations": [{"source_id": "S1", "quote": "Invented quotation", "note": "Direct quote"}],
                              "warnings": []}, {"S1": source()}, "apa")
    assert not result["citations"][0]["quote_verified"]
    assert not result["suggestion"] or "verify quotation" in result["suggestion"].lower()


def test_journal_connections_are_closed_after_each_operation(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    created = []
    class TrackedConnection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            return super().close()
    def tracked_connect(*args, **kwargs):
        result = real_connect(*args, factory=TrackedConnection, **kwargs)
        created.append(result)
        return result
    monkeypatch.setattr(journal_module.sqlite3, "connect", tracked_connect)
    try:
        journal = Journal(tmp_path / "chat.sqlite3")
        journal.add("document", "user", "Question")
        assert journal.list("document")[0]["content"] == "Question"
        assert created and all(connection.closed for connection in created)
    finally:
        for connection in created:
            connection.close()
