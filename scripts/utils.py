import json
import shutil
from pathlib import Path

from epub_commentor import LLM
from epub_commentor.llm._api_key import EPUB_COMMENTOR_API_KEY_ENV_VAR, resolve_api_key


def load_comment_llm(**args) -> LLM:
    """Read format.json from the repo root and construct a single LLM for commentary.

    format.json schema (flat, no translation/fill sub-keys):
    {
      "key": "<API key>",   # optional if $EPUB_COMMENTOR_API_KEY is set
      "url": "<base url>",
      "model": "<model name>",
      "token_encoding": "<tiktoken encoding name>",
      "timeout": 360.0,
      "retry_times": 5,
      "retry_interval_seconds": 6.0,
      "temperature": 0.4,
      "top_p": 0.9,
      "json_mode": false,
      "cache_path": "<optional cache dir>"
    }

    API key resolution
    ------------------
    The ``$EPUB_COMMENTOR_API_KEY`` env var takes precedence over the
    ``key`` field in ``format.json``. Set the env var (twelve-factor style
    — recommended for safety, since secrets never touch a checked-in file)
    and leave ``key`` empty / unset in ``format.json``; or keep the key in
    ``format.json`` if you prefer the file-based workflow.

    Set ``json_mode`` to ``true`` to force every chat-completion call to
    send ``response_format={"type": "json_object"}`` to the provider —
    supported by OpenAI, DeepSeek, and most other OpenAI-compatible
    services. Leave it ``false`` (default) to keep the SDK's unconstrained
    behaviour.
    """
    config = read_format_json()
    config["key"] = resolve_api_key(config.get("key"))
    if not config["key"]:
        raise RuntimeError(
            "missing LLM API key. "
            f"Set the ${EPUB_COMMENTOR_API_KEY_ENV_VAR} environment variable "
            f"(recommended for safety) or fill the 'key' field in format.json. "
            f"See format.template.json for the field list."
        )
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
