import json
import shutil
from pathlib import Path

from epub_commentor import LLM


def load_comment_llm(**args) -> LLM:
    """Read format.json from the repo root and construct a single LLM for commentary.

    format.json schema (flat, no translation/fill sub-keys):
    {
      "key": "<API key>",
      "url": "<base url>",
      "model": "<model name>",
      "token_encoding": "<tiktoken encoding name>",
      "timeout": 360.0,
      "retry_times": 5,
      "retry_interval_seconds": 6.0,
      "temperature": 0.4,
      "top_p": 0.9,
      "cache_path": "<optional cache dir>"
    }
    """
    config = read_format_json()
    return LLM(**config, **args)


def read_format_json() -> dict:
    path = Path(__file__).parent / ".." / "format.json"
    path = path.resolve()
    with open(path, encoding="utf-8") as file:
        return json.load(file)


def read_and_clean_temp() -> Path:
    temp_path = Path(__file__).parent / ".." / "temp"
    shutil.rmtree(temp_path, ignore_errors=True)
    temp_path.mkdir(parents=True, exist_ok=True)
    return temp_path.resolve()
