# Architecture and maintenance

## Runtime

`start.bat` runs the project's Python virtual environment. `run.py` obtains an OS process lock, reserves a loopback socket, constructs the app, and starts Uvicorn on `127.0.0.1:8765`. It opens the default browser after a health response. The server is single-process: do not add multiple Uvicorn workers against the same document workspace.

`thesishelper/app.py` contains the HTTP API and serves bundled static assets. It rejects non-loopback Host headers, cross-origin/cross-site requests, and mutations without the local custom request header. This protects the local application from ordinary cross-site request and DNS-rebinding attacks. It is not authentication for other programs running as your Windows user.

## Modules

| File | Responsibility |
| --- | --- |
| `config.py` | Settings validation; secret-free public settings; session/environment key handling and Windows DPAPI persistence |
| `extraction.py` | PDF and DOCX parsing, source locations, bounded passage splitting, optional local OCR |
| `library.py` | SQLite schema, hashing, index maintenance, embedding checkpoints, lexical/semantic retrieval and fusion |
| `documents.py` | Editor schema, DOCX conversion, atomic saves, optimistic revisions, history and export |
| `gateway.py` | Fixed OpenAI API endpoints, timeouts, bounded retries, embeddings, Responses calls, replayable reasoning/call items |
| `assistant.py` | Evidence retrieval, bounded local tool loop, source scope, structured output, history and usage |
| `citations.py` | Trusted metadata formatting, source-ID resolution, normalized exact quote matching and library bibliography exports |
| `jobs.py` | Two background worker threads, progress snapshots, duplicate-task prevention and cancellation flags |
| `journal.py` | Local per-document chat history; SQLite connections closed after every operation |
| `frontend/src/` | Tiptap editor, document/library/settings views, evidence dialogs and suggestion controls |
| `thesishelper/static/` | Prebuilt frontend delivered to users; no CDN or runtime Node dependency |

## Persistence

The `data/` and `workspace/` directories are created on first use. Everything uses paths resolved from the project root, not the launching terminal's working directory. User data is ignored by Git.

The library database uses WAL and foreign keys. Source digests and version-aware passage IDs prevent an edited file from silently changing what an old passage ID means. Size/mtime checks exclude stale sources from search. Force indexing re-extracts; normal indexing preserves successful embedding batches. Metadata verification and page mapping are reset when source contents change.

Document saves use an expected revision and a process-local lock. A stale tab receives HTTP 409. A new JSON history snapshot is written before the authoritative document JSON is replaced atomically. A DOCX mirror failure does not discard the saved editor text. Revision-specific exports avoid the usual `current.docx` Word lock. These guarantees assume the supported single-process launcher; external modifications of JSON files are unsupported.

Chat messages and answer evidence snapshots persist locally. They contain no API key and no hidden reasoning items. Encrypted Responses reasoning items are replayed only in memory during the current task.

## Retrieval and bounded agent work

The default embedding configuration is `text-embedding-3-large`, 1,024 dimensions. Each passage is bounded by characters and UTF-8 bytes; batches contain up to 32 passages. Vectors are normalized and stored as float32 blobs. Query vectors are cached in memory with a bounded cache. SQL reads vectors in batches of 256 for cosine search, followed by reciprocal rank fusion with FTS results and source diversity selection.

This simple implementation is portable and memory bounded, but exhaustive vector scanning is linear in corpus size. For very large corpora, a future indexed-vector backend could replace it without changing evidence IDs or the API. Do not claim full-book scale benchmarks based on the included synthetic smoke check.

An assistant task performs initial retrieval, then permits only `search_library`, `read_passages`, and `list_library`. It allows at most four Responses calls, six model-requested tool calls, and 24 distinct evidence passages. The final call forces tool choice `none`. Reads are restricted to discovered passage IDs; source scope is rechecked when packing evidence. The model receives structured output instructions with a JSON schema. Unknown tool names are returned as errors without executing anything.

There are no browsing, file-writing, shell, or remote file-search tools. Model outputs cannot change a document directly. The client applies a suggested replacement only if the original document, content version, selected range, and selected text still match. Append is explicit. Saving again uses the server's revision check.

## Citation boundaries

The model emits local labels such as `[S1]`. The backend maps them to retrieved evidence, never accepting model-supplied bibliographic metadata. Suggested insertions receive deterministic author-date citations only when metadata and relevant locators are ready; otherwise they receive persistent source-ID review markers.

If a model-supplied quote cannot be found after NFC/whitespace/soft-hyphen normalization, it is not presented as a matching quotation and the suggestion is withheld. Unknown source IDs also withhold insertion. Matching text does not validate semantic entailment, scientific accuracy, or extraction fidelity. User-visible warnings and source links remain necessary.

The formatter is for ordinary book author-date citations. It is not a complete APA/Harvard implementation and does not handle every academic publication type. BibTeX/RIS exports use supplied fields, with missing metadata noted.

## Settings and costs

The default model ID is explicit and editable, never inferred by sorting an account's model list. The connection test validates authentication and model listing only. The first real query checks actual Responses capability and account usage. Unknown/custom models may reject reasoning or structured-output options; errors are shown without silent fallback.

No API request occurs on application startup or during local-only indexing. Semantic indexing requires a deliberate action with an API key. Requests use `store:false`, fixed HTTPS endpoints and no redirects. Keys never appear in public settings, validation-error bodies, ordinary JSON files, chat records or source prompts.

Output-token settings are per model call and include reasoning. Pro mode is opt-in. The task-level call cap bounds work but is not a currency budget. In-flight requests can be billed after cancellation. Rate-limit/temporary-server errors have bounded retries; a timed-out generation is not retried automatically.

## Validation and extension

Tests use temporary directories, generated PDFs/DOCX and fake API transports. Windows CI exercises DPAPI with synthetic plaintext. Add real-corpus evaluation separately: representative questions, expected sources, page accuracy, retrieval recall, unsupported-claim rates and revision safety. Do not use successful mock requests as evidence that a paid API account is configured or that answer quality is acceptable.
