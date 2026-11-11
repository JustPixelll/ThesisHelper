"""Deterministic book citations. The model never supplies bibliographic metadata."""
from __future__ import annotations

import re
import unicodedata


def _authors(source: dict) -> list[str]:
    return [name.strip() for name in str(source.get("authors") or "").split(";") if name.strip()]


def citation_details(source: dict, style: str = "apa") -> dict:
    names = _authors(source)
    surnames = [name.split(",", 1)[0].strip() for name in names]
    year = str(source.get("year") or "").strip()
    title = str(source.get("title") or "").strip()
    issues = []
    if not source.get("metadata_verified", source.get("verified", False)):
        issues.append("Bibliographic metadata has not been marked as verified.")
    if not names:
        issues.append("Author or institutional author is missing.")
    if not year:
        issues.append("Publication year is missing (use n.d. only if genuinely undated).")
    if not title:
        issues.append("Publication title is missing.")
    if len(surnames) >= 3:
        author = surnames[0] + " et al."
    elif len(surnames) == 2:
        author = (" & " if style == "apa" else " and ").join(surnames)
    else:
        author = surnames[0] if surnames else "[author missing]"
    printed = source.get("printed_page")
    location = ""
    if source.get("page") is not None:
        if printed is None or str(printed) == "":
            issues.append("Printed book page is unverified; PDF file page is only a navigation locator.")
        else:
            location = f", p. {printed}"
    elif source.get("paragraph_start"):
        location = f", para. {source['paragraph_start']}"
        end = source.get("paragraph_end")
        if end and end != source["paragraph_start"]:
            location = f", paras. {source['paragraph_start']}–{end}"
    elif source.get("location"):
        # A table/cell locator is legitimate; do not invent Word page numbers.
        location = ", " + str(source["location"])
    formatted = f"({author}, {year or '[year missing]'}{location})" if style == "apa" else \
        f"({author} {year or '[year missing]'}{location})"
    author_list = "; ".join(names) if names else "[author missing]"
    publisher = str(source.get("publisher") or "").strip()
    edition = str(source.get("edition") or "").strip()
    entry = f"{author_list} ({year or '[year missing]'}). {title or '[title missing]'}"
    if edition:
        entry += f" ({edition})"
    entry += "."
    if publisher:
        entry += f" {publisher}."
    else:
        issues.append("Publisher is missing; the bibliography entry needs review.")
    doi = str(source.get("doi") or "").strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    if doi:
        if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            entry += f" https://doi.org/{doi}"
        else:
            issues.append("The supplied DOI does not look like a DOI.")
    # An absent publisher makes the bibliography incomplete, but not an otherwise
    # verified in-text author/date/page citation. Keep these concerns distinct.
    blocking = [issue for issue in issues if not issue.startswith(("Publisher", "The supplied DOI"))]
    return {"formatted_citation": formatted, "bibliography_entry": entry,
            "citation_ready": not blocking, "metadata_issues": issues,
            "citation_style_note": "Book author-date template; check your university's style rules."}


def normalize_quote(text: str) -> str:
    # Whitespace and soft hyphen normalization only; not fuzzy matching.
    return " ".join(unicodedata.normalize("NFC", text).replace("\u00ad", "").split())


def quote_in_source(quote: str, text: str) -> bool:
    quote = normalize_quote(quote)
    return bool(quote) and quote in normalize_quote(text)


