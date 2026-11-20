import json
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".rtf", ".xls", ".xlsx"}


def read_folder(input_dir: Path) -> list[Path]:
    """Returns all supported form files in the input directory."""
    return [
        f for f in sorted(input_dir.iterdir())
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def write_output(output_dir: Path, source_filename: str, data: dict) -> Path:
    """Writes parsed form data to a JSON file in the output directory."""
    out_path = output_dir / f"{Path(source_filename).stem}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return out_path