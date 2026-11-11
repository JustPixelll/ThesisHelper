# Windows installation and daily use

## 1. Download the app

Open [JustPixelll/ThesisHelper](https://github.com/JustPixelll/ThesisHelper), select **Code → Download ZIP**, and extract the whole ZIP to a writable local folder, for example:

```text
C:\Users\YourName\Documents\ThesisHelper
```

Do not launch it inside the ZIP preview. Avoid `Program Files` or another location that needs administrator access. Spaces and Unicode characters in the project path are supported. Keep the whole project together.

## 2. Install Python once

Install **Python 3.12, 64 bit** from the [official Windows downloads](https://www.python.org/downloads/windows/). In the installer, enable **Add python.exe to PATH** and the Python launcher. Python 3.11 and 3.13 are also accepted. Python 3.12 is the recommended baseline.

No GPU, Docker, Node.js, Microsoft Word installation, or administrator account is needed to run ThesisHelper. Microsoft Word is optional for opening or finishing exported `.docx` documents.

## 3. Install ThesisHelper

Double-click **install.bat**. It creates a private `.venv` environment and downloads the Python dependencies. The console stays open if an error occurs. The first installation needs an internet connection.

If Windows displays a downloaded-file warning, check that the extracted files came from your repository before running them. The scripts do not require disabling Windows protection or running as administrator.

When you see **Installation complete**, close that installer window.

## 4. Start the app

Double-click **start.bat**. Your default browser opens:

```text
http://127.0.0.1:8765
```

Keep the console window open while using the app. Press **Ctrl+C** in that window when you are finished. Closing the browser alone does not stop the server. The server accepts connections only from your own computer.

If the browser does not open automatically, enter the address above manually. If port 8765 is already in use, open a terminal in the project folder and run:

```bat
start.bat --port 8766
```

Alternatively set the `THESISHELPER_PORT` environment variable. Do not run two servers against the same project folder; the launcher prevents this to protect your working documents.

## 5. Add your API key and books

In **Settings**, enter your OpenAI API key and use the connection test. You need access to the selected API model and an API account with available usage. A ChatGPT website login is not an API key. See [OpenAI API keys](https://platform.openai.com/api-keys) and your API project's usage controls.

Copy PDFs and DOCX source files into **ingest/**. You can create subfolders for topics or use the upload button in the app. Start indexing from the Library panel. Keep your thesis drafts separate from the source-book folder unless you deliberately want a draft to become research evidence.

Local text indexing and keyword search run without model calls. Semantic indexing sends extracted source passages to the embedding API; assistant queries send your question, selected writing/context, and retrieved passages to the model API. These are separate paid API uses. The app does not browse or search the web.

Start with a few representative books. Check extraction quality, bibliographic metadata, and page references before indexing the rest. A scanned PDF needs a text layer or optional local OCR; a scan cannot be treated as searchable text merely because the file opens successfully.

## 6. Write and work with Word files

Create a new working document in the app, or import a `.docx`. Select text in the editor and choose an assistant action. Review the proposed change and its evidence before applying it. The editor autosaves after changes; wait for its saved status before closing the app.

**The browser editor is the live working document.** A normal `.docx` mirror is written at:

```text
workspace\documents\<document-id>\current.docx
```

Use **Export Word** for a download of the latest saved revision. You can open that file in Word for final layout. If Word has `current.docx` locked, ThesisHelper still saves your editor text and shows a warning; the separate export remains available. Close the mirror in Word and save/export again to refresh it.

Changes made directly to a Word file are not live-synchronized into the browser editor. To continue from external Word changes, import the edited `.docx` as a new working document. The original imported file is copied to `original.docx` next to the working document and is not overwritten.

Supported interchange includes body paragraphs, headings, bold/italic/underline/strike, superscript/subscript, hyperlinks, lists, block quotes, and ordinary tables. Word page layout, headers and footers, footnotes/endnotes, tracked changes, images, comments, bibliography/citation field behavior, and advanced formatting are not preserved as editable features. Import warnings identify detected special content. Keep your original university template for the final Word formatting stage.

The **History** view keeps saved revisions. Restoring an earlier revision creates a new current revision; it does not delete later snapshots. If two browser tabs edit the same document, a stale save is rejected instead of silently overwriting the newer text. Copy unsaved text before reloading after a conflict.

## Backups and updates

With the app stopped, back up the project folders below to another location:

| Folder | What it contains |
| --- | --- |
| `ingest/` | Original source books and papers |
| `workspace/` | Working documents, original imports, Word mirrors, exports, and revision history |
| `data/` | Local search database, app settings, and other application state |

Do not upload these private folders or API keys to GitHub. The repository's ignore rules exclude application data. On Windows, a remembered API key is encrypted with Windows DPAPI in `data/key.dpapi` and is tied to the current Windows account. It may need to be entered again after moving to another account or machine. Without protected persistence, the app keeps a key only in process memory for the current session. It can also read an `OPENAI_API_KEY` environment variable; manage or remove that variable in Windows if you want to change that source of credentials.

To update, stop the app, make a backup, download the updated code, and copy code changes into your existing folder while retaining `ingest/`, `workspace/`, and `data/`. Run `install.bat` again if dependencies changed. If you move the project folder, delete only `.venv` and rerun the installer because Python virtual environments are not portable.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| “A supported 64-bit Python installation was not found” | Install Python 3.12 64 bit with the launcher and PATH options; reopen the installer. |
| Dependency download fails | Read the console error, check internet/proxy access and free space, then rerun `install.bat`. |
| The app starts then reports missing packages | Rerun `install.bat`; do not manually launch it with a different Python installation. |
| Port is unavailable | Stop the previous server or use `start.bat --port 8766`. |
| “Already running for this project folder” | Use the existing tab or stop the older console with Ctrl+C. After a crash, Windows releases the process lock automatically. |
| API authentication/model error | Re-enter the API key, confirm API project billing/access, and select a model available to that project. |
| Some PDF pages produce no text | Use a searchable PDF or the optional local OCR workflow; inspect Library warnings. |
| Word mirror cannot update | Close that file in Word. Your editor text remains saved; use Export Word to get the current revision. |
| Save conflict | Copy your unsaved passage, reload the current document, and reapply the intended change. |
| Browser cannot connect | Check that `start.bat` is still running and that you are using its printed loopback address and port. |

## Developer commands

The installation scripts are optional. From the project folder:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python run.py
```

Run the offline regression suite with:

```bat
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

These tests use generated sample documents and mocked API calls; they do not need your private books or API key. The repository's CI matrix also runs tests on Windows and Linux.
