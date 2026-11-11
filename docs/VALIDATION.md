# Validation record

Release development date: **11 November 2026**.

## Automated checks

The repository includes offline tests for:

- PDF physical/printed page distinctions and DOCX paragraph/table locators.
- Incremental indexing, missing/replaced source exclusion, versioned evidence, semantic fallback, query safety, cancellation and embedding resumption.
- DOCX structure, inline formatting, numbering, tables, Unicode, original-import preservation and Word file-lock behavior.
- Atomic document revisions, conflicts between concurrent saves, history and restoration.
- Key redaction, protected/session persistence, same-origin API boundaries, safe upload naming, malformed input and HTTP integration.
- OpenAI payload shape, structured output/refusal/incomplete handling, encrypted-reasoning replay, tool-call bounds and unauthorized-tool rejection.
- Source-scope enforcement, quote checks, unknown-reference handling and suppressed unsafe suggestions.

Run `python -m pytest -q` to obtain the current count. The suite uses no real API key, private books or paid requests. Native DPAPI tests run on Windows and are skipped on other systems. GitHub Actions is configured for Windows and Linux with Python 3.11, 3.12 and 3.13.

## Scale smoke check

The local library was exercised with 500 short synthetic files (250 PDFs, 250 DOCX), 750 passages and fake 1,024-dimensional vectors. An unchanged rescan did not re-embed content. This is a file-count/persistence smoke check, not a test of 500 full books, real embedding throughput or research recall. See [LIBRARY.md](LIBRARY.md) for observed times and conditions.

## API and corpus acceptance on your PC

After installation, use a small representative source set and your API key:

1. Confirm Settings accepts your key and lists your chosen model.
2. Index local text; inspect known passages and PDF page locations. Try one scanned source separately if using OCR.
3. Add verified author/title/year metadata and a checked page offset where applicable.
4. Enable semantic indexing and confirm missing vectors complete without resetting earlier progress.
5. Ask one question with known support, one that needs two sources, and one the library cannot answer. Inspect the evidence rather than the wording alone.
6. Select a paragraph, request a rewrite, review its sources, and apply it. Then edit the draft while another request is running and verify stale replacement is refused.
7. Export Word and inspect the resulting document in your own Word version and final formatting workflow.

Live paid OpenAI requests and quality against the user's actual books were not performed during development. A model-list connection test does not prove generation access, remaining quota or answer accuracy. OCR availability and fidelity depend on the locally installed Tesseract executable and language data.
