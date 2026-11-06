"""Tests for the validation command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gallery.validate import main, validate
from tests.helpers import VALID_ENTRY, write_entry, write_registry


def test_valid_entry_passes(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "good", VALID_ENTRY)
    assert validate(root) == 0


def test_both_real_entries_pass():
    import gallery

    root = Path(gallery.__file__).resolve().parent.parent.parent
    assert validate(root) == 0


def test_invalid_yaml(tmp_path):
    root = write_registry(tmp_path)
    path = root / "templates" / "bad.yml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    assert validate(root) == 1


def test_not_a_mapping(tmp_path):
    root = write_registry(tmp_path)
    path = root / "templates" / "bad.yml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert validate(root) == 1


def test_fails_json_schema(tmp_path):
    root = write_registry(tmp_path)
    write_entry(
        root,
        "bad",
        {"name": "x", "url": "nope", "description": "short", "kind": "nope"},
    )
    # missing required fields and a bad url pattern -> schema errors
    assert validate(root) == 1


def test_bad_id(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "Bad_Id!", VALID_ENTRY)
    assert validate(root) == 1


def test_non_yml_extension(tmp_path):
    root = write_registry(tmp_path)
    (root / "templates" / "notes.txt").write_text("hello", encoding="utf-8")
    assert validate(root) == 1


def test_kind_not_in_taxonomy(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "bad", {**VALID_ENTRY, "kind": "unknown"})
    assert validate(root) == 1


def test_tag_not_in_taxonomy(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "bad", {**VALID_ENTRY, "tags": ["not-a-tag"]})
    assert validate(root) == 1


def test_url_with_credentials(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "bad", {**VALID_ENTRY, "url": "https://user:pass@github.com/x"})
    assert validate(root) == 1


def test_path_absolute(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "bad", {**VALID_ENTRY, "path": "/etc/passwd"})
    assert validate(root) == 1


def test_path_dotdot(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "bad", {**VALID_ENTRY, "path": "../escape"})
    assert validate(root) == 1


def test_ref_starts_with_dash(tmp_path):
    root = write_registry(tmp_path)
    write_entry(root, "bad", {**VALID_ENTRY, "ref": "-tags/something"})
    assert validate(root) == 1


def test_deprecated_tag_is_warning_not_error(tmp_path, capsys):
    root = write_registry(tmp_path)
    write_entry(root, "dep", {**VALID_ENTRY, "tags": ["phd"]})
    assert validate(root) == 0
    out = capsys.readouterr().out
    assert "phd" in out and "thesis" in out


def test_taxonomy_group_unknown_tag(tmp_path):
    taxonomy = (
        "kinds: {research: R}\ntags:\n  thesis: A PhD thesis."
        "\ngroups:\n  X: [ghost]\n"
    )
    root = write_registry(tmp_path, taxonomy=taxonomy)
    assert validate(root) == 1


def test_taxonomy_deprecated_without_replaced_by(tmp_path):
    taxonomy = (
        "kinds: {research: R}\n"
        "tags:\n"
        "  phd:\n"
        "    description: Old.\n"
        "    deprecated: true\n"
        "groups: {}\n"
    )
    root = write_registry(tmp_path, taxonomy=taxonomy)
    assert validate(root) == 1


def test_taxonomy_replaced_by_unknown(tmp_path):
    taxonomy = (
        "kinds: {research: R}\n"
        "tags:\n"
        "  phd:\n"
        "    description: Old.\n"
        "    deprecated: true\n"
        "    replaced_by: ghost\n"
        "groups: {}\n"
    )
    root = write_registry(tmp_path, taxonomy=taxonomy)
    assert validate(root) == 1


def test_taxonomy_replaced_by_cycle(tmp_path):
    taxonomy = (
        "kinds: {research: R}\n"
        "tags:\n"
        "  a:\n"
        "    description: a\n"
        "    deprecated: true\n"
        "    replaced_by: b\n"
        "  b:\n"
        "    description: b\n"
        "    deprecated: true\n"
        "    replaced_by: a\n"
        "groups: {}\n"
    )
    root = write_registry(tmp_path, taxonomy=taxonomy)
    assert validate(root) == 1


def test_taxonomy_invalid_structure(tmp_path):
    root = write_registry(tmp_path, taxonomy="tags: - not\n- a\n- mapping\n")
    assert validate(root) == 1


def test_reports_all_errors_not_only_first(tmp_path, capsys):
    root = write_registry(tmp_path)
    write_entry(
        root,
        "bad",
        {"name": "x", "url": "nope", "description": "short", "kind": "nope"},
    )
    validate(root)
    out = capsys.readouterr().out
    assert out.count(": error:") >= 4


def test_github_actions_annotations(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    root = write_registry(tmp_path)
    write_entry(
        root,
        "bad",
        {"name": "x", "url": "nope", "description": "short", "kind": "nope"},
    )
    validate(root, in_actions=True)
    out = capsys.readouterr().out
    assert "::error file=" in out


def test_github_actions_escapes_injection(tmp_path, capsys, monkeypatch):
    # An entry cannot inject workflow commands: %, CR, LF are escaped.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    root = write_registry(tmp_path)
    root.mkdir(exist_ok=True)
    (root / "templates").mkdir(exist_ok=True)
    (root / "schema").mkdir(exist_ok=True)
    (root / "taxonomy.yml").write_text(
        "kinds: {research: R}\ntags: {}\ngroups: {}\n", encoding="utf-8"
    )
    (root / "schema" / "template.schema.json").write_text(json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
        }
    ), encoding="utf-8")
    path = root / "templates" / "evil.yml"
    path.write_text(
        "name: x\nurl: nope\ndescription: a\nkind: ::\nset-output name=x::pwned\n",
        encoding="utf-8",
    )
    validate(root, in_actions=True)
    out = capsys.readouterr().out
    assert "::set-output" not in out


def test_cli_main(tmp_path, capsys):
    root = write_registry(tmp_path)
    write_entry(root, "good", VALID_ENTRY)
    assert main(["--root", str(root)]) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: write_entry(r, "b", {**VALID_ENTRY, "kind": ["a"]}),
        lambda r: write_entry(r, "b", {**VALID_ENTRY, "tags": [["x"]]}),
        lambda r: write_entry(r, "b", {**VALID_ENTRY, "url": 123}),
        lambda r: write_entry(r, "b", {**VALID_ENTRY, "url": "https://[bad"}),
        lambda r: write_entry(r, "b", {**VALID_ENTRY, "path": 5}),
    ],
)
def test_no_traceback_for_hostile_fields(tmp_path, mutate):
    root = write_registry(tmp_path)
    mutate(root)
    assert validate(root) == 1


def test_no_traceback_for_non_utf8_file(tmp_path):
    root = write_registry(tmp_path)
    (root / "templates" / "b.yml").write_bytes(b"\xff\xfe\x00\x80")
    assert validate(root) == 1


def test_broken_schema_is_error_not_crash(tmp_path):
    root = write_registry(tmp_path)
    (root / "schema" / "template.schema.json").write_text("{bad json", encoding="utf-8")
    write_entry(root, "good", VALID_ENTRY)
    assert validate(root) == 1


def test_missing_schema_is_error_not_crash(tmp_path):
    root = write_registry(tmp_path)
    (root / "schema" / "template.schema.json").unlink()
    write_entry(root, "good", VALID_ENTRY)
    assert validate(root) == 1


def test_missing_taxonomy_is_error_not_crash(tmp_path):
    root = write_registry(tmp_path)
    (root / "taxonomy.yml").unlink()
    write_entry(root, "good", VALID_ENTRY)
    assert validate(root) == 1


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
def test_taxonomy_structure_errors(tmp_path, taxonomy):
    root = write_registry(tmp_path, taxonomy=taxonomy)
    assert validate(root) == 1


def test_templates_subfolder_is_error(tmp_path):
    root = write_registry(tmp_path)
    (root / "templates" / "sub").mkdir()
    assert validate(root) == 1


def test_templates_symlink_is_error_and_not_read(tmp_path, capsys):
    root = write_registry(tmp_path)
    target = tmp_path / "outside-secret.yml"
    target.write_text("SECRET_CANARY: 12345\n", encoding="utf-8")
    (root / "templates" / "link.yml").symlink_to(target)
    assert validate(root) == 1
    out = capsys.readouterr().out
    assert "SECRET_CANARY" not in out


def test_plain_output_stays_on_one_line(tmp_path, capsys):
    root = write_registry(tmp_path)
    name = "b\n::notice::x\n.yml"
    (root / "templates" / name).write_text(
        "name: x\nurl: nope\ndescription: a\nkind: y\n", encoding="utf-8"
    )
    validate(root)
    out = capsys.readouterr().out
    assert all("\n" not in line for line in out.splitlines())
    assert not any(line.startswith("::notice") for line in out.splitlines())


def test_annotation_file_property_escapes_colon_and_comma(tmp_path, capsys):
    root = write_registry(tmp_path)
    (root / "templates" / "a,line:9.yml").write_text(
        "name: x\nurl: nope\ndescription: a\nkind: y\n", encoding="utf-8"
    )
    validate(root, in_actions=True)
    out = capsys.readouterr().out
    assert "file=templates/a%2Cline%3A9.yml" in out
    assert "file=templates/a,line:9.yml" not in out