def finalize_answer(raw: dict, evidence: dict[str, dict], style: str) -> dict:
    answer = str(raw.get("answer") or "")
    suggestion = str(raw.get("suggestion") or "")
    warnings = [str(item)[:1200] for item in raw.get("warnings", []) if isinstance(item, str)]
    citations = []
    used = set()
    unsafe_suggestion = False
    requested = {item.get("source_id"): item for item in raw.get("citations", []) if isinstance(item, dict)}
    markers = set(re.findall(r"\[(S\d+)\]", answer + "\n" + suggestion))
    for source_id in sorted(markers | set(requested), key=lambda x: str(x)):
        if source_id not in evidence:
            unsafe_suggestion = True
            warnings.append(f"Unrecognized evidence reference {source_id} was removed.")
            marker = f"[{source_id}]"
            answer = answer.replace(marker, "[reference unavailable]")
            suggestion = suggestion.replace(marker, "[reference unavailable]")
            continue
        source = evidence[source_id]
        quote = str(requested.get(source_id, {}).get("quote") or "")
        verified = quote_in_source(quote, source["text"])
        if quote and not verified:
            unsafe_suggestion = True
            warnings.append(f"{source_id}: the proposed quotation was not found verbatim in the extracted passage.")
            answer = answer.replace(quote, "[unverified quotation removed]")
            # Do not present the model's invented quote as evidence.
            quote = ""
        detail = citation_details(source, style)
        if requested.get(source_id, {}).get("quote") and not verified:
            detail["citation_ready"] = False
            detail["metadata_issues"].append("The proposed quotation failed exact-text verification.")
        citation = {**source, **detail, "label": source_id, "quote": quote,
                    "quote_verified": verified,
                    "note": str(requested.get(source_id, {}).get("note") or "")}
        citations.append(citation)
        used.add(source_id)
        if detail["citation_ready"]:
            suggestion = suggestion.replace(f"[{source_id}]", detail["formatted_citation"])
        elif f"[{source_id}]" in suggestion:
            # Persistent markers must remain resolvable outside this chat turn.
            locator = source.get("location") or source.get("filename", "source")
            suggestion = suggestion.replace(f"[{source_id}]",
                f"[SOURCE {source['chunk_id']}: {source.get('title', 'Untitled')}; {locator}; verify citation]")
            warnings.append(f"{source_id}: complete and verify metadata/page mapping before using this citation.")
    if not used and raw.get("citations"):
        warnings.append("No supplied citation could be matched to retrieved evidence.")
    if unsafe_suggestion and suggestion:
        suggestion = ""
        warnings.append("The proposed insertion was withheld because a citation or quotation could not be verified.")
    return {"answer": answer, "suggestion": suggestion, "citations": citations,
            "warnings": list(dict.fromkeys(warnings))}


def bibliography_export(documents: list[dict], format: str) -> str:
    """Export only supplied fields, with gaps and verification status in notes."""
    blocks = []
    for doc in documents:
        details = citation_details(doc)
        key = re.sub(r"[^A-Za-z0-9_:.-]", "", str(doc.get("citation_key") or doc["id"])) or doc["id"]
        if format == "ris":
            lines = ["TY  - BOOK", "ID  - " + key]
            for author in _authors(doc):
                lines.append("AU  - " + author.replace("\n", " "))
            for field, tag in (("title", "TI"), ("year", "PY"), ("publisher", "PB"), ("edition", "ET"), ("doi", "DO")):
                if doc.get(field):
                    lines.append(f"{tag}  - " + str(doc[field]).replace("\n", " "))
            if details["metadata_issues"]:
                lines.append("N1  - " + " ".join(details["metadata_issues"]))
            lines.append("ER  - ")
            blocks.append("\n".join(lines))
        else:
            def escaped(value: str) -> str:
                # Encode TeX-sensitive content without allowing it to close a field.
                return (str(value).replace("\\", "\\textbackslash{}")
                        .replace("{", "\\{").replace("}", "\\}")
                        .replace("%", "\\%").replace("&", "\\&")
                        .replace("_", "\\_").replace("#", "\\#")
                        .replace("$", "\\$").replace("\n", " "))
            fields = {"title": doc.get("title"), "author": " and ".join(_authors(doc)),
                      "year": doc.get("year"), "publisher": doc.get("publisher"),
                      "edition": doc.get("edition"), "doi": doc.get("doi"),
                      "note": " ".join(details["metadata_issues"])}
            lines = ["@book{" + key + ","]
            lines += [f"  {field} = {{{escaped(value)}}}," for field, value in fields.items() if value]
            lines.append("}")
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
