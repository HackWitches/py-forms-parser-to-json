import re
from pathlib import Path
import fitz  # PyMuPDF
from extractors import (
    extract_bureau_info,
    normalize_polish_pdf_text,
    slugify,
    split_label_and_footnote,
)


def parse_pdf(file_path: Path) -> dict:
    doc = fitz.open(file_path)
    title_lines = []
    full_text_pages = []
    items = []
    footnotes_map = {}

    current_section = None
    current_subsection = None

    section_re = re.compile(r"^([IVX]+\.\s+.+)$")
    subsec_re = re.compile(r"^(\d+\.\s+[^\d].+)$")
    footnote_line_re = re.compile(r"^(\d+)\)\s*(.+)$")

    for page_idx, page in enumerate(doc):
        page_w = page.rect.width
        page_h = page.rect.height

        # 1. Extract vector drawings: table borders & square checkboxes
        h_lines = []
        v_lines = []
        checkbox_rects = []

        for d in page.get_drawings():
            rect = d["rect"]
            w, h = rect.width, rect.height
            if h < 2.5 and w > 15.0:
                h_lines.append(((rect.y0 + rect.y1) / 2, rect.x0, rect.x1))
            elif w < 2.5 and h > 10.0:
                v_lines.append(((rect.x0 + rect.x1) / 2, rect.y0, rect.y1))
            elif 10.0 <= w <= 30.0 and 10.0 <= h <= 30.0:
                checkbox_rects.append((rect.x0, rect.y0, rect.x1, rect.y1))

        # 2. Extract individual SPANS (decoded BEFORE .split() so 0x0B/0x0C become '(' and ')')
        raw_page_text = normalize_polish_pdf_text(page.get_text("text"))
        full_text_pages.append(raw_page_text)

        spans_list = []
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for s in line["spans"]:
                    decoded = normalize_polish_pdf_text(s["text"])
                    clean_s = " ".join(decoded.split())
                    if not clean_s:
                        continue
                    is_bold = "bold" in s["font"].lower() or bool(s["flags"] & 16)
                    x0, y0, x1, y1 = s["bbox"]
                    spans_list.append({
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "text": clean_s,
                        "bold": is_bold,
                        "size": s["size"],
                    })

        spans_list.sort(key=lambda s: (round(s["y0"], 1), s["x0"]))

        # Merge spans that share the same visual line (e.g. "I." + "Dane wnioskodawcy:")
        visual_lines = []
        for sp in spans_list:
            if visual_lines and abs(visual_lines[-1]["y0"] - sp["y0"]) < 3.0:
                prev = visual_lines[-1]
                # Join without extra space if sp is a superscript footnote like "3)"
                is_super = sp["size"] < prev["size"] * 0.85 and re.match(r"^\d+\)$", sp["text"])
                sep = "" if is_super else " "
                prev["text"] = f"{prev['text']}{sep}{sp['text']}"
                prev["x1"] = max(prev["x1"], sp["x1"])
                prev["y1"] = max(prev["y1"], sp["y1"])
                prev["bold"] = prev["bold"] or sp["bold"]
                prev["spans"].append(sp)
            else:
                visual_lines.append({
                    "x0": sp["x0"], "y0": sp["y0"], "x1": sp["x1"], "y1": sp["y1"],
                    "text": sp["text"],
                    "bold": sp["bold"],
                    "size": sp["size"],
                    "spans": [sp],
                })

        # 3. Collect bottom-of-page footnotes (supporting multi-line footnotes)
        active_fn_id = None
        for vl in visual_lines:
            if vl["y0"] > page_h * 0.72:
                fn_m = footnote_line_re.match(vl["text"])
                if fn_m:
                    active_fn_id = fn_m.group(1)
                    footnotes_map[active_fn_id] = fn_m.group(2).strip()
                elif active_fn_id and not vl["text"].startswith("©") and "Dziennik Ustaw" not in vl["text"]:
                    footnotes_map[active_fn_id] += " " + vl["text"].strip()

        # 4. Build table row bands from horizontal lines
        ys = sorted({round(y, 1) for y, _, _ in h_lines if y < page_h * 0.88})
        clustered_ys = []
        for y in ys:
            if not clustered_ys or abs(clustered_ys[-1] - y) > 2.5:
                clustered_ys.append(y)

        # Group contiguous table regions so standalone headings between tables are NOT skipped
        table_bands = []
        for i in range(len(clustered_ys) - 1):
            y_top, y_bot = clustered_ys[i], clustered_ys[i + 1]
            row_h = y_bot - y_top
            if 8 <= row_h <= 280:
                xs = sorted({round(x, 1) for x, vy0, vy1 in v_lines if vy0 <= y_top + 3 and vy1 >= y_bot - 3})
                if len(xs) >= 2:
                    table_bands.append((y_top, y_bot, xs))

        def is_inside_any_table_row(y_mid: float) -> bool:
            return any(yt - 1.5 <= y_mid <= yb + 1.5 for yt, yb, _ in table_bands)

        # 5. Extract Page 1 Title
        first_table_y = table_bands[0][0] if table_bands else page_h
        if page_idx == 0:
            for vl in visual_lines:
                if vl["y1"] > first_table_y:
                    continue
                center_x = (vl["x0"] + vl["x1"]) / 2
                is_centered = abs(center_x - page_w / 2) < page_w * 0.25
                if is_centered and (vl["bold"] or vl["size"] >= 11.5):
                    upper = vl["text"].upper().replace(" ", "")
                    if not any(skip in upper for skip in ["DZIENNIKUSTAW", "ZAŁĄCZNIK", "POZ."]):
                        clean_t, _ = split_label_and_footnote(vl["text"])
                        title_lines.append(clean_t)

        # Deduplicate vector checkboxes on this page
        dedup_cbs = []
        for cb in sorted(checkbox_rects, key=lambda r: (round(r[1], 1), r[0])):
            if not dedup_cbs or abs(dedup_cbs[-1][0] - cb[0]) > 4 or abs(dedup_cbs[-1][1] - cb[1]) > 4:
                dedup_cbs.append(cb)

        # Build unified timeline of events (non-table lines, checkboxes, and table rows) sorted top-to-bottom
        events = []

        for vl in visual_lines:
            if vl["y0"] > page_h * 0.82:
                continue
            y_mid = (vl["y0"] + vl["y1"]) / 2
            if not is_inside_any_table_row(y_mid):
                events.append((vl["y0"], 0, "line", vl))

        # Group checkboxes by row Y so "Pan [ ] Pani [ ] 2)" pairs each box with the span immediately to its left
        cb_rows = []
        for cb in dedup_cbs:
            if cb_rows and abs(cb_rows[-1][0][1] - cb[1]) < 8.0:
                cb_rows[-1].append(cb)
            else:
                cb_rows.append([cb])

        for row_cbs in cb_rows:
            row_cbs.sort(key=lambda r: r[0])
            events.append((row_cbs[0][1], 1, "cb_row", row_cbs))

        for y_top, y_bot, xs in table_bands:
            events.append((y_top, 2, "table_row", (y_top, y_bot, xs)))

        events.sort(key=lambda e: (round(e[0], 1), e[1]))

        pending_col_headers = None

        for _, _, ev_type, payload in events:
            # A. Standalone lines outside tables (Sections I., II., Subsections 1., 2., instructions)
            if ev_type == "line":
                vl = payload
                txt = vl["text"]
                if title_lines and any(txt.startswith(t) for t in title_lines):
                    continue

                sec_m = section_re.match(txt)
                sub_m = subsec_re.match(txt)

                if sec_m and vl["bold"]:
                    sec_clean, fn_id = split_label_and_footnote(txt)
                    current_section = sec_clean
                    current_subsection = None
                    entry = {
                        "field_id": slugify(sec_clean),
                        "section": current_section,
                        "label": sec_clean,
                        "is_field": False,
                        "modifiable": False,
                        "type": "section_header",
                        "value": sec_clean,
                    }
                    if fn_id:
                        entry["_fn_ref"] = fn_id
                    items.append(entry)

                elif sub_m and (vl["bold"] or vl["x0"] < page_w * 0.25) and len(txt) < 120:
                    sub_clean, fn_id = split_label_and_footnote(txt)
                    current_subsection = sub_clean
                    entry = {
                        "field_id": slugify(sub_clean),
                        "section": current_section,
                        "subsection": current_subsection,
                        "label": sub_clean,
                        "is_field": False,
                        "modifiable": False,
                        "type": "subsection_header",
                        "value": sub_clean,
                    }
                    if fn_id:
                        entry["_fn_ref"] = fn_id
                    items.append(entry)

            # B. Checkbox row (e.g., Pan [ ]  Pani [ ] 2))
            elif ev_type == "cb_row":
                row_cbs = payload
                cy = (row_cbs[0][1] + row_cbs[0][3]) / 2

                # Check if a footnote marker like "2)" appears at the end of this checkbox row
                row_fn_id = None
                for sp in spans_list:
                    sy = (sp["y0"] + sp["y1"]) / 2
                    if abs(sy - cy) < 12 and sp["x0"] >= row_cbs[-1][2] - 2:
                        m_fn = re.match(r"^(\d+)\)$", sp["text"])
                        if m_fn:
                            row_fn_id = m_fn.group(1)

                prev_cb_right = -1.0
                for cx0, cy0, cx1, cy1 in row_cbs:
                    best_span = None
                    best_dist = 999
                    for sp in spans_list:
                        sy = (sp["y0"] + sp["y1"]) / 2
                        if abs(sy - cy) < 12 and prev_cb_right - 2 <= sp["x0"] and sp["x1"] <= cx0 + 6:
                            if not re.match(r"^\d+\)$", sp["text"]):
                                dist = cx0 - sp["x1"]
                                if dist < best_dist:
                                    best_dist = dist
                                    best_span = sp
                    if best_span:
                        lbl, fn_id = split_label_and_footnote(best_span["text"])
                        entry = {
                            "field_id": slugify(lbl),
                            "section": current_section,
                            "subsection": current_subsection,
                            "label": lbl,
                            "is_field": True,
                            "modifiable": True,
                            "type": "checkbox",
                            "value": False,
                        }
                        if fn_id or row_fn_id:
                            entry["_fn_ref"] = fn_id or row_fn_id
                        items.append(entry)
                    prev_cb_right = cx1

            # C. Table rows
            elif ev_type == "table_row":
                y_top, y_bot, xs = payload
                row_h = y_bot - y_top
                clustered_xs = []
                for x in xs:
                    if not clustered_xs or abs(clustered_xs[-1] - x) > 2.5:
                        clustered_xs.append(x)

                cells = []
                cell_lines_list = []
                for j in range(len(clustered_xs) - 1):
                    xl, xr = clustered_xs[j], clustered_xs[j + 1]
                    inside = [
                        vl for vl in visual_lines
                        if xl - 2 <= (vl["x0"] + vl["x1"]) / 2 <= xr + 2
                        and y_top - 1 <= (vl["y0"] + vl["y1"]) / 2 <= y_bot + 1
                    ]
                    inside.sort(key=lambda v: (round(v["y0"], 1), v["x0"]))
                    cell_lines_list.append(inside)
                    cells.append(" ".join(v["text"] for v in inside).strip())

                # Skip attachment checklist rows (Lp. | Nazwa dokumentu | 1..20)
                if cells[0] in {"Lp.", "Lp"} or cells[0].isdigit():
                    continue

                # Check for section banner inside a table cell (e.g. "III. Dane dotyczące zawodu:")
                for c_txt in cells:
                    m_sec = re.match(r"^([IVX]+\.\s+[^:]+):", c_txt)
                    if m_sec:
                        sec_clean, fn_id = split_label_and_footnote(m_sec.group(1))
                        current_section = sec_clean
                        current_subsection = None
                        entry = {
                            "field_id": slugify(sec_clean),
                            "section": current_section,
                            "label": sec_clean,
                            "is_field": False,
                            "modifiable": False,
                            "type": "section_header",
                            "value": sec_clean,
                        }
                        if fn_id:
                            entry["_fn_ref"] = fn_id
                        items.append(entry)

                def make_field(raw_lbl: str, sub_override: str | None = None) -> dict:
                    clean_lbl, fn_id = split_label_and_footnote(raw_lbl)
                    fld = {
                        "field_id": slugify(clean_lbl),
                        "section": current_section,
                        "subsection": sub_override if sub_override is not None else current_subsection,
                        "label": clean_lbl,
                        "is_field": True,
                        "modifiable": True,
                        "type": "text",
                        "value": None,
                    }
                    if fn_id:
                        fld["_fn_ref"] = fn_id
                    return fld

                def extract_stacked_fields(lines_in_cell: list[dict], sub_override: str | None = None):
                    """
                    Splits tall cells containing multiple distinct input labels separated by
                    blank writing space (e.g., E-mail / Numer telefonu / Numer faksu).
                    """
                    if len(lines_in_cell) <= 1:
                        if lines_in_cell:
                            items.append(make_field(lines_in_cell[0]["text"], sub_override))
                        return

                    # Group lines that are vertically close (< 18pt) as a single wrapped label;
                    # split whenever vertical gap >= 18pt (blank space left for handwriting).
                    groups = [[lines_in_cell[0]]]
                    for prev_l, curr_l in zip(lines_in_cell, lines_in_cell[1:]):
                        gap = curr_l["y0"] - prev_l["y1"]
                        if gap >= 18.0:
                            groups.append([curr_l])
                        else:
                            groups[-1].append(curr_l)

                    for grp in groups:
                        combined = " ".join(g["text"] for g in grp)
                        items.append(make_field(combined, sub_override))

                if len(cells) == 1 and cells[0]:
                    if not re.match(r"^[IVX]+\.\s+", cells[0]):
                        extract_stacked_fields(cell_lines_list[0])
                    pending_col_headers = None

                elif len(cells) == 2:
                    left, right = cells
                    left_w = clustered_xs[1] - clustered_xs[0]

                    # Pattern 1: Left label, right empty box (standard 2-column form row)
                    if left and not right:
                        items.append(make_field(left))

                    # Pattern 2: Right box has label(s) + writing space below, while left column
                    # is either empty (rowspan continuation) or a descriptive group label
                    elif right:
                        group_sub = current_subsection
                        if left and left_w > 180 and not re.match(r"^[IVX]+\.\s+", left):
                            grp_clean, fn_id = split_label_and_footnote(left)
                            group_sub = grp_clean
                            grp_entry = {
                                "field_id": slugify(grp_clean),
                                "section": current_section,
                                "subsection": grp_clean,
                                "label": grp_clean,
                                "is_field": False,
                                "modifiable": False,
                                "type": "group_header",
                                "value": grp_clean,
                            }
                            if fn_id:
                                grp_entry["_fn_ref"] = fn_id
                            items.append(grp_entry)

                        extract_stacked_fields(cell_lines_list[1], sub_override=group_sub)

                elif len(cells) >= 3:
                    non_empty = [c for c in cells if c]
                    if non_empty:
                        pending_col_headers = cells
                    elif pending_col_headers:
                        for col_label in pending_col_headers:
                            if col_label:
                                items.append(make_field(col_label))
                        pending_col_headers = None

    doc.close()

    # Attach resolved footnotes to their corresponding items
    for entry in items:
        fn_id = entry.pop("_fn_ref", None)
        if fn_id and fn_id in footnotes_map:
            entry["note"] = footnotes_map[fn_id]

    return {
        "source_file": file_path.name,
        "format": "pdf",
        "form_title": " ".join(title_lines) if title_lines else None,
        "bureau_info": extract_bureau_info("\n".join(full_text_pages)),
        "items": items,
        "notes": [f"{k}) {v}" for k, v in sorted(footnotes_map.items(), key=lambda x: int(x[0]))],
    }