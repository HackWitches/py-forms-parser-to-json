from pathlib import Path
from config import load_config
from file_io import read_folder, write_output
from parsers.pdf_parser import parse_pdf
from parsers.rtf_parser import parse_rtf
from parsers.xls_parser import parse_xls


def route_parser(file_path: Path) -> dict:
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return parse_pdf(file_path)
    if ext == ".rtf":
        return parse_rtf(file_path)
    if ext in {".xls", ".xlsx"}:
        return parse_xls(file_path)
    raise ValueError(f"Unsupported file format: {ext}")


def main():
    input_dir, output_dir = load_config()
    files = read_folder(input_dir)

    if not files:
        print(f"No supported forms found in {input_dir}")
        return

    for file_path in files:
        try:
            parsed = route_parser(file_path)
            out_path = write_output(output_dir, file_path.name, parsed)
            print(f"[OK] {file_path.name} -> {out_path.name} ({len(parsed['fields'])} fields)")
        except Exception as e:
            print(f"[ERROR] Failed to parse {file_path.name}: {e}")


if __name__ == "__main__":
    main()