"""Test helpers: build a minimal registry tree in a tmp directory."""

from __future__ import annotations

import json
from pathlib import Path

TAXONOMY = """\
kinds:
  research: Research projects.
  software: Libraries and services.

tags:
  thesis: A PhD thesis.
  multi-repo: Several code repos.
  phd:
    description: Old name of thesis.
    deprecated: true
    replaced_by: thesis

groups:
  Project: [thesis]
  Setup: [multi-repo]
"""

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://specsfirst.github.io/gallery/schema/template.schema.json",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "url", "description", "kind"],
    "properties": {
        "name": {"type": "string", "minLength": 3, "maxLength": 60},
        "url": {"type": "string", "pattern": "^https://[^\\s]+$"},
        "description": {"type": "string", "minLength": 20, "maxLength": 200},
        "kind": {"type": "string"},
        "tags": {
            "type": "array",
            "maxItems": 4,
            "uniqueItems": True,
            "items": {"type": "string"},
        },
        "path": {"type": "string"},
        "ref": {"type": "string"},
        "placement": {
            "type": "string",
            "enum": ["standalone", "in-repo"],
            "default": "standalone",
        },
        "command": {
            "type": "string",
            "minLength": 1,
            "maxLength": 300,
            "pattern": "^[^\\r\\n]+$",
        },
        "language": {"type": "string", "pattern": "^[a-z]{2}$", "default": "en"},
    },
}


def write_registry(
    root: Path, taxonomy: str = TAXONOMY, schema: dict | None = None
) -> Path:
    """Write a taxonomy.yml and schema under ``root``; return the root."""
    (root / "schema").mkdir(parents=True, exist_ok=True)
    (root / "taxonomy.yml").write_text(taxonomy, encoding="utf-8")
    (root / "schema" / "template.schema.json").write_text(
        json.dumps(schema if schema is not None else SCHEMA), encoding="utf-8"
    )
    (root / "templates").mkdir(parents=True, exist_ok=True)
    return root


def write_entry(root: Path, entry_id: str, data: dict) -> Path:
    """Write one templates/<id>.yml file with the given fields."""
    import yaml

    path = root / "templates" / f"{entry_id}.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


VALID_ENTRY = {
    "name": "A valid research template",
    "url": "https://github.com/example/repo",
    "description": "A sufficiently long description for a research template.",
    "kind": "research",
    "tags": ["thesis"],
}
