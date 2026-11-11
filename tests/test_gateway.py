import json
import threading

import httpx
import pytest

from thesishelper.config import DEFAULTS
from thesishelper.gateway import APIError, Cancelled, OpenAIGateway, replayable_output, response_text


def test_responses_payload_has_no_browsing_storage_or_sampling_parameters():
    seen = []

    def handler(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]}]})
    gateway = OpenAIGateway("test-key", transport=httpx.MockTransport(handler))
    result = gateway.respond({**DEFAULTS, "reasoning_mode": "pro"}, "instructions", [{"role": "user", "content": "hello"}])
    assert response_text(result) == "done"
    assert seen[0]["store"] is False
    assert seen[0]["model"] == "gpt-6-astra"
    assert seen[0]["reasoning"] == {"effort": "high", "mode": "pro"}
    assert not set(seen[0]) & {"temperature", "top_p", "previous_response_id", "background"}
    assert "tools" not in seen[0]


def test_embeddings_sorted_and_dimensionality_validated():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "text-embedding-3-large"
        assert payload["dimensions"] == 1024
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0.2] * 1024},
                                                {"index": 0, "embedding": [0.1] * 1024}],
                                        "usage": {"total_tokens": 8}})
    gateway = OpenAIGateway("test-key", transport=httpx.MockTransport(handler))
    vectors = gateway.embed(["A passage.", "Another passage."])
    assert vectors[0][0] == 0.1
    assert gateway.embedding_tokens == 8
    with pytest.raises(APIError, match="7,500-byte"):
        gateway.embed(["🐦" * 2000])


def test_refusal_incomplete_and_key_redaction():
    with pytest.raises(APIError, match="declined"):
        response_text({"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "declined"}]}]})
    gateway = OpenAIGateway("secret", transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}})))
    with pytest.raises(APIError, match="Increase the output budget"):
        gateway.respond(DEFAULTS, "", [])
    gateway = OpenAIGateway("secret", transport=httpx.MockTransport(lambda r: httpx.Response(
        400, json={"error": {"message": "bad input secret"}})))
    with pytest.raises(APIError) as error:
        gateway.list_models()
    assert "secret" not in str(error.value)


def test_replay_preserves_reasoning_calls_and_phase():
    output = [
        {"type": "reasoning", "id": "r", "summary": [], "encrypted_content": "encrypted", "status": "completed"},
        {"type": "function_call", "id": "f", "call_id": "c", "name": "search_library", "arguments": "{}", "status": "completed"},
        {"type": "message", "id": "m", "role": "assistant", "phase": "commentary", "status": "completed",
         "content": [{"type": "output_text", "text": "searching", "annotations": [], "logprobs": []}]},
    ]
    replayed = replayable_output({"output": output})
    assert replayed[0]["encrypted_content"] == "encrypted"
    assert replayed[1]["call_id"] == "c"
    assert replayed[2]["phase"] == "commentary"
    assert all("status" not in item for item in replayed)


def test_cancelled_call_makes_no_network_request():
    event = threading.Event()
    event.set()
    gateway = OpenAIGateway("test-key", cancel=event, transport=httpx.MockTransport(
        lambda r: pytest.fail("Cancelled request reached network")))
    with pytest.raises(Cancelled):
        gateway.list_models()
