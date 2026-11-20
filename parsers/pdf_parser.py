import re
from pathlib import Path
import fitz  # PyMuPDF
from extractors import extract_bureau_info, normalize_polish_pdf_text, slugify


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
    footnote_line_re = re.compile(r"^(\d+)\)\s+(.+)$")

    for page_idx, page in enumerate(doc):
        page_w = page.rect.width
        page_h = page.rect.height

        # 1. Extract vector drawings: horizontal/vertical table borders and square checkboxes
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

        # 2. Extract text spans with font flags and coordinates
        raw_page_text = normalize_polish_pdf_text(page.get_text("text"))
        full_text_pages.append(raw_page_text)

        text_boxes = []
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if "lines" not in block:
                continue
            for line in block["lines"]:
                spans = line["spans"]
                raw_t = "".join(s["text"] for s in spans)
                clean_t = normalize_polish_pdf_text(" ".join(raw_t.split()))
                if not clean_t:
                    continue

                is_bold = any(
                    "bold" in s["font"].lower() or bool(s["flags"] & 16)
                    for s in spans
                )
                max_size = max((s["size"] for s in spans), default=0.0)
                x0, y0, x1, y1 = line["bbox"]

                text_boxes.append({
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "text": clean_t, "bold": is_bold, "size": max_size,
                })

        # Sort top-to-bottom (PyMuPDF y=0 is top of page), then left-to-right
        text_boxes.sort(key=lambda b: (round(b["y0"], 1), b["x0"]))

        # Collect bottom-of-page footnotes like "2) Zaznaczyć właściwe."
        for b in text_boxes:
            if b["y0"] > page_h * 0.72:
                fn_m = footnote_line_re.match(b["text"])
                if fn_m:
                    footnotes_map[fn_m.group(1)] = fn_m.group(2).strip()

        # 3. Centered bold title on Page 1
        first_table_y = min((y for y, _, _ in h_lines if y > page_h * 0.12), default=page_h)
        if page_idx == 0:
            for b in text_boxes:
                if b["y1"] > first_table_y:
                    continue
                center_x = (b["x0"] + b["x1"]) / 2
                is_centered = abs(center_x - page_w / 2) < page_w * 0.22
                if is_centered and (b["bold"] or b["size"] >= 11.5):
                    upper = b["text"].upper().replace(" ", "")
                    if not any(skip in upper for skip in ["DZIENNIKUSTAW", "ZAŁĄCZNIK", "POZ."]):
                        clean_part = re.sub(r"\d+\)$", "", b["text"]).strip()
                        title_lines.append(clean_part)

        # 4. Track Sections (I., II., ...) and Subsections (1. Wykształcenie...) as is_field=False
        for b in text_boxes:
            # Skip table interior text here; handled in grid reconstruction
            in_table = any(
                y_t - 2 <= (b["y0"] + b["y1"]) / 2 <= y_b + 2
                for y_t, y_b in [(min(y for y, _, _ in h_lines), max(y for y, _, _ in h_lines))]
            ) if h_lines else False

            if b["bold"] and not in_table:
                sec_m = section_re.match(b["text"])
                sub_m = subsec_re.match(b["text"])
                if sec_m:
                    fn_ref = re.search(r"(\d+)\)$", b["text"])
                    sec_clean = re.sub(r"\d+\)$", "", b["text"]).rstrip(":").strip()
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
                    if fn_ref:
                        entry["_fn_ref"] = fn_ref.group(1)
                    items.append(entry)
                elif sub_m and len(b["text"]) < 110:
                    sub_clean = re.sub(r"\d+\)$", "", b["text"]).rstrip(":").strip()
                    current_subsection = sub_clean
                    items.append({
                        "field_id": slugify(sub_clean),
                        "section": current_section,
                        "subsection": current_subsection,
                        "label": sub_clean,
                        "is_field": False,
                        "modifiable": False,
                        "type": "subsection_header",
                        "value": sub_clean,
                    })

        # 5. Vector square checkboxes (e.g. Pan [ ], Pani [ ])
        dedup_cbs = []
        for cb in sorted(checkbox_rects):
            if not dedup_cbs or abs(dedup_cbs[-1][0] - cb[0]) > 4 or abs(dedup_cbs[-1][1] - cb[1]) > 4:
                dedup_cbs.append(cb)

        for cx0, cy0, cx1, cy1 in dedup_cbs:
            cy = (cy0 + cy1) / 2
            best_label = None
            best_dist = 999
            fn_ref = None
            for b in text_boxes:
                by = (b["y0"] + b["y1"]) / 2
                if abs(by - cy) < 14:
                    # Check if a footnote marker like "2)" sits just to the right of the checkbox
                    if b["x0"] >= cx1 - 2 and re.match(r"^\d+\)$", b["text"]):
                        fn_ref = b["text"].rstrip(")")
                    if b["x1"] <= cx0 + 6:
                        dist = cx0 - b["x1"]
                        words = [w for w in b["text"].split() if not w.endswith(":")]
                        if words:
                            cand = words[-1]
                            if dist < best_dist and not re.match(r"^\d+\)$", cand):
                                best_dist = dist
                                best_label = cand
            if best_label:
                entry = {
                    "field_id": slugify(best_label),
                    "section": current_section,
                    "label": best_label,
                    "is_field": True,
                    "modifiable": True,
                    "type": "checkbox",
                    "value": False,
                }
                if fn_ref:
                    entry["_fn_ref"] = fn_ref
                items.append(entry)

        # 6. Reconstruct grid boxes from horizontal & vertical lines
        ys = sorted({round(y, 1) for y, _, _ in h_lines})
        clustered_ys = []
        for y in ys:
            if not clustered_ys or abs(clustered_ys[-1] - y) > 2.5:
                clustered_ys.append(y)

        pending_col_headers = None

        for i in range(len(clustered_ys) - 1):
            y_top, y_bot = clustered_ys[i], clustered_ys[i + 1]
            row_h = y_bot - y_top
            if row_h < 8 or row_h > 250:
                continue

            xs = sorted({round(x, 1) for x, vy0, vy1 in v_lines if vy0 <= y_top + 3 and vy1 >= y_bot - 3})
            clustered_xs = []
            for x in xs:
                if not clustered_xs or abs(clustered_xs[-1] - x) > 2.5:
                    clustered_xs.append(x)

            if len(clustered_xs) < 2:
                continue

            cells = []
            for j in range(len(clustered_xs) - 1):
                xl, xr = clustered_xs[j], clustered_xs[j + 1]
                inside = [
                    b for b in text_boxes
                    if xl - 2 <= (b["x0"] + b["x1"]) / 2 <= xr + 2
                    and y_top - 1 <= (b["y0"] + b["y1"]) / 2 <= y_bot + 1
                ]
                inside.sort(key=lambda b: (round(b["y0"], 1), b["x0"]))
                cells.append(" ".join(b["text"] for b in inside).strip())

            # Skip attachment checklist rows (Lp. | Nazwa dokumentu | 1..20)
            if cells[0] in {"Lp.", "Lp"} or cells[0].isdigit():
                continue

            # Detect section headers embedded inside a table banner
            for c_txt in cells:
                m_sec = re.match(r"^([IVX]+\.\s+[^:]+):", c_txt)
                if m_sec:
                    current_section = m_sec.group(1).strip()
                    items.append({
                        "field_id": slugify(current_section),
                        "section": current_section,
                        "label": current_section,
                        "is_field": False,
                        "modifiable": False,
                        "type": "section_header",
                        "value": current_section,
                    })

            def build_field(raw_label: str) -> dict:
                fn_m = re.search(r"(\d+)\)$", raw_label)
                clean_lbl = re.sub(r"\d+\)$", "", raw_label).strip()
                fld = {
                    "field_id": slugify(clean_lbl),
                    "section": current_section,
                    "subsection": current_subsection,
                    "label": clean_lbl,
                    "is_field": True,
                    "modifiable": True,
                    "type": "text",
                    "value": None,
                }
                if fn_m:
                    fld["_fn_ref"] = fn_m.group(1)
                return fld

            if len(cells) == 1 and cells[0]:
                items.append(build_field(cells[0]))
                pending_col_headers = None

            elif len(cells) == 2:
                left, right = cells
                if left and not right:
                    items.append(build_field(left))
                elif right and not left:
                    items.append(build_field(right))

            elif len(cells) >= 3:
                non_empty = [c for c in cells if c]
                if non_empty:
                    pending_col_headers = cells
                elif pending_col_headers:
                    for col_label in pending_col_headers:
                        if col_label:
                            items.append(build_field(col_label))
                    pending_col_headers = None

    doc.close()

    # Resolve footnote references onto their corresponding items
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
        "notes": list(footnotes_map.values()),
    }