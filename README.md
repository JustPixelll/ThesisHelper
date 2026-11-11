# ThesisHelper

**A local thesis workspace for Windows: your books, your writing, and an assistant that works from your sources.**

Put PDF and DOCX books in `ingest/`, index them, and write your bachelor or master thesis in the built-in editor. Select a passage to find evidence, improve academic wording, critique an argument, check citations, or summarize. Review the answer and source excerpts before applying a suggestion. Export the working document to Word whenever you need it.

The default model is **GPT-6 Astra**, identified as OpenAI's most capable model in the [official API catalog](https://developers.openai.com/api/docs/models/gpt-6-astra) when this release was built on **11 November 2026**. Settings offer other models, a custom model ID, reasoning effort, and optional Pro reasoning mode. Availability depends on your OpenAI API project. The app never silently switches to a different model.

## Get running on Windows

1. Open [this repository](https://github.com/JustPixelll/ThesisHelper), select **Code → Download ZIP**, and **extract the entire ZIP** to a writable folder such as `Documents\ThesisHelper`.
2. Install **64-bit Python 3.12** from [python.org](https://www.python.org/downloads/windows/), enabling the Python launcher and **Add python.exe to PATH**. Python 3.11 and 3.13 are also supported.
3. Double-click **`install.bat`** and wait for **Installation complete**.
4. Double-click **`start.bat`**. The app opens at **http://127.0.0.1:8765**. Keep its console window open.
5. Open **Settings**, enter your [OpenAI API key](https://platform.openai.com/api-keys), save it, and test the connection. Choose English or German output as appropriate.
6. Copy a few source PDFs or DOCX files into **`ingest/`**. Subfolders work. Open **Library → Index local text**. Check the extraction warnings and metadata.
7. Optionally start **semantic indexing** for conceptual and cross-language retrieval. This sends extracted passages to the OpenAI embeddings API and uses your API account.
8. Create a thesis or **Import Word**, write in the editor, select a passage, and choose an assistant action. Use **Export Word** to download the saved document.

**Full installation, troubleshooting, backups, and Word workflow:** [docs/WINDOWS.md](docs/WINDOWS.md).

No Docker, Node.js, GPU, hosted database, or Microsoft Word installation is required to run the downloaded app. The frontend is already built and included. An internet connection is needed for installation and OpenAI API requests. Editing and keyword search work locally without an API key or internet access after installation.

## What is included

| Area | Functionality |
| --- | --- |
| Source library | Recursive PDF/DOCX ingestion, uploads, incremental rescans, change detection, per-file errors, optional local OCR, resumable semantic indexing |
| Retrieval | SQLite full-text search plus OpenAI embeddings stored locally; combined rankings and adjacent-passage reading |
| Research assistant | A bounded multi-step agent that searches the library, reads surrounding passages, and drafts an evidence-based answer |
| Selected-text actions | Find evidence, academic rewrite, critique, citation audit, and summary |
| Evidence | Clickable source file/page, extracted text, source-version ID, quote matching, and visible metadata/page uncertainty |
| Thesis editor | Headings, emphasis, lists, quotes, links, superscripts/subscripts, tables, undo, word count, autosave, and browser recovery copy |
| Word documents | DOCX import, a working `.docx` mirror, export, preserved original imports, and revision history |
| Citation support | Editable and verified book metadata, printed-page mapping, author-date citation templates, BibTeX/RIS library export |
| Control | Model/effort selection, optional Pro mode, output budget, English/German output, source scope, job progress, cancellation, and usage counts |
| Data protection | Loopback binding, same-origin request checks, local storage, no CDN assets, and optional Windows-protected key persistence |

## How the assistant uses the books

ThesisHelper builds a searchable index; it does **not** train a model or place all 500 books into every prompt.

1. Extract text while retaining physical PDF pages or DOCX paragraph/table locations.
2. Save passages and metadata in a local SQLite database.
3. If enabled, generate `text-embedding-3-large` vectors with 1,024 dimensions and store those locally too.
4. Retrieve relevant passages for the question and selected text.
5. Let the model request additional local searches or neighboring passages within a bounded budget.
6. Return an answer, an optional suggested edit, source cards, and warnings. Only an explicit editor action applies a suggestion.

The model has **no web-search tool, shell, file-writing tool, or external document retrieval**. Academic facts are instructed to come from the supplied evidence; general writing or structural advice is distinguished from source verification. The app rejects invented source IDs and withholds suggestions when a supplied quotation fails its exact-text check. These checks improve traceability; they do not prove that every interpretation is correct or that retrieval found every relevant passage.

There is no artificial 500-book cap. Index size and speed depend on page counts, extraction quality, OCR, and available API throughput. A normal PC can run the app; an SSD and enough space for the original books plus the index are useful. Search uses bounded vector batches rather than loading the entire vector collection into RAM. Extraction uses memory for one source at a time. Very large or malformed PDFs may need splitting.

**The 500-file smoke test used short synthetic documents, not 500 complete books.** See [the library guide](docs/LIBRARY.md) for the benchmark, scale limits, extraction behavior, and retrieval details.

## Writing in Word

**The browser editor is the live workspace.** This release is not a Microsoft Word add-in and does not monitor your selection inside desktop Word.

Every working document has:

```text
workspace/documents/<document-id>/
  document.json       Authoritative editor document
  current.docx        Word mirror of the saved document
  original.docx       Preserved original, if imported
  history/            Saved revision snapshots
  exports/            Revision-specific Word exports
```

Use the browser for selected-text assistance and Word for final university-template formatting. If you edit an exported file in Word, import it as a new working document to continue in ThesisHelper. Do not edit `current.docx` and expect automatic bidirectional synchronization.

Import/export preserves the supported body text, headings, inline styles, ordinary hyperlinks, lists, and tables. Advanced Word layout and features such as footnotes/endnotes, comments, tracked changes, images, citation-manager fields, and automatic bibliographies do not round-trip as live features. The app preserves the original import and shows warnings. Keep your original university template and original Word files.

## Citations: what gets checked

Source cards always provide reproducible locations. **PDF page 13 does not automatically mean printed book page 13.** Enter a page offset only after checking the source: if physical PDF page 13 contains printed page 1, the offset is `-12`. One offset is suitable only for a continuous page sequence; front matter, inserted pages, and compilations require care. DOCX sources use paragraph/table locations because Word pagination depends on layout.

Complete each source's title, authors, year, publisher, edition and DOI as applicable. Enter authors as `Family, Initials; Family, Initials` or use the full institutional author. Mark metadata verified after checking it against the book. Imported file properties are not automatically trusted. Editing metadata or replacing a source clears verification unless you deliberately re-verify it.

The app checks that cited passage IDs were actually retrieved and that supplied quotations occur in the extracted text after whitespace normalization. It distinguishes a **matching quote** from a **supported claim**; the latter still requires careful reading. Secondary citations must be identified as such. For final direct quotations, check the page image, especially after OCR.

APA/Harvard options are transparent **book author-date templates**, not a complete CSL citation processor for every publication type and university variant. Unverified details remain explicit review markers rather than invented references. BibTeX/RIS exports contain the metadata you supplied and notes for gaps; review them in your reference manager.

## API usage and privacy

| Operation | Network/data flow |
| --- | --- |
| Edit, save, import/export Word | Local files and browser only |
| Extract books and keyword search | Local only |
| Semantic indexing | Extracted source passages go to OpenAI's embeddings endpoint; vectors are stored locally |
| Semantic search | Search query goes to the embeddings endpoint; similarity search happens locally |
| Assistant query | Question, selected text/draft context, bounded recent chat, and retrieved passages go to OpenAI's Responses endpoint |
| Connection test | Lists models available to your API key; does not generate an answer or guarantee remaining billing quota |

The API key stays on the local Python backend. On Windows you can remember it using **DPAPI encryption tied to your Windows account**, saved as `data/key.dpapi`. Otherwise it stays in process memory. `OPENAI_API_KEY` is also supported as an environment variable. Keys are not returned to the browser, written into ordinary settings JSON, or included in Git.

Model requests use `store: false`. This disables Responses application-state storage; it does **not** mean OpenAI receives no data or that your account has Zero Data Retention. Your API account's applicable data controls and policies still govern processing. Review [OpenAI's data controls](https://developers.openai.com/api/docs/guides/your-data).

API model and embedding usage is separate from using the ChatGPT chat interface. Check your API account's credits, access and limits, and [current API pricing](https://developers.openai.com/api/docs/pricing). Strong reasoning and Pro mode can use substantially more tokens and take longer. Successful answers display aggregate usage. An assistant task can make up to four model calls, each subject to your output-token setting, with at most six additional tool calls and 24 evidence passages. The output budget includes reasoning tokens.

Cancellation stops subsequent work and is checked between operations. A request already being processed by OpenAI may still finish and be billed. Timed-out generation requests are not automatically replayed. Rate-limit/temporary-server errors receive bounded retries. No model fallback is performed without your selection.

## Optional local OCR

For scanned PDFs, install Tesseract and the optional Python dependencies:

```bat
.venv\Scripts\python -m pip install -r requirements-ocr.txt
```

Install the local Tesseract executable using the [Tesseract project's installation guidance](https://tesseract-ocr.github.io/tessdoc/Installation.html). Install the language data you need. Enable OCR when indexing.

If automatic detection does not find your installation, set the following in the terminal before starting:

```bat
set "THESISHELPER_TESSERACT=C:\Program Files\Tesseract-OCR\tesseract.exe"
set "THESISHELPER_OCR_LANG=deu+eng"
start.bat
```

OCR is local and opt-in; missing OCR dependencies and unreadable pages produce visible warnings. Review extracted quotations against the actual page images.

## Backups

Stop the app and copy **`ingest/`, `workspace/`, and `data/`** to your backup destination. Revision history protects against editing mistakes, not disk failure. Your DPAPI-encrypted key may need re-entering on another Windows account or computer.

Books, drafts, keys, indexes, and browser-test fixtures are excluded from version control. Update code while preserving your data folders. If you move the project folder, recreate `.venv` by running the installer again.

## Development

The Windows scripts are the normal user workflow. On Linux/macOS or for development:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python run.py --no-browser
python -m pytest -q
python -m ruff check thesishelper tests run.py
```

The checked-in frontend requires no build for users. To change it:

```bash
cd frontend
npm ci
npm run check
npm run build
```

Commit both `frontend/src/` and the updated `thesishelper/static/` bundle. See [architecture](docs/ARCHITECTURE.md), [validation notes](docs/VALIDATION.md), and [third-party notices](THIRD_PARTY_NOTICES.md).

## Current practical limits

- Full text retrieval is not exhaustive understanding of every page. Review evidence, especially for literature coverage and absence claims.
- Complex PDF tables, mathematics, reading order and OCR can be imperfect. Images are not interpreted by the text index.
- Word interchange is intentionally limited to the supported editor features; preserve original files and final templates.
- Citation templates and supplied-quote checks assist review; they are not a guarantee of academic correctness.
- This is a single-user app for your own PC. It is not designed for LAN exposure or multiuser hosting.
- Live OpenAI access, output quality, account billing and actual corpus recall must be checked with your API key and books. Automated tests use fake responses and generated documents.

## Official API references used

- [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [Responses function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Reasoning modes and effort](https://developers.openai.com/api/docs/guides/reasoning)
- [Embeddings](https://developers.openai.com/api/docs/guides/embeddings)

The application uses those API capabilities through a small HTTPS client. It does not depend on a separate agent framework or a cloud vector store.
