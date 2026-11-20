from pathlib import Path
import xlrd
import openpyxl
from extractors import extract_bureau_info, slugify


def parse_xls(file_path: Path) -> dict:
    if file_path.suffix.lower() == ".xlsx":
        return _parse_xlsx(file_path)
    return _parse_legacy_xls(file_path)


def _attach_note_to_preceding_group(items: list[dict], note_text: str) -> bool:
    """
    Attaches a footnote starting with '*' or '1)' to the immediately preceding
    block of checkboxes or asterisk-marked fields. Returns True if attached.
    """
    attached = False
    for entry in reversed(items):
        if not entry.get("is_field"):
            break
        if entry.get("type") == "checkbox" or "*" in entry.get("label", ""):
            existing = entry.get("note")
            entry["note"] = f"{existing} | {note_text}" if existing else note_text
            attached = True
        elif attached:
            break
    return attached


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

    def cell_is_italic(r: int, c: int) -> bool:
        xf = wb.xf_list[sheet.cell_xf_index(r, c)]
        return wb.font_list[xf.font_index].italic == 1

    title = None
    current_section = None
    items = []
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

            # 2. Asterisk / italic footnote directly under checkboxes or fields
            if clean_val.startswith("*") or (cell_is_italic(r, c) and re.match(r"^(\*|\d+\))", clean_val)):
                note_txt = clean_val.lstrip("* ").strip()
                if len(note_txt) > 3:
                    notes.append(note_txt)
                    _attach_note_to_preceding_group(items, note_txt)
                consumed_cells.add((r, c))
                continue

            # 3. Single-cell square checkbox immediately to the left of text (c - 1)
            if (
                c >= 1
                and sheet.cell_value(r, c - 1) == ""
                and cell_has_full_box(r, c - 1)
                and (r, c - 1) not in merged_map
            ):
                items.append({
                    "field_id": slugify(clean_val),
                    "section": current_section,
                    "label": clean_val,
                    "is_field": True,
                    "modifiable": True,
                    "type": "checkbox",
                    "value": False,
                })
                consumed_cells.add((r, c))
                continue

            # 4. Scan to the right for a bordered box (empty, dotted template, OR pre-filled static value like '*1*')
            target_box = None
            for scan_c in range(c + 1, sheet.ncols):
                scan_val = sheet.cell_value(r, scan_c)
                if (r, scan_c) in merged_map:
                    rlo, rhi, clo, chi = merged_map[(r, scan_c)]
                    top_left = sheet.cell_value(rlo, clo)
                    if cell_has_full_box(rlo, clo):
                        target_box = (rlo, clo, top_left)
                        consumed_cells.add((rlo, clo))
                        break
                elif cell_has_full_box(r, scan_c):
                    target_box = (r, scan_c, scan_val)
                    consumed_cells.add((r, scan_c))
                    break
                elif isinstance(scan_val, str) and scan_val.strip():
                    break

            if target_box is not None:
                _, _, box_raw_val = target_box
                box_str = " ".join(str(box_raw_val).split()) if box_raw_val not in ("", None) else ""
                label = clean_val.rstrip(":")

                # Check if the box is empty or dotted template -> modifiable=True
                if box_str == "" or "…" in box_str or ".." in box_str:
                    entry = {
                        "field_id": slugify(label),
                        "section": current_section,
                        "label": label,
                        "is_field": True,
                        "modifiable": True,
                        "type": "text",
                        "value": None,
                    }
                    if box_str:
                        entry["template"] = box_str
                else:
                    # Pre-filled bordered box (e.g., wyróżnik: "*1*") -> modifiable=False
                    entry = {
                        "field_id": slugify(label),
                        "section": current_section,
                        "label": label,
                        "is_field": True,
                        "modifiable": False,
                        "type": "text",
                        "value": box_str,
                    }

                items.append(entry)
                consumed_cells.add((r, c))
                continue

            # 5. Signature box directly ABOVE the label
            if r >= 1 and cell_has_full_box(r - 1, c) and sheet.cell_value(r - 1, c) == "":
                items.append({
                    "field_id": slugify(clean_val),
                    "section": current_section,
                    "label": clean_val,
                    "is_field": True,
                    "modifiable": True,
                    "type": "signature",
                    "value": None,
                })
                consumed_cells.add((r, c))
                continue

            # 6. Bold section header along the left margin (kept with is_field=False)
            if cell_is_bold(r, c) and c <= 3:
                sec_name = clean_val.rstrip(":")
                current_section = sec_name
                items.append({
                    "field_id": slugify(sec_name),
                    "section": current_section,
                    "label": sec_name,
                    "is_field": False,
                    "modifiable": False,
                    "type": "section_header",
                    "value": clean_val,
                })
                consumed_cells.add((r, c))
                continue

            # 7. Label with static/pre-defined value directly below it (e.g., rodzaj pieczęci)
            if r + 1 < sheet.nrows and c <= 3:
                below_val = sheet.cell_value(r + 1, c)
                if isinstance(below_val, str) and below_val.strip() and not cell_is_bold(r + 1, c):
                    has_right_box = any(cell_has_full_box(r + 1, sc) for sc in range(c + 1, sheet.ncols))
                    if not has_right_box:
                        label = clean_val.rstrip(":")
                        items.append({
                            "field_id": slugify(label),
                            "section": current_section,
                            "label": label,
                            "is_field": True,
                            "modifiable": False,
                            "type": "static_value",
                            "value": " ".join(below_val.split()),
                        })
                        consumed_cells.update({(r, c), (r + 1, c)})
                        continue

        if row_texts:
            all_lines.append(" ".join(row_texts))

    return {
        "source_file": file_path.name,
        "format": "xls",
        "form_title": title,
        "bureau_info": extract_bureau_info("\n".join(all_lines)),
        "items": items,
        "notes": notes,
    }


