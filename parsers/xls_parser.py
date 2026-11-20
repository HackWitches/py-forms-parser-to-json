from pathlib import Path
import xlrd
from extractors import extract_bureau_info, slugify


def parse_xls(file_path: Path) -> dict:
    """Parses legacy .xls forms for centered/bold title, bureau info, and input cells."""
    # formatting_info=True lets us inspect font weight (bold) and alignment (center)
    wb = xlrd.open_workbook(file_path, formatting_info=True)
    sheet = wb.sheet_by_index(0)

    title_parts = []
    all_text_lines = []
    fields = []

    for r in range(sheet.nrows):
        row_texts = []
        for c in range(sheet.ncols):
            val = sheet.cell_value(r, c)
            if not isinstance(val, str) or not val.strip():
                continue

            clean_val = " ".join(val.split())
            row_texts.append(clean_val)

            # Inspect cell formatting for title detection (top 12 rows)
            if r < 12:
                xf_index = sheet.cell_xf_index(r, c)
                xf = wb.xf_list[xf_index]
                font = wb.font_list[xf.font_index]
                is_bold = font.bold == 1
                # hor_align == 2 means centered in Excel BIFF8 format
                is_centered = xf.alignment.hor_align == 2

                if is_bold and (is_centered or "wniosek" in clean_val.lower()):
                    title_parts.append(clean_val)

            # Detect input fields: text label followed by an empty cell to the right or below
            right_is_empty = (
                c + 1 < sheet.ncols and sheet.cell_value(r, c + 1) == ""
            )
            if (clean_val.endswith(":") or "...." in clean_val) and right_is_empty:
                label = clean_val.rstrip(":. ")
                if len(label) > 1:
                    fields.append({
                        "field_id": slugify(label),
                        "label": label,
                        "cell_coord": f"R{r+1}C{c+2}",
                        "type": "text",
                        "value": None,
                    })

        if row_texts:
            all_text_lines.append(" ".join(row_texts))

    full_text = "\n".join(all_text_lines)

    return {
        "source_file": file_path.name,
        "format": "xls",
        "form_title": " ".join(title_parts) if title_parts else None,
        "bureau_info": extract_bureau_info(full_text),
        "fields": fields,
    }