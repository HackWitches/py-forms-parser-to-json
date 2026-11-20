import re
from pathlib import Path
from striprtf.striprtf import rtf_to_text
from extractors import extract_bureau_info, slugify


def parse_rtf(file_path: Path) -> dict:
    raw_bytes = file_path.read_bytes()
    cp_match = re.search(rb"\\ansicpg(\d+)", raw_bytes[:500])
    encoding = f"cp{cp_match.group(1).decode()}" if cp_match else "cp1250"
    raw_rtf = raw_bytes.decode(encoding, errors="replace")
    plain_text = rtf_to_text(raw_rtf)

    # 1. Extract centered + bold title from raw RTF paragraphs
    paragraphs = re.split(r"\\par\b", raw_rtf)
    title_parts = []
    for p in paragraphs[:15]:
        is_centered = r"\qc" in p
        is_bold = bool(re.search(r"\\b(?!0)\b", p))
        if is_centered and is_bold:
            clean = " ".join(rtf_to_text(p).split())
            if clean:
                title_parts.append(clean)
        elif title_parts:
            break
    title = " ".join(title_parts) if title_parts else None

    lines = [line.replace("\xa0", " ").strip() for line in plain_text.splitlines()]
    lines = [l for l in lines if l != ""]

    if not title:
        for i, line in enumerate(lines[:10]):
            if line.lower().startswith("wniosek"):
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                title = f"{line} {nxt}".strip()
                break

    # Match both ASCII dots (....) and Unicode ellipsis (……)
    dots_only_re = re.compile(r"^[.\u2026\s]{4,}$")
    label_dots_re = re.compile(r"^(.+?)\s*:?\s*[.\u2026]{4,}\s*$")
    sub_desc_re = re.compile(r"^(/.*?/|\(.*?\))$")

    fields = []
    notes = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if title and line in title:
            i += 1
            continue

        # Case A: "Label ............."
        m = label_dots_re.match(line)
        if m and not dots_only_re.match(line):
            label = m.group(1).strip().rstrip(":")
            multiline = False
            while i + 1 < len(lines) and dots_only_re.match(lines[i + 1]):
                multiline = True
                i += 1

            # Capture italic /slash/ or (parenthetical) description lines below the dots
            descriptions = []
            while i + 1 < len(lines) and sub_desc_re.match(lines[i + 1]):
                desc_line = lines[i + 1].strip()
                # Do not swallow "/podpis/" if it belongs to a signature line
                if "podpis" in desc_line.lower():
                    break
                descriptions.append(desc_line)
                i += 1

            field_obj = {
                "field_id": slugify(label),
                "label": label,
                "type": "multiline_text" if multiline else "text",
                "value": None,
            }
            if descriptions:
                field_obj["description"] = " ".join(descriptions)
            fields.append(field_obj)
            i += 1
            continue

        # Case B: Label on line i, pure dots ".........." on line i+1
        if i + 1 < len(lines) and dots_only_re.match(lines[i + 1]) and not dots_only_re.match(line):
            label = line.rstrip(":")
            i += 1
            while i + 1 < len(lines) and dots_only_re.match(lines[i + 1]):
                i += 1
            descriptions = []
            while i + 1 < len(lines) and sub_desc_re.match(lines[i + 1]):
                descriptions.append(lines[i + 1].strip())
                i += 1

            field_obj = {
                "field_id": slugify(label),
                "label": label,
                "type": "text",
                "value": None,
            }
            if descriptions:
                field_obj["description"] = " ".join(descriptions)
            fields.append(field_obj)
            i += 1
            continue

        # Case C: Standalone signature dots above "/podpis/"
        if dots_only_re.match(line):
            if i + 1 < len(lines) and "podpis" in lines[i + 1].lower():
                fields.append({
                    "field_id": "podpis",
                    "label": lines[i + 1].strip("/ "),
                    "type": "signature",
                    "value": None,
                })
                i += 2
                continue

        if line.startswith("- "):
            notes.append(line[2:].strip())

        i += 1

    return {
        "source_file": file_path.name,
        "format": "rtf",
        "form_title": title,
        "bureau_info": extract_bureau_info(plain_text),
        "fields": fields,
        "notes": notes,
    }