import re


def _decode_shifted_glyphs(text: str) -> str:
    """
    Decodes Polish government PDFs whose embedded subset fonts emit raw glyph IDs
    shifted by -29, while preserving standard ASCII space (0x20 = 32) used in
    justified lines.
    """
    high_glyph_map = {
        121: "ó",
        171: ".",
        177: "–",
        193: "„",
        207: "Ó",
        222: "Ł",
        225: "ł",
        238: "Ż",
        243: "Ź",
        248: "Ś",
        252: "ć",
        261: "ą",
        266: "ę",
        276: "ń",
        286: "ś",
        298: "ż",
        312: "”",
    }

    out = []
    for ch in text:
        code = ord(ch)
        if code in high_glyph_map:
            out.append(high_glyph_map[code])
        elif code in (3, 32):
            out.append(" ")
        elif 4 <= code <= 226:
            shifted = code + 29
            if 32 <= shifted <= 255:
                try:
                    out.append(bytes([shifted]).decode("cp1250"))
                    continue
                except Exception:
                    pass
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def _looks_like_shifted_stream(text: str) -> bool:
    """
    Detects -29 glyph-shifted strings both with spaces ('\\x03') and single-word
    spans without spaces (e.g., 'ZQLRVNRGDZF\\\\' -> 'wnioskodawcy', '1D]ZLVNR' -> 'Nazwisko').
    """
    if "\x03" in text or any(1 <= ord(c) <= 31 and c not in "\r\n\t" for c in text):
        return True
    # Single-word shifted tokens contain uppercase runs mixed with ']', '\\', '^', or leading digits
    if re.search(r"[A-Z]{3,}[\\\]\^]|[\\\]\^][A-Z]{2,}|^1[A-Z\]]{4,}", text.strip()):
        return True
    return False


def normalize_polish_pdf_text(text: str) -> str:
    """
    Must be called on raw span text BEFORE whitespace collapsing so that
    0x0B ('(') and 0x0C (')') are not eaten by .split().
    """
    if _looks_like_shifted_stream(text):
        text = _decode_shifted_glyphs(text)

    # Collapse spaced-out single capital letters like "W Z ě R" -> "WZÓR"
    text = re.sub(
        r"\b([A-ZĄĆĘŁŃÓŚŹŻěţ])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻěţ]\b)",
        r"\1",
        text,
    )

    char_fixes = {
        "ě": "Ó",
        "ţ": "ł",
        "Ţ": "Ł",
        "ď": "ó",
        "ŕ": "ó",
        "Ľ": "Ż",
        "ť": "ł",
        "ˇ": "Ą",
        "¦": "Ś",
        "¬": "Ź",
        "¯": "Ż",
        "±": "ą",
        "¶": "ś",
        "¼": "ź",
        "¿": "ż",
        "Ĩ": "ź",
        "ĝ": "Ś",
        "³": "ł",
        "£": "Ł",
        "ê": "ę",
        "Ê": "Ę",
        "ñ": "ń",
        "Ñ": "Ń",
        "æ": "ć",
        "Æ": "Ć",
        "á": "„",
        "Ń": "”",
    }
    for bad, good in char_fixes.items():
        text = text.replace(bad, good)

    return text


def split_label_and_footnote(raw_label: str) -> tuple[str, str | None]:
    """
    Separates superscript footnote markers like 'Nazwa państwa wnioskodawcy3)'
    or 'Pełnomocnik w Rzeczypospolitej Polskiej 7) do doręczeń...' into clean text + '7'.
    """
    cleaned = " ".join(raw_label.split()).strip()

    # 1. Trailing footnote marker at end of string
    m_end = re.search(r"(?<=[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ\)\]])\s*(\d+)\)\s*$", cleaned)
    if m_end:
        fn_id = m_end.group(1)
        cleaned = cleaned[: m_end.start()].rstrip(" :").strip()
        return cleaned, fn_id

    # 2. Inline superscript marker inside a multi-line wrapped label (e.g. "...Polskiej 7) do doręczeń...")
    m_mid = re.search(r"(?<=[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ])\s+(\d+)\)\s+(?=[a-ząćęłńóśźż])", cleaned)
    if m_mid:
        fn_id = m_mid.group(1)
        cleaned = (cleaned[: m_mid.start()] + " " + cleaned[m_mid.end() :]).rstrip(" :").strip()
        return cleaned, fn_id

    # 3. Guard against superscript marker sorted ahead of a non-numbered label
    m_start = re.match(r"^(\d+)\)\s+([A-ZĄĆĘŁŃÓŚŹŻ].+)$", cleaned)
    if m_start:
        return m_start.group(2).rstrip(" :").strip(), m_start.group(1)

    return cleaned.rstrip(":").strip(), None


def slugify(text: str) -> str:
    """Generates a clean snake_case identifier from a label."""
    text, _ = split_label_and_footnote(text)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "_", text).strip("_")[:60]


def extract_bureau_info(text: str) -> dict | None:
    """Extracts bureau name, address, phone, fax, email, and website."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    info = {
        "bureau": None,
        "address": None,
        "phone": None,
        "fax": None,
        "email": None,
        "website": None,
    }

    bureau_re = re.compile(
        r"^((?:Ministerstwo|Urząd|Biuro|Kancelaria|Izba|Inspektorat|Sąd|Mennica)\b.+)",
        re.IGNORECASE,
    )
    address_re = re.compile(
        r"((?:ul\.|al\.|pl\.|os\.)?\s*[A-ZĄĆĘŁŃÓŚŹŻ][\w\s.-]+\d+[a-zA-Z0-9/]*,\s*\d{2}-\d{3}\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\s-]+)"
    )
    phone_re = re.compile(r"(?:telefon|tel\.?)\s*:?\s*([+\d\s()-]{7,})", re.IGNORECASE)
    fax_re = re.compile(r"(?:faks|fax)\s*:?\s*([+\d\s()-]{7,})", re.IGNORECASE)
    email_re = re.compile(
        r"(?:e-?mail\s*:?\s*)?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        re.IGNORECASE,
    )
    web_re = re.compile(
        r"(?:strona internetowa\s*:?\s*)?((?:https?://)?(?:www\.)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?|(?:https?://)?[a-zA-Z0-9.-]+\.(?:pl|org|gov\.pl|eu|com|net)(?:/[^\s]*)?)",
        re.IGNORECASE,
    )

    for line in lines:
        if not info["bureau"]:
            m = bureau_re.match(line)
            if m and "rozporządzenia" not in line.lower():
                info["bureau"] = m.group(1).strip()

        if not info["address"]:
            m = address_re.search(line)
            if m:
                info["address"] = m.group(1).strip()

        if not info["phone"]:
            m = phone_re.search(line)
            if m:
                info["phone"] = m.group(1).strip()

        if not info["fax"]:
            m = fax_re.search(line)
            if m:
                info["fax"] = m.group(1).strip()

        if not info["email"]:
            m = email_re.search(line)
            if m:
                info["email"] = m.group(1).strip()

        if not info["website"]:
            line_no_email = email_re.sub("", line)
            m = web_re.search(line_no_email)
            if m:
                info["website"] = m.group(1).strip()

    return info if any(info.values()) else None