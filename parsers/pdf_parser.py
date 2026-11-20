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
    title_fn_ref = None
    full_text_pages = []
    items = []

    # Strictly per-page footnotes: footnotes_by_page[page_idx][fn_id] = text
    footnotes_by_page: dict[int, dict[str, str]] = {}
    footnotes_global_fallback: dict[str, str] = {}

    current_section = None
    current_subsection = None
    in_attachment_checklist = False

    section_re = re.compile(r"^([IVX]+\.\s+.+)$")
    subsec_re = re.compile(r"^(\d+\.\s+[^\d].+)$")
    footnote_line_re = re.compile(r"^(\d+)\)\s*(.*)$")

    for page_idx, page in enumerate(doc):
        page_w = page.rect.width
        page_h = page.rect.height
        footnotes_by_page[page_idx] = {}

        # 1. Extract vector lines and square checkboxes
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

        # 2. Extract spans & build visual lines
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
                    base_y = s["origin"][1] if "origin" in s else y1
                    spans_list.append({
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "base_y": base_y,
                        "text": clean_s,
                        "bold": is_bold,
                        "size": s["size"],
                    })

        spans_list.sort(key=lambda s: (round(s["base_y"] / 4.0), s["x0"]))

        visual_lines = []
        for sp in spans_list:
            if visual_lines and abs(visual_lines[-1]["base_y"] - sp["base_y"]) < 5.0:
                prev = visual_lines[-1]
                is_super = sp["size"] < prev["size"] * 0.88 and re.match(r"^\d+\)$", sp["text"])
                sep = "" if is_super else " "
                prev["text"] = f"{prev['text']}{sep}{sp['text']}"
                prev["x1"] = max(prev["x1"], sp["x1"])
                prev["y0"] = min(prev["y0"], sp["y0"])
                prev["y1"] = max(prev["y1"], sp["y1"])
                prev["bold"] = prev["bold"] or sp["bold"]
                prev["spans"].append(sp)
            else:
                visual_lines.append({
                    "x0": sp["x0"], "y0": sp["y0"], "x1": sp["x1"], "y1": sp["y1"],
                    "base_y": sp["base_y"],
                    "text": sp["text"],
                    "bold": sp["bold"],
                    "size": sp["size"],
                    "spans": [sp],
                })

        visual_lines.sort(key=lambda v: (round(v["y0"], 1), v["x0"]))

        # 3. Locate the short horizontal footnote separator line on THIS page
        short_sep_ys = [
            y for y, x0, x1 in h_lines
            if y > page_h * 0.45 and 30.0 <= (x1 - x0) <= 250.0 and x0 < page_w * 0.30
        ]
        if short_sep_ys:
            footnote_sep_y = max(short_sep_ys)
        else:
            full_table_ys = [y for y, x0, x1 in h_lines if (x1 - x0) > page_w * 0.45 and y < page_h * 0.90]
            footnote_sep_y = (max(full_table_ys) + 2.0) if full_table_ys else (page_h * 0.68)

        # 4. Collect footnotes strictly for THIS page (page_idx).
        # Top-level footnote markers (1), 2), 3), 4), 7)) are superscripts at the left margin (~x0=71, size < 8.5),
        # whereas nested sub-points inside a footnote (e.g. 1), 2), 3) inside 7)) are normal-size & indented.
        active_fn_id: str | None = None
        top_level_fn_x0: float | None = None

        for vl in visual_lines:
            if vl["y0"] < footnote_sep_y - 2.0:
                continue

            txt = vl["text"].strip()
            if (
                txt.startswith("©")
                or "Dziennik Ustaw" in txt
                or re.match(r"^-?\s*\d+\s*-?$", txt)
                or txt.startswith("Poz.")
                or txt.startswith("Załącznik nr")
            ):
                continue

            first_span = vl["spans"][0]
            fn_m = footnote_line_re.match(txt)

            if fn_m:
                num = fn_m.group(1)
                body = fn_m.group(2).strip()
                is_first_span_super = first_span["size"] < 8.5

                if top_level_fn_x0 is None and is_first_span_super:
                    top_level_fn_x0 = first_span["x0"]

                is_top_level = (
                    is_first_span_super
                    and top_level_fn_x0 is not None
                    and abs(first_span["x0"] - top_level_fn_x0) < 8.0
                )

                if is_top_level:
                    active_fn_id = num
                    footnotes_by_page[page_idx][active_fn_id] = body
                    footnotes_global_fallback.setdefault(active_fn_id, body)
                elif active_fn_id is not None:
                    # Nested sub-point (e.g., 1), 2), 3) inside 7)) -> fold into active_fn_id as a single line
                    existing = footnotes_by_page[page_idx][active_fn_id]
                    sep = " " if existing else ""
                    footnotes_by_page[page_idx][active_fn_id] = f"{existing}{sep}{num}) {body}".strip()
                    footnotes_global_fallback[active_fn_id] = footnotes_by_page[page_idx][active_fn_id]
            elif active_fn_id is not None:
                prev_txt = footnotes_by_page[page_idx][active_fn_id]
                if not prev_txt:
                    footnotes_by_page[page_idx][active_fn_id] = txt
                elif prev_txt.endswith("-") and not prev_txt.endswith(" –"):
                    footnotes_by_page[page_idx][active_fn_id] = prev_txt[:-1] + txt
                else:
                    footnotes_by_page[page_idx][active_fn_id] = prev_txt + " " + txt
                footnotes_global_fallback[active_fn_id] = footnotes_by_page[page_idx][active_fn_id]

        # 5. Build table rows above footnote_sep_y
        ys = sorted({round(y, 1) for y, x0, x1 in h_lines if y < footnote_sep_y - 2.0 and (x1 - x0) > 60.0})
        clustered_ys = []
        for y in ys:
            if not clustered_ys or abs(clustered_ys[-1] - y) > 2.5:
                clustered_ys.append(y)

        table_bands = []
        for i in range(len(clustered_ys) - 1):
            y_top, y_bot = clustered_ys[i], clustered_ys[i + 1]
            row_h = y_bot - y_top
            if 8 <= row_h <= 280:
                xs = sorted({
                    round(x, 1)
                    for x, vy0, vy1 in v_lines
                    if (min(vy1, y_bot) - max(vy0, y_top)) >= row_h * 0.5
                })
                if len(xs) >= 2:
                    table_bands.append((y_top, y_bot, xs))

        def is_inside_any_table_row(y_mid: float) -> bool:
            return any(yt - 1.5 <= y_mid <= yb + 1.5 for yt, yb, _ in table_bands)

        # 6. Extract Page 1 Title
        first_table_y = table_bands[0][0] if table_bands else footnote_sep_y
        if page_idx == 0:
            for vl in visual_lines:
                if vl["y1"] > first_table_y:
                    continue
                center_x = (vl["x0"] + vl["x1"]) / 2
                is_centered = abs(center_x - page_w / 2) < page_w * 0.25
                if is_centered and (vl["bold"] or vl["size"] >= 11.5):
                    upper = vl["text"].upper().replace(" ", "")
                    if not any(skip in upper for skip in ["DZIENNIKUSTAW", "ZAŁĄCZNIK", "POZ."]):
                        clean_t, fn_id = split_label_and_footnote(vl["text"])
                        title_lines.append(clean_t)
                        if fn_id:
                            title_fn_ref = (page_idx, fn_id)

        # Deduplicate vector checkboxes
        dedup_cbs = []
        for cb in sorted(checkbox_rects, key=lambda r: (round(r[1], 1), r[0])):
            if not dedup_cbs or abs(dedup_cbs[-1][0] - cb[0]) > 4 or abs(dedup_cbs[-1][1] - cb[1]) > 4:
                dedup_cbs.append(cb)

        # 7. Unified top-to-bottom timeline of events on this page
        events = []

        for vl in visual_lines:
            if vl["y0"] >= footnote_sep_y - 2.0:
                continue
            y_mid = (vl["y0"] + vl["y1"]) / 2
            if not is_inside_any_table_row(y_mid):
                events.append((vl["y0"], 0, "line", vl))

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

        for _, _, ev_type, payload in events:
            # A. Standalone lines outside tables
            if ev_type == "line":
                vl = payload
                txt = vl["text"]
                if title_lines and any(txt.startswith(t) for t in title_lines):
                    continue
                if (
                    txt.startswith("©")
                    or "Dziennik Ustaw" in txt
                    or re.match(r"^-?\s*\d+\s*-?$", txt)
                    or txt.startswith("Poz.")
                    or txt.startswith("Załącznik nr")
                ):
                    continue

                sec_m = section_re.match(txt)
                sub_m = subsec_re.match(txt)

                if sec_m and vl["bold"]:
                    sec_clean, fn_id = split_label_and_footnote(txt)
                    current_section = sec_clean
                    current_subsection = None
                    in_attachment_checklist = sec_clean.startswith("VI.")

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
                        entry["_fn_ref"] = (page_idx, fn_id)
                    items.append(entry)

                    if sec_clean.startswith("V."):
                        table_fld = {
                            "field_id": "wykaz_dokumentow_dolaczonych",
                            "section": current_section,
                            "subsection": None,
                            "label": "Nazwa dokumentu (lista załączników)",
                            "is_field": True,
                            "modifiable": True,
                            "type": "repeatable_text",
                            "value": [],
                        }
                        if fn_id:
                            table_fld["_fn_ref"] = (page_idx, fn_id)
                        items.append(table_fld)

                elif in_attachment_checklist:
                    if sub_m:
                        item_clean, fn_id = split_label_and_footnote(txt)
                        entry = {
                            "field_id": slugify(item_clean),
                            "section": current_section,
                            "subsection": None,
                            "label": item_clean,
                            "is_field": False,
                            "modifiable": False,
                            "type": "checklist_item",
                            "value": item_clean,
                        }
                        if fn_id:
                            entry["_fn_ref"] = (page_idx, fn_id)
                        items.append(entry)
                    elif items and items[-1].get("type") == "checklist_item":
                        cont_clean, fn_id = split_label_and_footnote(txt)
                        items[-1]["label"] += " " + cont_clean
                        items[-1]["value"] += " " + cont_clean
                        if fn_id:
                            items[-1]["_fn_ref"] = (page_idx, fn_id)

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
                        entry["_fn_ref"] = (page_idx, fn_id)
                    items.append(entry)

            # B. Checkbox row (e.g., Pan [ ]  Pani [ ] 2))
            elif ev_type == "cb_row":
                row_cbs = payload
                cy = (row_cbs[0][1] + row_cbs[0][3]) / 2

                row_fn_id = None
                for sp in spans_list:
                    sy = (sp["y0"] + sp["y1"]) / 2
                    if abs(sy - cy) < 12 and sp["x0"] >= row_cbs[-1][2] - 2:
                        m_fn = re.match(r"^(\d+)\)$", sp["text"])
                        if m_fn:
                            row_fn_id = m_fn.group(1)

                word_tokens = []
                for sp in spans_list:
                    sy = (sp["y0"] + sp["y1"]) / 2
                    if abs(sy - cy) < 12 and not re.match(r"^\d+\)$", sp["text"]):
                        parts = sp["text"].split()
                        if len(parts) == 1:
                            word_tokens.append((sp["x0"], sp["x1"], parts[0]))
                        else:
                            span_w = max(sp["x1"] - sp["x0"], 1.0)
                            step = span_w / len(parts)
                            for idx_w, w_txt in enumerate(parts):
                                wx0 = sp["x0"] + idx_w * step
                                wx1 = wx0 + step
                                word_tokens.append((wx0, wx1, w_txt))

                prev_cb_right = -1.0
                for cx0, cy0, cx1, cy1 in row_cbs:
                    best_word = None
                    best_dist = 999
                    for wx0, wx1, w_txt in word_tokens:
                        if prev_cb_right - 2 <= wx0 and wx1 <= cx0 + 8:
                            dist = cx0 - wx1
                            if dist < best_dist:
                                best_dist = dist
                                best_word = w_txt
                    if best_word:
                        lbl, fn_id = split_label_and_footnote(best_word)
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
                        resolved_id = fn_id or row_fn_id
                        if resolved_id:
                            entry["_fn_ref"] = (page_idx, resolved_id)
                        items.append(entry)
                    prev_cb_right = cx1

            # C. Table rows
            elif ev_type == "table_row":
                y_top, y_bot, xs = payload
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

                if cells[0] in {"Lp.", "Lp"} or cells[0].isdigit() or (current_section and current_section.startswith("V.")):
                    continue

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
                        fld["_fn_ref"] = (page_idx, fn_id)
                    return fld

                def extract_stacked_fields(lines_in_cell: list[dict], sub_override: str | None = None):
                    if len(lines_in_cell) <= 1:
                        if lines_in_cell:
                            items.append(make_field(lines_in_cell[0]["text"], sub_override))
                        return

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

                elif len(cells) == 2:
                    left, right = cells
                    left_w = clustered_xs[1] - clustered_xs[0]

                    if left and not right:
                        items.append(make_field(left))

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
                                grp_entry["_fn_ref"] = (page_idx, fn_id)
                            items.append(grp_entry)

                        extract_stacked_fields(cell_lines_list[1], sub_override=group_sub)

                elif len(cells) >= 3:
                    for c_txt in cells:
                        if c_txt:
                            items.append(make_field(c_txt))

    doc.close()

    def resolve_footnote(ref: tuple[int, str] | None) -> str | None:
        if not ref:
            return None
        p_idx, fn_id = ref
        # 1. Strictly look on the SAME page first
        page_notes = footnotes_by_page.get(p_idx, {})
        if fn_id in page_notes:
            return page_notes[fn_id]
        # 2. Only fall back to other pages if missing on p_idx
        return footnotes_global_fallback.get(fn_id)

    for entry in items:
        desc = resolve_footnote(entry.pop("_fn_ref", None))
        if desc:
            entry["description"] = desc

    all_notes = []
    for p_idx in sorted(footnotes_by_page.keys()):
        for fn_id, txt in footnotes_by_page[p_idx].items():
            all_notes.append(f"{fn_id}) {txt}")

    result = {
        "source_file": file_path.name,
        "format": "pdf",
        "form_title": " ".join(title_lines) if title_lines else None,
        "bureau_info": extract_bureau_info("\n".join(full_text_pages)),
        "items": items,
        "notes": all_notes,
    }

    title_desc = resolve_footnote(title_fn_ref)
    if title_desc:
        result["form_description"] = title_desc

    return result