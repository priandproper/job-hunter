"""Tiny helper to read API keys from an env var or a local .secrets.json file.

.secrets.json is git-ignored. Shape: {"JSEARCH_API_KEY": "...", "APOLLO_API_KEY": "..."}
"""

import json
import os
from pathlib import Path


def get_key(env_name: str, secrets_file: Path | None) -> str | None:
    val = os.environ.get(env_name)
    if val:
        return val
    if secrets_file and Path(secrets_file).exists():
        try:
            data = json.loads(Path(secrets_file).read_text())
            return data.get(env_name)
        except (json.JSONDecodeError, OSError):
            return None
    return None
