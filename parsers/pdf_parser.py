import re
from pathlib import Path
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer, LTRect, LTCurve, LTChar
from extractors import extract_bureau_info, slugify


def decode_cid_text(text: str) -> str:
    """Decodes pdfminer '(cid:N)' glyph IDs used by Polish government PDF generators."""
    def repl(match):
        cid = int(match.group(1))
        if cid == 3:
            return " "
        if 4 <= cid <= 226:
            byte_val = cid + 29
            if 32 <= byte_val <= 255:
                try:
                    return bytes([byte_val]).decode("cp1250")
                except Exception:
                    pass
        cid_map = {
            252: "ć", 261: "ą", 266: "ę", 276: "ń", 286: "ś",
            298: "ż", 225: "ł", 121: "ó", 177: "–", 171: ".",
            207: "Ó", 222: "Ł", 248: "Ś", 238: "Ż", 243: "Ź",
        }
        return cid_map.get(cid, "")

    return re.sub(r"\(cid:(\d+)\)", repl, text)


def parse_pdf(file_path: Path) -> dict:
    title_lines = []
    full_text_pages = []
    fields = []
    current_section = None
    current_subsection = None

    section_re = re.compile(r"^([IVX]+\.\s+.+)$")
    subsec_re = re.compile(r"^(\d+\.\s+[^\d].+)$")

    for page in extract_pages(file_path):
        page_w = page.width
        h_lines = []
        v_lines = []
        checkbox_rects = []
        text_boxes = []

        def walk(el):
            if isinstance(el, LTRect):
                if el.height < 2.5 and el.width > 15.0:
                    h_lines.append(((el.y0 + el.y1) / 2, el.x0, el.x1))
                elif el.width < 2.5 and el.height > 10.0:
                    v_lines.append(((el.x0 + el.x1) / 2, el.y0, el.y1))
            elif isinstance(el, LTCurve):
                if 10.0 <= el.width <= 30.0 and 10.0 <= el.height <= 30.0:
                    checkbox_rects.append((el.x0, el.y0, el.x1, el.y1))
            elif isinstance(el, LTTextContainer):
                raw_t = decode_cid_text(el.get_text())
                clean_t = " ".join(raw_t.split())
                if clean_t:
                    is_bold = False
                    max_size = 0.0
                    for line in el:
                        if hasattr(line, "__iter__"):
                            for ch in line:
                                if isinstance(ch, LTChar):
                                    if "bold" in ch.fontname.lower():
                                        is_bold = True
                                    if ch.size > max_size:
                                        max_size = ch.size
                    text_boxes.append({
                        "x0": el.x0, "y0": el.y0, "x1": el.x1, "y1": el.y1,
                        "text": clean_t, "raw": raw_t, "bold": is_bold, "size": max_size,
                    })
            if hasattr(el, "__iter__") and not isinstance(el, LTTextContainer):
                for child in el:
                    walk(child)

        walk(page)
        text_boxes.sort(key=lambda b: (-round(b["y1"], 1), b["x0"]))
        full_text_pages.append("\n".join(b["raw"] for b in text_boxes))

        # 1. Centered bold title on page 1 (above the first table)
        first_table_y = max((y for y, _, _ in h_lines if y < page.height * 0.8), default=0)
        if page.pageid == 1:
            for b in text_boxes:
                if b["y0"] < first_table_y:
                    continue
                center_x = (b["x0"] + b["x1"]) / 2
                is_centered = abs(center_x - page_w / 2) < page_w * 0.18
                if is_centered and (b["bold"] or b["size"] >= 11.5):
                    upper = b["text"].upper().replace(" ", "")
                    if not any(skip in upper for skip in ["DZIENNIKUSTAW", "WZÓR", "ZAŁĄCZNIK"]):
                        clean_part = re.sub(r"\d+\)$", "", b["text"]).strip()
                        title_lines.append(clean_part)

        # 2. Track Roman-numeral sections & numbered subsections
        for b in text_boxes:
            if b["bold"]:
                if section_re.match(b["text"]):
                    current_section = b["text"].rstrip(":")
                    current_subsection = None
                elif subsec_re.match(b["text"]) and len(b["text"]) < 100:
                    current_subsection = b["text"].rstrip(":")

        # 3. Vector square checkboxes (e.g., Pan [ ], Pani [ ])
        dedup_cbs = []
        for cb in sorted(checkbox_rects):
            if not dedup_cbs or abs(dedup_cbs[-1][0] - cb[0]) > 4 or abs(dedup_cbs[-1][1] - cb[1]) > 4:
                dedup_cbs.append(cb)

        for cx0, cy0, cx1, cy1 in dedup_cbs:
            cy = (cy0 + cy1) / 2
            best_label = None
            best_dist = 999
            for b in text_boxes:
                by = (b["y0"] + b["y1"]) / 2
                if abs(by - cy) < 14:
                    # Find closest word to the left of the checkbox
                    if b["x1"] <= cx0 + 6:
                        dist = cx0 - b["x1"]
                        words = [w for w in b["text"].split() if not w.endswith(":")]
                        if words:
                            cand = words[-1]
                            if dist < best_dist and not re.match(r"^\d+\)$", cand):
                                best_dist = dist
                                best_label = cand
            if best_label:
                fields.append({
                    "field_id": slugify(best_label),
                    "section": current_section,
                    "label": best_label,
                    "type": "checkbox",
                    "value": False,
                })

        # 4. Reconstruct grid boxes from horizontal & vertical borders
        ys = sorted({round(y, 1) for y, _, _ in h_lines}, reverse=True)
        clustered_ys = []
        for y in ys:
            if not clustered_ys or abs(clustered_ys[-1] - y) > 2.5:
                clustered_ys.append(y)

        pending_col_headers = None

        for i in range(len(clustered_ys) - 1):
            y_top, y_bot = clustered_ys[i], clustered_ys[i + 1]
            row_h = y_top - y_bot
            if row_h < 8 or row_h > 250:
                continue

            xs = sorted({round(x, 1) for x, vy0, vy1 in v_lines if vy0 <= y_bot + 3 and vy1 >= y_top - 3})
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
                    and y_bot - 1 <= (b["y0"] + b["y1"]) / 2 <= y_top + 1
                ]
                inside.sort(key=lambda b: (-round(b["y1"], 1), b["x0"]))
                cells.append(" ".join(b["text"] for b in inside).strip())

            # Skip numbered attachment list tables (Lp. | Nazwa dokumentu)
            if cells[0] in {"Lp.", "Lp"} or cells[0].isdigit():
                continue

            for c_txt in cells:
                m_sec = re.match(r"^([IVX]+\.\s+[^:]+):", c_txt)
                if m_sec:
                    current_section = m_sec.group(1)

            # Single full-width box with label at top and blank space below
            if len(cells) == 1 and cells[0]:
                label = re.sub(r"\d+\)$", "", cells[0]).strip()
                fields.append({
                    "field_id": slugify(label),
                    "section": current_section,
                    "subsection": current_subsection,
                    "label": label,
                    "type": "text",
                    "value": None,
                })
                pending_col_headers = None

            # 2-column row: [Label | Empty Input Box]
            elif len(cells) == 2:
                left, right = cells
                if left and not right:
                    label = re.sub(r"\d+\)$", "", left).strip()
                    fields.append({
                        "field_id": slugify(label),
                        "section": current_section,
                        "subsection": current_subsection,
                        "label": label,
                        "type": "text",
                        "value": None,
                    })
                elif right and not left:
                    label = re.sub(r"\d+\)$", "", right).strip()
                    fields.append({
                        "field_id": slugify(label),
                        "section": current_section,
                        "subsection": current_subsection,
                        "label": label,
                        "type": "text",
                        "value": None,
                    })

            # Multi-column table (3+ columns: e.g., Name | Państwo | Miejscowość | Data)
            elif len(cells) >= 3:
                non_empty = [c for c in cells if c]
                if non_empty:
                    pending_col_headers = [re.sub(r"\d+\)$", "", c).strip() for c in cells]
                elif pending_col_headers:
                    for col_label in pending_col_headers:
                        if col_label:
                            fields.append({
                                "field_id": slugify(col_label),
                                "section": current_section,
                                "subsection": current_subsection,
                                "label": col_label,
                                "type": "text",
                                "value": None,
                            })
                    pending_col_headers = None

    return {
        "source_file": file_path.name,
        "format": "pdf",
        "form_title": " ".join(title_lines) if title_lines else None,
        "bureau_info": extract_bureau_info("\n".join(full_text_pages)),
        "fields": fields,
    }