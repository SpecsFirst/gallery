"""Tests for the registry module."""

from __future__ import annotations

from pathlib import Path

import pytest

from gallery.registry import (
    RegistryError,
    load_entry,
    load_registry,
    load_schema,
    load_taxonomy,
)
from tests.helpers import TAXONOMY, VALID_ENTRY, write_entry, write_registry


def test_load_taxonomy_kinds_and_tags(tmp_path):
    root = write_registry(tmp_path)
    taxonomy = load_taxonomy(root / "taxonomy.yml")

    assert taxonomy.kinds == {
        "research": "Research projects.",
        "software": "Libraries and services.",
    }

    thesis = taxonomy.tags["thesis"]
    assert thesis.description == "A PhD thesis."
    assert thesis.active is True

    phd = taxonomy.tags["phd"]
    assert phd.deprecated is True
    assert phd.replaced_by == "thesis"
    assert phd.active is False


def test_load_taxonomy_groups_in_file_order(tmp_path):
    root = write_registry(tmp_path)
    taxonomy = load_taxonomy(root / "taxonomy.yml")

    assert [g.name for g in taxonomy.groups] == ["Project", "Setup"]
    assert taxonomy.groups[0].tags == ("thesis",)
    assert taxonomy.groups[1].tags == ("multi-repo",)


def test_active_tags_and_ungrouped(tmp_path):
    root = write_registry(tmp_path)
    taxonomy = load_taxonomy(root / "taxonomy.yml")

    assert "phd" not in taxonomy.active_tags
    assert taxonomy.active_tags["thesis"] == "A PhD thesis."
    # 'multi-repo' is active and grouped; thesis is grouped too -> none ungrouped
    assert taxonomy.ungrouped_active_tags == {}

    # an active tag in no group is still exposed
    no_group = TAXONOMY.replace(
        "  thesis: A PhD thesis.", "  thesis: A PhD thesis.\n  bench: Lab work."
    )
    write_registry(tmp_path, taxonomy=no_group)
    taxonomy2 = load_taxonomy(tmp_path / "taxonomy.yml")
    assert "bench" in taxonomy2.active_tags
    assert taxonomy2.ungrouped_active_tags == {"bench": "Lab work."}


def test_resolve_tag_follows_chain_to_end(tmp_path):
    taxonomy_yaml = """\
kinds: {research: R}
tags:
  old1:
    description: old one
    deprecated: true
    replaced_by: old2
  old2:
    description: old two
    deprecated: true
    replaced_by: thesis
  thesis: A PhD thesis.
groups: {}
"""
    write_registry(tmp_path, taxonomy=taxonomy_yaml)
    taxonomy = load_taxonomy(tmp_path / "taxonomy.yml")

    assert taxonomy.resolve_tag("thesis") == "thesis"
    assert taxonomy.resolve_tag("old1") == "thesis"
    assert taxonomy.resolve_tag("old2") == "thesis"


def test_resolve_tag_unknown_and_cycle(tmp_path):
    taxonomy_yaml = """\
kinds: {research: R}
tags:
  a:
    description: a
    deprecated: true
    replaced_by: b
  b:
    description: b
    deprecated: true
    replaced_by: a
  thesis: A PhD thesis.
groups: {}
"""
    write_registry(tmp_path, taxonomy=taxonomy_yaml)
    taxonomy = load_taxonomy(tmp_path / "taxonomy.yml")

    assert taxonomy.resolve_tag("missing") is None
    assert taxonomy.resolve_tag("a") is None  # cycle


def test_load_entry_exposes_id_fields_and_language(tmp_path):
    root = write_registry(tmp_path)
    schema = load_schema(root / "schema" / "template.schema.json")
    path = write_entry(root, "my-entry", {**VALID_ENTRY, "language": "fr"})

    entry = load_entry(path, schema)
    assert entry.id == "my-entry"
    assert entry.name == VALID_ENTRY["name"]
    assert entry.kind == "research"
    assert entry.language == "fr"


def test_load_entry_default_language_is_en(tmp_path):
    root = write_registry(tmp_path)
    schema = load_schema(root / "schema" / "template.schema.json")
    path = write_entry(root, "my-entry", VALID_ENTRY)

    entry = load_entry(path, schema)
    assert entry.language == "en"


def test_load_entry_invalid_yaml_raises(tmp_path):
    root = write_registry(tmp_path)
    schema = load_schema(root / "schema" / "template.schema.json")
    path = root / "templates" / "bad.yml"
    path.write_text("name: [unclosed\n", encoding="utf-8")

    with pytest.raises(RegistryError):
        load_entry(path, schema)


def test_load_entry_not_a_mapping_raises(tmp_path):
    root = write_registry(tmp_path)
    schema = load_schema(root / "schema" / "template.schema.json")
    path = root / "templates" / "bad.yml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(RegistryError):
        load_entry(path, schema)


def test_load_registry(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "entry-a", VALID_ENTRY)
    write_entry(root, "entry-b", {**VALID_ENTRY, "kind": "software"})

    registry = load_registry(root)
    assert {e.id for e in registry.entries} == {"entry-a", "entry-b"}
    assert registry.taxonomy is not None
    assert registry.schema["$id"].endswith("template.schema.json")


def test_load_registry_uses_real_files(tmp_path):
    # The real taxonomy/schema must load and expose data (read-only sanity).
    import gallery

    pkg_dir = Path(gallery.__file__).resolve().parent.parent.parent
    registry = load_registry(pkg_dir)
    assert "research" in registry.taxonomy.kinds
    assert "thesis" in registry.taxonomy.active_tags
    assert len(registry.entries) == len(list((pkg_dir / "templates").glob("*.yml")))


def test_load_registry_never_reads_symlink(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "good", VALID_ENTRY)
    secret = tmp_path / "outside-secret.yml"
    secret.write_text("SECRET_CANARY: 12345\n", encoding="utf-8")
    (root / "templates" / "evil.yml").symlink_to(secret)

    registry = load_registry(root)
    assert [e.id for e in registry.entries] == ["good"]


def test_load_entry_non_utf8_raises(tmp_path):
    root = write_registry(tmp_path)
    schema = load_schema(root / "schema" / "template.schema.json")
    path = root / "templates" / "bad.yml"
    path.write_bytes(b"\xff\xfe\x00\x80")

    with pytest.raises(RegistryError):
        load_entry(path, schema)


def test_load_schema_missing_raises(tmp_path):
    root = write_registry(tmp_path)
    (root / "schema" / "template.schema.json").unlink()
    with pytest.raises(RegistryError):
        load_schema(root / "schema" / "template.schema.json")


def test_load_taxonomy_missing_raises(tmp_path):
    root = write_registry(tmp_path)
    (root / "taxonomy.yml").unlink()
    with pytest.raises(RegistryError):
        load_taxonomy(root / "taxonomy.yml")


@pytest.mark.parametrize(
    "taxonomy",
    [
        "kinds: {r: R}\ntags: {a: A.}\ngroups:\n  X: notalist\n",
        "tags: {a: A.}\ngroups: {}\n",
        "kinds: {r: R}\ngroups: {}\n",
        (
            "kinds: {r: R}\n"
            "tags:\n"
            "  a:\n"
            "    description: A\n"
            "    deprecated: true\n"
            "    replaced_by: [x]\n"
            "groups: {}\n"
        ),
    ],
)
def test_load_taxonomy_rejects_bad_structure(tmp_path, taxonomy):
    root = write_registry(tmp_path, taxonomy=taxonomy)
    with pytest.raises(RegistryError):
        load_taxonomy(root / "taxonomy.yml")
