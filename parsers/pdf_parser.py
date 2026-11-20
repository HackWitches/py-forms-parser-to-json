from pathlib import Path
import fitz  # PyMuPDF
import pdfplumber
from extractors import extract_bureau_info, slugify


def _extract_pdf_center_bold_title(pdf_path: Path) -> str | None:
    """Finds large/bold centered text on Page 1 of a PDF."""
    doc = fitz.open(pdf_path)
    if len(doc) == 0:
        return None

    page = doc[0]
    page_width = page.rect.width
    blocks = page.get_text("dict")["blocks"]
    title_lines = []

    for b in blocks:
        if "lines" not in b:
            continue
        for line in b["lines"]:
            x0, _, x1, _ = line["bbox"]
            line_center = (x0 + x1) / 2
            is_centered = abs(line_center - (page_width / 2)) < (page_width * 0.15)

            spans = line["spans"]
            text = "".join(s["text"] for s in spans).strip()
            if not text:
                continue

            is_bold = any(
                "bold" in s["font"].lower() or (s["flags"] & 2**4)
                for s in spans
            )
            max_size = max(s["size"] for s in spans)

            if is_centered and (is_bold or max_size >= 11.5):
                # Filter out legal header boilerplate
                if not any(skip in text.upper() for skip in ["DZIENNIK USTAW", "WZÓR", "ZAŁĄCZNIK"]):
                    title_lines.append(text)

    doc.close()
    return " ".join(title_lines) if title_lines else None


def parse_pdf(file_path: Path) -> dict:
    """Parses PDF form title, bureau block, and table-based input fields."""
    title = _extract_pdf_center_bold_title(file_path)
    full_text_pages = []
    fields = []
    current_section = None

    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            full_text_pages.append(page_text)

            # Extract structured tables (used heavily in Polish official forms)
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    cleaned_cells = [
                        " ".join(c.split()) if c else "" for c in row
                    ]
                    non_empty = [c for c in cleaned_cells if c]

                    # Skip empty rows or table list headers like ["Lp.", "Nazwa dokumentu"]
                    if not non_empty or non_empty == ["Lp.", "Nazwa dokumentu"]:
                        continue

                    # Detect section headers spanning the row (e.g., "I. Dane wnioskodawcy:")
                    if len(non_empty) == 1 and len(cleaned_cells) > 1:
                        if non_empty[0].endswith(":"):
                            current_section = non_empty[0].rstrip(":")
                        continue

                    # Standard 2-column form row: [Label, Empty Input Cell]
                    if len(cleaned_cells) >= 2:
                        label = cleaned_cells[0]
                        val_cell = cleaned_cells[1]

                        # Skip numbered attachment rows (1..20)
                        if label.isdigit():
                            continue

                        if label and val_cell == "":
                            field_type = "checkbox" if "☐" in label else "text"
                            clean_label = label.replace("☐", "").strip()
                            fields.append({
                                "field_id": slugify(clean_label),
                                "section": current_section,
                                "label": clean_label,
                                "type": field_type,
                                "value": None,
                            })

    full_text = "\n".join(full_text_pages)

    return {
        "source_file": file_path.name,
        "format": "pdf",
        "form_title": title,
        "bureau_info": extract_bureau_info(full_text),
        "fields": fields,
    }