"""Small OpenAI HTTPS client, deliberately limited to models, embeddings and Responses."""
from __future__ import annotations

import json
import math
import threading
from typing import Any

import httpx

API_BASE = "https://api.openai.com/v1"


class APIError(RuntimeError):
    def __init__(self, message: str, *, abort_indexing: bool = False):
        super().__init__(message)
        self.abort_indexing = abort_indexing


class Cancelled(RuntimeError):
    pass


class OpenAIGateway:
    model = "text-embedding-3-large"
    dimensions = 1024

    def __init__(self, api_key: str, cancel: threading.Event | None = None,
                 transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise APIError("Add an OpenAI API key in Settings first. Local editing and keyword search work without it.")
        self._key = api_key
        self.cancel = cancel or threading.Event()
        self.transport = transport
        self.embedding_tokens = 0

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("Cancelled. An API request already in progress may still be billed.")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        self._check_cancel()
        # Never follow redirects carrying Authorization, and never log request bodies.
        with httpx.Client(base_url=API_BASE, headers={"Authorization": f"Bearer {self._key}"},
                          timeout=httpx.Timeout(600, connect=20), follow_redirects=False,
                          transport=self.transport) as client:
            for attempt in range(3):
                self._check_cancel()
                try:
                    response = client.request(method, path, json=payload)
                except httpx.TimeoutException as error:
                    # Replaying a timed-out generation can duplicate charges: leave retry to the user.
                    raise APIError("OpenAI did not finish within 10 minutes. No automatic generation retry was made. "
                                   "Try a smaller task or a lower reasoning effort.") from error
                except httpx.RequestError as error:
                    raise APIError("Could not reach OpenAI. Check your internet connection, proxy, and firewall.") from error
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    if response.status_code == 429:
                        try:
                            code = response.json().get("error", {}).get("code")
                        except (ValueError, AttributeError):
                            code = ""
                        if code in {"insufficient_quota", "billing_hard_limit_reached"}:
                            raise APIError("OpenAI API credit or billing limit reached. Check the API dashboard.",
                                           abort_indexing=True)
                    try:
                        wait = min(20.0, max(1.0, float(response.headers.get("retry-after", 2 ** attempt))))
                    except ValueError:
                        wait = float(2 ** attempt)
                    if self.cancel.wait(wait):
                        raise Cancelled("Cancelled.")
                    continue
                if response.is_error or response.is_redirect:
                    messages = {
                        401: "OpenAI rejected the API key. Replace it in Settings.",
                        403: "Your OpenAI project cannot access this model or API feature.",
                        404: "OpenAI could not find the selected model. Select an available model in Settings.",
                        429: "OpenAI rate or usage limit reached. Check API limits and retry later.",
                    }
                    message = messages.get(response.status_code)
                    if not message:
                        message = f"OpenAI returned HTTP {response.status_code}."
                        try:
                            detail = response.json().get("error", {}).get("message", "")
                            # The upstream may echo a request value. Always redact the secret.
                            message += " " + str(detail).replace(self._key, "[redacted]")[:600]
                        except (ValueError, AttributeError):
                            pass
                    raise APIError(message, abort_indexing=response.status_code in {400, 401, 403, 404})
                try:
                    result = response.json()
                except ValueError as error:
                    raise APIError("OpenAI returned an unreadable response. Try again later.") from error
                self._check_cancel()
                if not isinstance(result, dict):
                    raise APIError("OpenAI returned an unexpected response shape.")
                return result
        raise APIError("OpenAI is temporarily unavailable.")

    def list_models(self) -> list[str]:
        result = self._request("GET", "/models")
        return sorted(item["id"] for item in result.get("data", []) if isinstance(item.get("id"), str))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Each extraction chunk is <= 7,500 UTF-8 bytes. At most 32 inputs keeps
        # even the byte-token upper bound below the batch embeddings token cap.
        vectors = []
        for start in range(0, len(texts), 32):
            batch = texts[start:start + 32]
            if any(not text.strip() or len(text.encode("utf-8")) > 7500 for text in batch):
                raise APIError("An embedding input is empty or exceeds the local 7,500-byte limit.")
            result = self._request("POST", "/embeddings", {"model": self.model, "input": batch,
                                    "dimensions": self.dimensions, "encoding_format": "float"})
            items = sorted(result.get("data", []), key=lambda item: item.get("index", -1))
            if [item.get("index") for item in items] != list(range(len(batch))):
                raise APIError("OpenAI returned an incomplete embedding batch; resume indexing to retry it.")
            for item in items:
                vector = item.get("embedding", [])
                if (len(vector) != self.dimensions or
                        any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)):
                    raise APIError("OpenAI returned an invalid embedding vector.")
                vectors.append(vector)
            self.embedding_tokens += result.get("usage", {}).get("total_tokens", 0)
        return vectors

    def respond(self, settings: dict, instructions: str, inputs: list[dict],
                tools: list[dict] | None = None, schema: dict | None = None,
                force_final: bool = False, output_budget: int | None = None) -> dict:
        reasoning: dict[str, Any] = {"effort": settings["reasoning_effort"]}
        if settings.get("reasoning_mode") == "pro":
            reasoning["mode"] = "pro"
        payload: dict[str, Any] = {
            "model": settings["model"], "store": False,
            "instructions": instructions, "input": inputs,
            "reasoning": reasoning,
            "max_output_tokens": output_budget or settings["max_output_tokens"],
        }
        if tools:
            payload["tools"] = tools
            payload["parallel_tool_calls"] = False
            payload["tool_choice"] = "none" if force_final else "auto"
        if schema:
            payload["text"] = {"format": {"type": "json_schema", "name": "thesis_response",
                                           "strict": True, "schema": schema}}
        response = self._request("POST", "/responses", payload)
        if response.get("status") == "incomplete":
            reason = response.get("incomplete_details", {}).get("reason", "output limit")
            raise APIError(f"OpenAI could not complete the answer ({reason}). Increase the output budget or "
                           "lower reasoning effort. Partial text was not applied to your document.")
        if response.get("status") in {"failed", "cancelled"}:
            raise APIError("OpenAI did not complete this request. Your document has not been changed.")
        return response


def response_text(response: dict) -> str:
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message" or item.get("phase") not in {None, "final_answer"}:
            continue
        for part in item.get("content", []):
            if part.get("type") == "refusal":
                raise APIError(part.get("refusal", "OpenAI declined this request."))
            if part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    return "\n".join(parts)


def replayable_output(response: dict) -> list[dict]:
    """Preserve reasoning and call IDs but strip response-only message properties."""
    output = []
    for item in response.get("output", []):
        item = json.loads(json.dumps(item))
        kind = item.get("type")
        if kind == "message":
            keep = {"type", "id", "role", "content", "phase"}
            item = {k: v for k, v in item.items() if k in keep}
            item["content"] = [{k: v for k, v in c.items() if k in {"type", "text", "annotations", "refusal"}}
                               for c in item.get("content", [])]
        elif kind == "reasoning":
            item = {k: v for k, v in item.items() if k in {"type", "id", "summary", "encrypted_content"}}
        elif kind == "function_call":
            item = {k: v for k, v in item.items() if k in {"type", "id", "call_id", "name", "arguments"}}
        else:
            continue
        output.append(item)
    return output
