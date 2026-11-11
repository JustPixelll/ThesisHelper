# How the local book library works

ThesisHelper indexes PDF and DOCX files recursively from `ingest/`. You can keep subject folders inside that directory. Temporary Word files whose names start with `~$` are ignored. Files outside `ingest/`, including links pointing outside it, are not indexed.

## Indexing and changes

The local SQLite database contains document metadata, extracted passages, a full-text search index, and optional embedding vectors. An ordinary scan skips extraction for unchanged files. When a file's size or modification timestamp changes, ThesisHelper calculates its SHA-256 digest. A force scan also catches replacements that deliberately preserve their original size and timestamp.

Each path has a stable document ID. Each passage ID incorporates its source digest and extracted text. Replacing a book cannot silently make an old passage ID point at different text. Before serving search results, the library checks source file size and modification time. A modified source is marked **stale** and a removed source is marked **missing**; neither supplies current evidence until rescanned. A changed source clears the metadata verification flag and the old page offset so its bibliography and pagination can be checked again.

A failed file does not stop the other files. The document list reports errors and extraction warnings. A stopped indexing run can be resumed with another scan. Semantic vectors are saved after each successful batch of up to 32 passages, so a transient API failure does not require embedding the entire book again. If embeddings fail, the extracted passages remain available to keyword search. Permanent key, model-access, or billing failures stop the indexing job rather than sending the same doomed request for every remaining book. Correct the API settings/account and scan again to resume saved progress.

To stop retrieving a source in the app, move it out of `ingest/` and rescan. Its record becomes **missing** and no longer supplies evidence. Restore the file and rescan to make it available again.

## Sources and locations

### PDF

Passages do not cross physical PDF page boundaries. The first physical page is `PDF page 1`.

Three different things may appear on an evidence card:

- **PDF page:** the physical, one-based position in the file. This always remains available.
- **PDF label:** a label embedded by the PDF's producer, if the PDF explicitly defines one. A label is not automatically treated as a checked printed page number.
- **Printed page:** available only after you enter a page offset in source metadata. The rule is `printed page = physical PDF page + offset`. For example, if PDF page 13 displays printed page 1, use `-12`. Pages that would map to zero or a negative number remain without a printed-page citation (typically front matter).

A single offset is appropriate only for a continuous page-number sequence. Front matter with Roman numerals, inserted pages, chapter-specific numbering, and compiled articles may need separate handling. Check the original page before using a final citation. The app identifies an entered offset as **user-mapped**, rather than claiming it independently verified the pagination.

PDF extraction is textual. Tables, equations, columns, and unusual fonts can have imperfect reading order. The warning on each source makes this limitation visible.

### DOCX

DOCX files do not contain a reliable fixed page layout. The library cites main-body paragraph numbers and table/row numbers, never invented Word page numbers. Adjacent short paragraphs may be combined into one passage with a paragraph range.

The main body and its top-level tables are indexed. Headers, footers, text boxes, embedded image text, equations, footnotes, and endnotes are not part of this DOCX extractor. Sources with detected footnotes, endnotes, or embedded images show additional warnings. To include their full context, export that source to a searchable PDF and index the PDF.

## Bibliographic metadata

The filename supplies the initial title unless the source includes a title in its document properties. Author properties can also be imported. These properties are **unverified**, and they often describe a scanner, editor, or software account rather than the book's author.

Publication years, publishers, editions, and DOI values are never inferred from file creation dates. Complete and check these fields in the Library interface. Mark metadata verified only after checking it against the source. Bibliographic fields remain editable across subsequent scans. Changing a bibliographic field without explicitly re-verifying it clears the verification flag; a combined edit-and-verify operation is allowed after checking the source.

A citation key is an identifier, not an academic citation. The default `source_<id>` key does not claim to know the author or year.

## Search

Keyword retrieval uses SQLite FTS5 with Unicode tokenization. Search input is tokenized and parameterized; it is not executed as raw SQL or raw FTS query syntax. Titles and authors are searchable as well as passage text.

When semantic vectors exist for the selected embedding model and dimension, the library also embeds your query and computes cosine similarity against the stored vectors. It combines keyword and semantic rankings with reciprocal rank fusion, then favors a spread of books among the top results. A query-embedding failure falls back to keyword results with a visible retrieval warning. Results remain candidate evidence: their ranking is not proof that they support a particular claim.

Embedding inputs are bounded to 3,000 characters and 7,500 UTF-8 bytes each. Up to 32 passages are embedded per request, providing a conservative token bound even for unusual Unicode. The implementation reads vectors in batches of 256. It does not keep every vector in memory at once. Search time still scales with the number and size of vectors. There is no artificial 500-book limit, but 500 long books can require substantial disk space and indexing time. Extraction uses memory for one source at a time; large or malformed PDFs may need to be split. No performance claim is based on a 500-book benchmark unless separately documented.

Different embedding models/dimensions are stored separately and never mixed in the same similarity calculation. Changing either requires indexing missing vectors for that configuration. Already computed vectors for the old configuration remain available if you switch back.

## Optional local OCR

OCR applies to PDF pages with little or no extractable text. Once OCR text has been saved for an unchanged file, normal scans preserve it even when the OCR checkbox is unchecked, including when adding semantic vectors. An explicit force rescan can discard that text if OCR is disabled. If OCR was unavailable or failed, enabling it on a later scan retries extraction after the dependency or language setup is repaired. It runs locally with Tesseract, `pypdfium2`, `pytesseract`, and Pillow. The Python extras and the Tesseract executable are separate installations; use the main installation guide for setup. Without these components, readable PDF text still gets indexed and scanned pages receive a warning.

You can specify:

```text
THESISHELPER_TESSERACT=C:\Program Files\Tesseract-OCR\tesseract.exe
THESISHELPER_OCR_LANG=eng
```

For German and English OCR, install both corresponding Tesseract language data files, then use `deu+eng`. If OCR fails or pages remain unreadable, that is reported per document. OCR-derived quotations require a check against the actual page image.

## Network and persistence

Extraction and keyword search run locally. This subsystem has no web-search, website-fetching, or remote document-hosting tool. When you enable semantic indexing, passage text is passed to the configured embedding provider. Semantic search passes query text to that provider. The assistant separately sends the selected thesis context and retrieved passages to the selected OpenAI model. See the main README for the full data flow and API setup.

Keep your `ingest/` folder and local application data together when backing up or moving the project. Passage locations refer to the original files and their recorded versions.

## Development scale smoke check (2026-11-11)

A Linux/Python 3.12 smoke check indexed **500 synthetic small files**: 250 two-page PDFs and 250 short DOCX files, producing 750 passages. Embeddings were deterministic, fake 1,024-dimensional vectors; no OpenAI request was made. This checks file counts, persistence, repeated scans, and the retrieval path. It does not establish performance or recall for 500 full books, OCR-heavy sources, or a live API account.

| Operation | Observed time |
| --- | ---: |
| Initial extraction, local fake-vector storage, and indexing | 11.344 seconds |
| Unchanged rescan | 1.218 seconds |
| Keyword search, median of five runs | 0.0336 seconds |
| Hybrid search, median of five runs | 0.0441 seconds |

The unchanged rescan reported all 500 files unchanged and generated zero new embeddings. The database occupied 7.17 MiB. A separate interruption test verified that successful 32-passage embedding batches persist and are not repeated when a later batch fails.
