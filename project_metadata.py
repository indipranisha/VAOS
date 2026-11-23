"""Central read-only loader for public VAOS project identity and attribution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


METADATA_PATH = Path(__file__).resolve().with_name("VAOS_PROJECT_METADATA.json")
_FALLBACK: dict[str, Any] = {
    "name": "VAOS",
    "version": "1.2.0",
    "license": "AGPL-3.0-only",
    "creator": {
        "name": "Prateek Dubey",
        "role": "Founder and Original Creator",
        "website": "https://prateekdubey.in",
        "linkedin": "https://www.linkedin.com/in/progressiveprateek",
    },
    "creator_statement": "VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community.",
}


def load_project_metadata() -> dict[str, Any]:
    """Return the public metadata, falling back safely if a packaging error occurs."""
    try:
        raw = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        creator = raw["creator"]
        if (raw.get("name") != "VAOS" or not isinstance(creator, dict)
                or not all(isinstance(creator.get(key), str) and creator[key].strip() for key in ("name", "role", "website", "linkedin"))
                or not all(creator[key].startswith("https://") for key in ("website", "linkedin"))
                or raw.get("creator_statement") != _FALLBACK["creator_statement"]):
            raise ValueError("VAOS project metadata is incomplete or invalid")
        return raw
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return json.loads(json.dumps(_FALLBACK))


PROJECT_METADATA = load_project_metadata()
