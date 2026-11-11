"""Real-browser smoke gate with generated sources and a strictly fake OpenAI API.

Run explicitly: ``python tests/browser_smoke.py``. This is intentionally not a
pytest-discovered test; CI installs Chromium in its separate browser job.
No private documents, real API keys, external websites, or paid calls are used.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parents[1]
ARTIFACTS = REPO / "artifacts"
SOURCE_TEXT = "Employee participation can improve strategy decisions through shared knowledge."
DRAFT_TEXT = "Employee participation improves strategy decisions through shared knowledge."
PROPOSAL = "Employee participation may improve strategy decisions by enabling knowledge sharing."
ADDITION = " An additional limitation requires review."
TITLE = "Browser smoke: employee inclusion"


class FixtureGateway:
    """Return evidence grounded in the actual locally indexed fixture passage."""

    calls = 0
    embedding_tokens = 0

    def __init__(self, api_key: str, cancel: threading.Event | None = None, **kwargs):
        self.cancel = cancel

    def respond(self, settings: dict, instructions: str, inputs: list[dict], **kwargs) -> dict:
        if self.cancel and self.cancel.is_set():
            raise RuntimeError("Fixture request was cancelled.")
        request = json.loads(inputs[0]["content"])
        sources = request["initial_local_evidence"]
        evidence = next((source for source in sources if SOURCE_TEXT in source["text"]), None)
        if evidence is None:
            raise AssertionError("Browser smoke did not retrieve its actual indexed source.")
        type(self).calls += 1
        label = evidence["source_id"]
        result = {
            "answer": f"The local source supports a cautious formulation of this claim [{label}]. "
                      "This browser test uses a deterministic fixture response.",
            "suggestion": f"{PROPOSAL} [{label}]",
            "citations": [{"source_id": label, "quote": SOURCE_TEXT, "note": "Local fixture passage"}],
            "warnings": [],
        }
        return {
            "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 120, "output_tokens": 60,
                      "output_tokens_details": {"reasoning_tokens": 10}},
        }

    def embed(self, texts: list[str]):
        raise AssertionError("The browser smoke must never request remote embeddings.")


def plain_text(node: dict) -> str:
    return node.get("text", "") + "".join(plain_text(child) for child in node.get("content", []))


def fixture_app(root: Path):
    from docx import Document
    from thesishelper.app import create_app
    from thesishelper.gateway import OpenAIGateway

    # An accidental fallback is a test failure, even if CI has API credentials.
    os.environ.pop("OPENAI_API_KEY", None)

    def reject_real_api(*args, **kwargs):
        raise AssertionError("A real OpenAI request is forbidden in the browser smoke test.")

    OpenAIGateway._request = reject_real_api
    app = create_app(root)
    fixture = Document()
    fixture.core_properties.title = "Open Strategy Methods"
    fixture.core_properties.author = "Example, A."
    fixture.add_paragraph(SOURCE_TEXT)
    fixture.save(root / "ingest" / "open-strategy-fixture.docx")
    app.state.library.scan()
    sources = app.state.library.list_documents()
    assert len(sources) == 1 and sources[0]["chunks"] > 0, "Fixture indexing failed."
    app.state.library.update_metadata(sources[0]["id"], {
        "title": "Open Strategy Methods", "authors": "Example, A.", "year": "2024",
        "publisher": "Fixture University Press", "verified": True,
    })
    app.state.settings.update({"api_key": "synthetic-browser-test-key", "remember_key": False})
    app.state.assistant.gateway_factory = FixtureGateway
    return app


def start_server(app):
    import uvicorn

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(("127.0.0.1", 0))
    port = server_socket.getsockname()[1]
    server_socket.listen(128)
    server_socket.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                         log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(100):
        if not thread.is_alive():
            raise RuntimeError("The browser smoke's local server exited during startup.")
        try:
            with opener.open(base_url + "/api/health", timeout=0.3) as response:
                if json.load(response).get("ok"):
                    return server, thread, server_socket, base_url
        except OSError:
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=3)
    server_socket.close()
    raise RuntimeError("The browser smoke's local server did not become ready.")


def allow_loopback_only(context, base_url: str, blocked: list[str]) -> None:
    expected = urlsplit(base_url).netloc

    def route(request_route):
        url = urlsplit(request_route.request.url)
        if url.scheme in {"blob", "data"} or (url.scheme == "http" and url.netloc == expected):
            request_route.continue_()
        else:
            blocked.append(request_route.request.url)
            request_route.abort()

    context.route("**/*", route)


def assert_no_horizontal_overflow(page) -> None:
    sizes = page.evaluate("""() => ({
        viewport: window.innerWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth
    })""")
    assert sizes["document"] <= sizes["viewport"] + 1, sizes
    assert sizes["body"] <= sizes["viewport"] + 1, sizes


def exercise_browser(app, base_url: str) -> dict[str, Any]:
    from docx import Document
    from playwright.sync_api import expect, sync_playwright

    diagnostics: dict[str, Any] = {"checkpoints": [], "page_errors": [], "blocked_requests": []}
    page = None
    mobile = None
    context = None
    mobile_context = None
    with sync_playwright() as playwright:
        # CI installs this exact Playwright version's Chromium. Do not silently
        # substitute another browser or skip when Chromium is unavailable.
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            context.set_default_timeout(20000)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            allow_loopback_only(context, base_url, diagnostics["blocked_requests"])
            page = context.new_page()
            page.on("pageerror", lambda error: diagnostics["page_errors"].append(str(error)))
            page.goto(base_url, wait_until="networkidle")
            editor = page.locator('.ProseMirror[contenteditable="true"]')
            expect(editor).to_be_editable()
            expect(page.locator("#indexed-count")).to_have_text("1")
            diagnostics["checkpoints"].append("boot")

            page.locator("#document-title").fill(TITLE)
            editor.click()
            page.keyboard.insert_text(DRAFT_TEXT)
            expect(editor).to_have_text(DRAFT_TEXT)
            expect(page.locator("#save-status")).to_contain_text("Saved locally")
            draft_id = app.state.documents.list_documents()[0]["id"]
            stored = app.state.documents.get(draft_id)
            assert stored["title"] == TITLE and DRAFT_TEXT in plain_text(stored["content"])
            diagnostics["checkpoints"].append("edit_and_autosave")

            # Real browser keyboard selection exercises ProseMirror and the DOM
            # selection event instead of injecting an application-state snapshot.
            editor.click()
            editor.press("Control+A")
            assert page.evaluate("window.getSelection().toString()") == DRAFT_TEXT
            expect(page.locator("#selection-box")).to_be_visible()
            expect(page.locator("#selection-preview")).to_have_text(DRAFT_TEXT)
            page.locator('[data-action="rewrite"]').click()
            expect(page.locator(".suggestion-card")).to_have_count(1)
            suggestion = page.locator(".suggestion-card").last
            expect(suggestion.locator(".suggestion-text")).to_contain_text(PROPOSAL)
            evidence = page.locator("#chat-feed .evidence-card").last
            expect(evidence).to_contain_text(SOURCE_TEXT)
            expect(evidence.locator(".quote-status")).to_contain_text("found in the extracted source")
            replacement = suggestion.get_by_role("button", name="Replace selection", exact=True)
            expect(replacement).to_be_enabled()
            replacement.click()
            expect(editor).to_contain_text(PROPOSAL)
            expect(editor).to_contain_text("Example, 2024")
            expect(page.locator("#save-status")).to_contain_text("Saved locally")
            diagnostics["checkpoints"].append("selection_rewrite_evidence_apply")

            # Generate a second suggestion, change the writing, then verify that
            # the stale proposal cannot overwrite the changed selection.
            expect(page.locator('[data-action="rewrite"]')).to_be_enabled()
            editor.click()
            editor.press("Control+A")
            expect(page.locator("#selection-box")).to_be_visible()
            page.locator('[data-action="rewrite"]').click()
            expect(page.locator(".suggestion-card")).to_have_count(2)
            second = page.locator(".suggestion-card").last
            second_replacement = second.get_by_role("button", name="Replace selection", exact=True)
            expect(second_replacement).to_be_enabled()
            editor.click()
            editor.press("Control+End")
            page.keyboard.insert_text(ADDITION)
            expect(editor).to_contain_text(ADDITION.strip())
            expect(second_replacement).to_be_disabled()
            expect(second.locator(".stale-note")).to_be_visible()
            expect(page.locator("#save-status")).to_contain_text("Saved locally")
            diagnostics["checkpoints"].append("stale_selection_guard")

            page.locator("#open-library").click()
            expect(page.locator("#library-dialog")).to_be_visible()
            page.locator("#library-search").fill("employee participation")
            page.locator("#library-search-button").click()
            search_evidence = page.locator("#library-results .evidence-card").first
            expect(search_evidence).to_contain_text(SOURCE_TEXT)
            search_evidence.get_by_role("button", name="Read context", exact=True).click()
            expect(page.locator("#evidence-dialog")).to_be_visible()
            expect(page.locator("#evidence-content")).to_contain_text(SOURCE_TEXT)
            page.get_by_role("button", name="Close Source passage", exact=True).click()
            page.screenshot(path=str(ARTIFACTS / "desktop-library.png"), full_page=True)
            page.get_by_role("button", name="Close Source library", exact=True).click()
            diagnostics["checkpoints"].append("library_search_and_original_context")

            with page.expect_download() as pending_download:
                page.locator("#export-document").click()
            download = pending_download.value
            assert download.suggested_filename.endswith(".docx")
            export_path = ARTIFACTS / "browser-smoke-export.docx"
            download.save_as(export_path)
            exported = Document(export_path)
            export_text = "\n".join(paragraph.text for paragraph in exported.paragraphs)
            assert PROPOSAL in export_text and ADDITION.strip() in export_text
            assert "Example, 2024" in export_text
            diagnostics["checkpoints"].append("word_download_content")

            page.reload(wait_until="networkidle")
            expect(page.locator("#document-title")).to_have_value(TITLE)
            editor = page.locator('.ProseMirror[contenteditable="true"]')
            expect(editor).to_contain_text(PROPOSAL)
            expect(editor).to_contain_text(ADDITION.strip())
            expect(page.locator("#chat-feed .suggestion-card")).to_have_count(2)
            assert_no_horizontal_overflow(page)
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(ARTIFACTS / "desktop-writing.png"), full_page=True)
            diagnostics["checkpoints"].append("reload_persistence")

            mobile_context = browser.new_context(viewport={"width": 390, "height": 844},
                                                 is_mobile=True, has_touch=True, device_scale_factor=1)
            mobile_context.set_default_timeout(20000)
            allow_loopback_only(mobile_context, base_url, diagnostics["blocked_requests"])
            mobile = mobile_context.new_page()
            mobile.on("pageerror", lambda error: diagnostics["page_errors"].append(str(error)))
            mobile.goto(base_url, wait_until="networkidle")
            expect(mobile.locator('.ProseMirror[contenteditable="true"]')).to_contain_text(PROPOSAL)
            expect(mobile.locator("#mobile-library")).to_be_visible()
            mobile.evaluate("document.fonts.ready")
            assert_no_horizontal_overflow(mobile)
            mobile.screenshot(path=str(ARTIFACTS / "mobile-writing.png"), full_page=True)
            mobile.locator("#mobile-library").click()
            expect(mobile.locator("#library-dialog")).to_be_visible()
            assert_no_horizontal_overflow(mobile)
            mobile.screenshot(path=str(ARTIFACTS / "mobile-library.png"), full_page=True)
            diagnostics["checkpoints"].append("mobile_layout_and_library")
            assert FixtureGateway.calls == 2, f"Expected exactly 2 fake model calls; got {FixtureGateway.calls}"
            assert not diagnostics["page_errors"], diagnostics["page_errors"]
            assert not diagnostics["blocked_requests"], diagnostics["blocked_requests"]
            diagnostics["status"] = "passed"
            return diagnostics
        except Exception as error:
            diagnostics["status"] = "failed"
            diagnostics["error"] = f"{type(error).__name__}: {error}"
            for label, candidate in (("desktop", page), ("mobile", mobile)):
                if candidate and not candidate.is_closed():
                    try:
                        candidate.screenshot(path=str(ARTIFACTS / f"{label}-failure.png"), full_page=True)
                    except Exception:
                        pass
            raise
        finally:
            (ARTIFACTS / "browser-smoke-report.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
            if context:
                context.tracing.stop(path=str(ARTIFACTS / "browser-trace.zip"))
                context.close()
            if mobile_context:
                mobile_context.close()
            browser.close()


def main() -> None:
    sys.path.insert(0, str(REPO))
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="thesishelper-browser-") as temporary:
        app = fixture_app(Path(temporary))
        server, thread, server_socket, base_url = start_server(app)
        try:
            result = exercise_browser(app, base_url)
            print(json.dumps(result, indent=2))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            server_socket.close()
            if thread.is_alive():
                raise RuntimeError("The browser smoke server did not stop cleanly.")


if __name__ == "__main__":
    main()
