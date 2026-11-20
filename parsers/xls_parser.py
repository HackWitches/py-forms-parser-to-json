from pathlib import Path
import xlrd
import openpyxl
from extractors import extract_bureau_info, slugify


def parse_xls(file_path: Path) -> dict:
    """Dispatches .xls (xlrd) or .xlsx (openpyxl)."""
    if file_path.suffix.lower() == ".xlsx":
        return _parse_xlsx(file_path)
    return _parse_legacy_xls(file_path)


def _parse_legacy_xls(file_path: Path) -> dict:
    wb = xlrd.open_workbook(file_path, formatting_info=True)
    sheet = wb.sheet_by_index(0)

    merged_map = {}
    for rlo, rhi, clo, chi in sheet.merged_cells:
        for r in range(rlo, rhi):
            for c in range(clo, chi):
                merged_map[(r, c)] = (rlo, rhi, clo, chi)

    def cell_has_full_box(r: int, c: int) -> bool:
        xf = wb.xf_list[sheet.cell_xf_index(r, c)]
        b = xf.border
        return all([b.top_line_style, b.bottom_line_style, b.left_line_style, b.right_line_style])

    def cell_is_bold(r: int, c: int) -> bool:
        xf = wb.xf_list[sheet.cell_xf_index(r, c)]
        return wb.font_list[xf.font_index].bold == 1

    title = None
    current_section = None
    fields = []
    notes = []
    all_lines = []
    consumed_cells = set()

    for r in range(sheet.nrows):
        row_texts = []
        for c in range(sheet.ncols):
            if (r, c) in consumed_cells:
                continue

            val = sheet.cell_value(r, c)
            if not isinstance(val, str) or not val.strip():
                continue

            clean_val = " ".join(val.split())
            row_texts.append(clean_val)

            # 1. Main centered/offset bold title near the top
            if title is None and r < 15 and cell_is_bold(r, c) and c >= 4:
                title = clean_val
                continue

            # 2. Bold section headers along the left margin
            if cell_is_bold(r, c) and c <= 3:
                current_section = clean_val.rstrip(":")
                continue

            # 3. Single-cell square checkbox immediately to the left of text (c - 1)
            if (
                c >= 1
                and sheet.cell_value(r, c - 1) == ""
                and cell_has_full_box(r, c - 1)
                and (r, c - 1) not in merged_map
            ):
                fields.append({
                    "field_id": slugify(clean_val),
                    "section": current_section,
                    "label": clean_val,
                    "cell_coord": f"R{r+1}C{c}",
                    "type": "checkbox",
                    "value": False,
                })
                consumed_cells.add((r, c))
                continue

            # 4. Scan to the right for a bordered empty box or dotted template box
            target_box = None
            for scan_c in range(c + 1, sheet.ncols):
                scan_val = sheet.cell_value(r, scan_c)
                if (r, scan_c) in merged_map:
                    rlo, rhi, clo, chi = merged_map[(r, scan_c)]
                    top_left = sheet.cell_value(rlo, clo)
                    if cell_has_full_box(rlo, clo) and (top_left == "" or "…" in str(top_left) or ".." in str(top_left)):
                        target_box = (rlo, clo, top_left if top_left != "" else None)
                        consumed_cells.add((rlo, clo))
                        break
                elif scan_val == "" and cell_has_full_box(r, scan_c):
                    target_box = (r, scan_c, None)
                    break
                elif isinstance(scan_val, str) and scan_val.strip():
                    break

            if target_box:
                tr, tc, template_val = target_box
                label = clean_val.rstrip(":")
                field_obj = {
                    "field_id": slugify(label),
                    "section": current_section,
                    "label": label,
                    "cell_coord": f"R{tr+1}C{tc+1}",
                    "type": "text",
                    "value": None,
                }
                if template_val:
                    field_obj["template"] = " ".join(str(template_val).split())
                fields.append(field_obj)
                consumed_cells.add((r, c))
                continue

            # 5. Signature box directly ABOVE the label
            if r >= 1 and cell_has_full_box(r - 1, c) and sheet.cell_value(r - 1, c) == "":
                fields.append({
                    "field_id": slugify(clean_val),
                    "section": current_section,
                    "label": clean_val,
                    "cell_coord": f"R{r}C{c+1}",
                    "type": "signature",
                    "value": None,
                })
                consumed_cells.add((r, c))
                continue

            # 6. Label with static/option text directly below it
            if r + 1 < sheet.nrows and c <= 3:
                below_val = sheet.cell_value(r + 1, c)
                if isinstance(below_val, str) and below_val.strip() and not cell_is_bold(r + 1, c):
                    has_right_box = any(cell_has_full_box(r + 1, sc) for sc in range(c + 1, sheet.ncols))
                    if not has_right_box:
                        fields.append({
                            "field_id": slugify(clean_val),
                            "section": current_section,
                            "label": clean_val.rstrip(":"),
                            "type": "static_or_option",
                            "value": " ".join(below_val.split()),
                        })
                        consumed_cells.update({(r, c), (r + 1, c)})
                        continue

            # 7. Footnotes / instructions starting with "*"
            if clean_val.startswith("*"):
                note_txt = clean_val.lstrip("* ").strip()
                if len(note_txt) > 3:
                    notes.append(note_txt)

        if row_texts:
            all_lines.append(" ".join(row_texts))

    return {
        "source_file": file_path.name,
        "format": "xls",
        "form_title": title,
        "bureau_info": extract_bureau_info("\n".join(all_lines)),
        "fields": fields,
        "notes": notes,
    }


def _parse_xlsx(file_path: Path) -> dict:
    """Modern .xlsx equivalent using openpyxl border & merge inspection."""
    wb = openpyxl.load_workbook(file_path, data_only=False)
    sheet = wb.active

    def has_box(cell) -> bool:
        b = cell.border
        return bool(b and b.top and b.top.style and b.bottom and b.bottom.style and b.left and b.left.style and b.right and b.right.style)

    title = None
    current_section = None
    fields = []
    all_lines = []

    for r in range(1, sheet.max_row + 1):
        row_texts = []
        for c in range(1, sheet.max_column + 1):
            cell = sheet.cell(r, c)
            if not isinstance(cell.value, str) or not cell.value.strip():
                continue
            clean_val = " ".join(cell.value.split())
            row_texts.append(clean_val)

            if title is None and r < 15 and cell.font and cell.font.bold and c >= 4:
                title = clean_val
                continue
            if cell.font and cell.font.bold and c <= 3:
                current_section = clean_val.rstrip(":")
                continue
            if c > 1 and sheet.cell(r, c - 1).value is None and has_box(sheet.cell(r, c - 1)):
                fields.append({
                    "field_id": slugify(clean_val),
                    "section": current_section,
                    "label": clean_val,
                    "cell_coord": sheet.cell(r, c - 1).coordinate,
                    "type": "checkbox",
                    "value": False,
                })
                continue
            for sc in range(c + 1, sheet.max_column + 1):
                target = sheet.cell(r, sc)
                if target.value is None and has_box(target):
                    fields.append({
                        "field_id": slugify(clean_val.rstrip(":")),
                        "section": current_section,
                        "label": clean_val.rstrip(":"),
                        "cell_coord": target.coordinate,
                        "type": "text",
                        "value": None,
                    })
                    break
        if row_texts:
            all_lines.append(" ".join(row_texts))

    return {
        "source_file": file_path.name,
        "format": "xlsx",
        "form_title": title,
        "bureau_info": extract_bureau_info("\n".join(all_lines)),
        "fields": fields,
        "notes": [],
    }