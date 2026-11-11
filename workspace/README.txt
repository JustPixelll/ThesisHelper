ThesisHelper stores working documents in documents/<document-id>/.
document.json is the editor's authoritative document; current.docx is its Word mirror.
The application keeps revision history and original DOCX imports separately.
Edit in the browser and export to Word. To bring external Word edits back, import
the edited DOCX as a new document. Do not edit current.docx concurrently with the app.
Back up this entire workspace folder regularly. See README.md for details.