def _parse_xlsx(file_path: Path) -> dict:
    """Modern .xlsx parser matching the exact same schema."""
    wb = openpyxl.load_workbook(file_path, data_only=False)
    sheet = wb.active

    def has_box(cell) -> bool:
        b = cell.border
        return bool(
            b and b.top and b.top.style and b.bottom and b.bottom.style
            and b.left and b.left.style and b.right and b.right.style
        )

    title = None
    current_section = None
    items = []
    notes = []
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

            if clean_val.startswith("*"):
                note_txt = clean_val.lstrip("* ").strip()
                notes.append(note_txt)
                _attach_note_to_preceding_group(items, note_txt)
                continue

            if c > 1 and sheet.cell(r, c - 1).value is None and has_box(sheet.cell(r, c - 1)):
                items.append({
                    "field_id": slugify(clean_val),
                    "section": current_section,
                    "label": clean_val,
                    "is_field": True,
                    "modifiable": True,
                    "type": "checkbox",
                    "value": False,
                })
                continue

            found_box = False
            for sc in range(c + 1, sheet.max_column + 1):
                target = sheet.cell(r, sc)
                if has_box(target):
                    box_str = " ".join(str(target.value).split()) if target.value not in ("", None) else ""
                    is_empty_or_dots = box_str == "" or "…" in box_str or ".." in box_str
                    items.append({
                        "field_id": slugify(clean_val.rstrip(":")),
                        "section": current_section,
                        "label": clean_val.rstrip(":"),
                        "is_field": True,
                        "modifiable": is_empty_or_dots,
                        "type": "text",
                        "value": None if is_empty_or_dots else box_str,
                    })
                    found_box = True
                    break

            if not found_box and cell.font and cell.font.bold and c <= 3:
                sec_name = clean_val.rstrip(":")
                current_section = sec_name
                items.append({
                    "field_id": slugify(sec_name),
                    "section": current_section,
                    "label": sec_name,
                    "is_field": False,
                    "modifiable": False,
                    "type": "section_header",
                    "value": clean_val,
                })

        if row_texts:
            all_lines.append(" ".join(row_texts))

    return {
        "source_file": file_path.name,
        "format": "xlsx",
        "form_title": title,
        "bureau_info": extract_bureau_info("\n".join(all_lines)),
        "items": items,
        "notes": notes,
    }