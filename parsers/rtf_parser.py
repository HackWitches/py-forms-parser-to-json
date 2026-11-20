import re
from pathlib import Path
from striprtf.striprtf import rtf_to_text
from extractors import extract_bureau_info, slugify


def _extract_rtf_center_bold_title(raw_rtf: str) -> str | None:
    """Finds centered + bold paragraphs in raw RTF markup."""
    # Split into RTF paragraph blocks
    paragraphs = re.split(r"\\par\b", raw_rtf)
    title_parts = []

    for p in paragraphs[:15]:  # Title is near the top of the document
        is_centered = r"\qc" in p
        is_bold = bool(re.search(r"\\b(?!0)\b", p))
        if is_centered and is_bold:
            clean = rtf_to_text(p).strip()
            if clean:
                title_parts.append(clean)
        elif title_parts:
            # Stop once the centered bold block finishes
            break

    return " ".join(title_parts) if title_parts else None


def parse_rtf(file_path: Path) -> dict:
    """Parses an RTF form for title, bureau info, and dotted/bracketed fields."""
    # Polish RTF files often use cp1250 or utf-8; striprtf handles RTF hex escapes (\'xx)
    raw_rtf = file_path.read_text(encoding="utf-8", errors="ignore")
    plain_text = rtf_to_text(raw_rtf)

    title = _extract_rtf_center_bold_title(raw_rtf)
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]

    # Fallback title: look for "Wniosek" and the following line
    if not title:
        for i, line in enumerate(lines[:10]):
            if line.lower().startswith("wniosek"):
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                title = f"{line} {next_line}".strip()
                break

    fields = []
    # Match patterns like "Imię /imiona/ ............." or "data ............"
    dotted_field_re = re.compile(r"^([^.:\n]{2,80}?)\s*:?\s*\.{4,}\s*$")

    for line in lines:
        match = dotted_field_re.match(line)
        if match:
            label = match.group(1).strip()
            # Skip standalone signature lines
            if label.lower() in {"podpis", "/podpis/"}:
                fields.append({
                    "field_id": "podpis",
                    "label": "Podpis",
                    "type": "signature",
                    "value": None,
                })
                continue

            fields.append({
                "field_id": slugify(label),
                "label": label,
                "type": "text",
                "value": None,
            })

    return {
        "source_file": file_path.name,
        "format": "rtf",
        "form_title": title,
        "bureau_info": extract_bureau_info(plain_text),
        "fields": fields,
    }