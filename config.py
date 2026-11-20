import os
from pathlib import Path
from dotenv import load_dotenv


def load_config() -> tuple[Path, Path]:
    """Loads INPUT_DIR and OUTPUT_DIR from .env."""
    load_dotenv()
    input_dir = Path(os.getenv("INPUT_DIR", "./input_forms")).resolve()
    output_dir = Path(os.getenv("OUTPUT_DIR", "./output_json")).resolve()

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    return input_dir, output_dir
