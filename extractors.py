import re


def slugify(text: str) -> str:
    """Creates a clean ASCII/Polish-safe snake_case identifier from a label."""
    text = re.sub(r"\d+\)$", "", text.strip())  # Strip footnote markers like 2), 15)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "_", text).strip("_")[:60]


def extract_bureau_info(text: str) -> dict | None:
    """
    Extracts bureau contact block matching patterns like:
    Ministerstwo Nauki i Szkolnictwa Wyższego
    ul. Hoża 20, 00-529 Warszawa
    telefon: (+48 22) 52 92 266
    faks: (+48 22) 50 17 130
    e-mail: kwalifikacje@nauka.gov.pl
    strona internetowa: www.nauka.gov.pl
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    info = {
        "bureau": None,
        "address": None,
        "phone": None,
        "fax": None,
        "email": None,
        "website": None,
    }

    # Regex patterns for Polish administrative contact details
    bureau_re = re.compile(
        r"^((?:Ministerstwo|Urząd|Biuro|Kancelaria|Izba|Inspektorat|Sąd)\b.+)",
        re.IGNORECASE,
    )
    address_re = re.compile(
        r"((?:ul\.|al\.|pl\.|os\.)?\s*[A-ZĄĆĘŁŃÓŚŹŻ][\w\s.-]+\d+[a-zA-Z0-9/]*,\s*\d{2}-\d{3}\s+[A-ZĄĆĘŁŃÓŚŹŻ][\w\s-]+)"
    )
    phone_re = re.compile(
        r"(?:telefon|tel\.?)\s*:?\s*([+\d\s()-]{7,})", re.IGNORECASE
    )
    fax_re = re.compile(
        r"(?:faks|fax)\s*:?\s*([+\d\s()-]{7,})", re.IGNORECASE
    )
    email_re = re.compile(
        r"(?:e-?mail\s*:?\s*)?([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
        re.IGNORECASE,
    )
    web_re = re.compile(
        r"(?:strona internetowa|www|http)\s*:?\s*((?:https?://)?(?:www\.)?[a-zA-Z0-9.-]+\.(?:pl|org|gov\.pl|eu|com|net)\b[^\s]*)",
        re.IGNORECASE,
    )

    for i, line in enumerate(lines):
        if not info["bureau"]:
            m = bureau_re.match(line)
            # Avoid matching legal citations like "rozporządzenia Ministra..."
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
            m = web_re.search(line)
            if m:
                info["website"] = m.group(1).strip()

    # Return None if nothing meaningful was detected
    if not any(info.values()):
        return None
    return info