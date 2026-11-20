import os
from pathlib import Path
from dotenv import load_dotenv


def load_config() -> tuple[Path, Path]:
    """Loads INPUT_DIR and OUTPUT_DIR from the .env file."""
    load_dotenv()

    input_dir_str = os.getenv("INPUT_DIR", "./input_forms")
    output_dir_str = os.getenv("OUTPUT_DIR", "./output_json")

    input_dir = Path(input_dir_str).resolve()
    output_dir = Path(output_dir_str).resolve()

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    return input_dir, output_dir