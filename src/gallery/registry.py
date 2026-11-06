"""Load the gallery registry (taxonomy, schema and entries) into typed objects.

This module is the single source of truth for the registry's data model. It is
used by validation now, and later by detection and the static site build. No
field name, kind or tag is hard-coded here: everything is read from the
``taxonomy.yml`` and ``schema/template.schema.json`` files, which are treated as
read-only inputs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RegistryError(Exception):
    """Raised when a registry file cannot be loaded as well-formed data."""


@dataclass(frozen=True)
class Tag:
    """A tag from ``taxonomy.yml``, in either the simple or the deprecated form."""

    name: str
    description: str
    deprecated: bool = False
    replaced_by: str | None = None

    @property
    def active(self) -> bool:
        return not self.deprecated


@dataclass(frozen=True)
class Group:
    """A display group: a name and the ordered tags it contains."""

    name: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class Taxonomy:
    """The parsed ``taxonomy.yml``: kinds, tags and display groups."""

    kinds: dict[str, str]
    tags: dict[str, Tag]
    groups: tuple[Group, ...]

    @property
    def active_tags(self) -> dict[str, str]:
        """Every active (non-deprecated) tag: name -> description."""
        return {name: tag.description for name, tag in self.tags.items() if tag.active}

    @property
    def grouped_tags(self) -> set[str]:
        """Names of the tags that appear in at least one group."""
        return {name for group in self.groups for name in group.tags}

    @property
    def ungrouped_active_tags(self) -> dict[str, str]:
        """Active tags that appear in no group, so no tag vanishes from filters."""
        grouped = self.grouped_tags
        return {
            name: desc
            for name, desc in self.active_tags.items()
            if name not in grouped
        }

    def resolve_tag(self, name: str) -> str | None:
        """Follow the ``replaced_by`` chain to its active end.

        Returns the resolved active tag name, or ``None`` when the tag is
        unknown, deprecated without a valid target, or part of a cycle.
        """
        seen: set[str] = set()
        current = name
        while True:
            tag = self.tags.get(current)
            if tag is None:
                return None
            if tag.active:
                return current
            if current in seen:
                return None
            seen.add(current)
            if tag.replaced_by is None:
                return None
            current = tag.replaced_by


@dataclass(frozen=True)
class Entry:
    """One template entry: its id plus the schema's fields and resolved language."""

    id: str
    fields: dict[str, Any]
    language: str

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self.fields:
            return self.fields[name]
        raise AttributeError(name)


@dataclass(frozen=True)
class Registry:
    """The whole registry: taxonomy, schema and the ordered template entries."""

    taxonomy: Taxonomy
    schema: dict[str, Any]
    entries: tuple[Entry, ...]
    template_dir: Path = field(
        default_factory=lambda: Path("templates")
    )



def load_taxonomy(path: Path) -> Taxonomy:
    """Load and type the ``taxonomy.yml`` file.

    Raises ``RegistryError`` on bad structure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{path}: cannot read taxonomy: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(f"{path}: taxonomy must be a mapping")

    if "kinds" not in raw:
        raise RegistryError(f"{path}: missing 'kinds'")
    kinds = raw["kinds"]
    if not isinstance(kinds, dict):
        raise RegistryError(f"{path}: 'kinds' must be a mapping")
    kinds = {name: str(desc) for name, desc in kinds.items()}

    if "tags" not in raw:
        raise RegistryError(f"{path}: missing 'tags'")
    raw_tags = raw["tags"]
    if not isinstance(raw_tags, dict):
        raise RegistryError(f"{path}: 'tags' must be a mapping")
    tags: dict[str, Tag] = {}
    for name, value in raw_tags.items():
        if isinstance(value, str):
            tags[name] = Tag(name=name, description=value)
        elif isinstance(value, dict):
            replaced_by = value.get("replaced_by")
            if replaced_by is not None and not isinstance(replaced_by, str):
                raise RegistryError(
                    f"{path}: replaced_by of tag {name!r} must be a string"
                )
            tags[name] = Tag(
                name=name,
                description=str(value.get("description", "")),
                deprecated=bool(value.get("deprecated", False)),
                replaced_by=replaced_by,
            )
        else:
            raise RegistryError(
                f"{path}: tag {name!r} must be a description string or a mapping"
            )

    raw_groups = raw.get("groups", {})
    if not isinstance(raw_groups, dict):
        raise RegistryError(f"{path}: 'groups' must be a mapping")
    groups_list: list[Group] = []
    for name, items in raw_groups.items():
        if not isinstance(items, list):
            raise RegistryError(f"{path}: group {name!r} must be a list of tags")
        groups_list.append(
            Group(name=str(name), tags=tuple(str(t) for t in items))
        )

    return Taxonomy(kinds=kinds, tags=tags, groups=tuple(groups_list))


def load_schema(path: Path) -> dict[str, Any]:
    """Load the JSON Schema file as a dict."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{path}: cannot read schema: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid JSON in {path}: {exc}") from exc


def _default_language(schema: dict[str, Any] | None) -> str:
    """The schema's default for the ``language`` field, or ``en``."""
    if schema is None:
        return "en"
    lang = schema.get("properties", {}).get("language", {})
    default = lang.get("default", "en")
    return str(default)


def load_entry(path: Path, schema: dict[str, Any]) -> Entry:
    """Load one ``templates/<id>.yml`` file into an ``Entry``."""
    entry_id = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{path}: cannot read entry: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryError(f"{path}: entry is not a mapping")

    language = raw.get("language", _default_language(schema))
    return Entry(id=entry_id, fields=raw, language=language)


def load_registry(root: Path) -> Registry:
    """Load taxonomy, schema and every ``templates/*.yml`` entry under ``root``."""
    taxonomy = load_taxonomy(root / "taxonomy.yml")
    schema = load_schema(root / "schema" / "template.schema.json")
    template_dir = root / "templates"
    entries = tuple(
        load_entry(path, schema)
        for path in sorted(template_dir.iterdir())
        if not path.is_symlink() and path.is_file() and path.suffix == ".yml"
    )
    return Registry(
        taxonomy=taxonomy,
        schema=schema,
        entries=entries,
        template_dir=template_dir,
    )
