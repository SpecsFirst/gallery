"""Tests for the read-only static API and llms.txt (ADR-010, TASK-013)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from gallery.build import build
from tests.helpers import VALID_ENTRY, write_entry, write_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (REPO_ROOT / "schema" / "api-v1.schema.json").read_text(encoding="utf-8")
)
BASE_URL = "https://specsfirst.github.io/gallery/"
VALIDATOR = Draft202012Validator(SCHEMA)


def detected(**overrides) -> dict:
    base = {
        "status": "ok",
        "error": None,
        "license": "MIT",
        "copier": True,
        "tools": ["AGENTS.md", "CLAUDE.md"],
        "last_update": "2026-10-01T09:30:00Z",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "tree": ["main.md", "subrepos/", ".github/", ".github/ci.yml", "src/a.py"],
        "tree_truncated": False,
        "preview": "# Hello\n\nA preview.",
    }
    base.update(overrides)
    return base


FAILED = {
    "status": "failed",
    "error": "timeout",
    "license": None,
    "copier": None,
    "tools": None,
    "last_update": None,
    "commit": None,
    "tree": None,
    "tree_truncated": None,
    "preview": None,
}


def write_data(path: Path, templates: dict) -> None:
    path.write_text(
        json.dumps({"generated_at": "2026-11-06T12:00:00Z", "templates": templates}),
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path):
    root = write_registry(tmp_path / "repo")
    write_entry(root, "alpha", {**VALID_ENTRY, "name": "Alpha template"})
    write_entry(
        root,
        "beta",
        {
            **VALID_ENTRY,
            "name": "Beta template",
            "kind": "software",
            "tags": ["multi-repo"],
        },
    )
    return root


def build_site(root: Path, tmp_path: Path, det=None, votes=None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out"
    det_file = tmp_path / "detected.json"
    votes_file = tmp_path / "votes.json"
    if det is not None:
        write_data(det_file, det)
    if votes is not None:
        write_data(votes_file, votes)
    build(
        root,
        out,
        det_file if det is not None else None,
        votes_file if votes is not None else None,
    )
    return out


def index_of(out: Path) -> dict:
    return json.loads(
        (out / "api" / "v1" / "templates.json").read_text(encoding="utf-8")
    )


def detail_of(out: Path, entry_id: str) -> dict:
    return json.loads(
        (out / "api" / "v1" / "templates" / f"{entry_id}.json").read_text(
            encoding="utf-8"
        )
    )


def card_order(out: Path) -> list[str]:
    from tests.test_build import parse

    return parse(out / "index.html").card_order()


# ---------------------------------------------------------------- the files


def test_api_files_are_written(repo, tmp_path):
    out = build_site(repo, tmp_path)

    assert (out / "api" / "v1" / "templates.json").is_file()
    for entry_id in ("alpha", "beta"):
        assert (out / "api" / "v1" / "templates" / f"{entry_id}.json").is_file()
    assert (out / "llms.txt").is_file()
    # JSON is UTF-8, indented, with a final newline
    text = (out / "api" / "v1" / "templates.json").read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert '  "templates"' in text


# ------------------------------------------------------------ schema and shape


@pytest.mark.parametrize(
    "det",
    [
        None,  # no data files at all
        {"alpha": FAILED, "beta": FAILED},  # failed detection
        {"alpha": detected(), "beta": detected()},  # full data
    ],
    ids=["none", "failed", "ok"],
)
def test_every_document_validates_against_the_schema(repo, tmp_path, det):
    out = build_site(repo, tmp_path, det=det)

    docs = [index_of(out)]
    for entry_id in ("alpha", "beta"):
        docs.append(detail_of(out, entry_id))
    for doc in docs:
        errors = list(VALIDATOR.iter_errors(doc))
        assert errors == [], errors


def test_every_key_is_present_and_nullable_values(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(), "beta": FAILED})

    index = index_of(out)
    assert index["version"] == "v1"
    assert index["generated_at"].endswith("Z")
    alpha = index["templates"][0]
    for key in (
        "id",
        "name",
        "description",
        "kind",
        "tags",
        "placement",
        "url",
        "ref",
        "path",
        "detection",
        "license",
        "updated",
        "commit",
        "stars",
        "command",
        "page",
        "detail",
    ):
        assert key in alpha, key

    detail = detail_of(out, "alpha")["template"]
    for key in ("tree", "files", "truncated"):
        assert key in detail, key


# ------------------------------------------------------- detection and values


def test_index_is_in_home_page_order_and_matches_cards(repo, tmp_path):
    det = {
        "alpha": detected(last_update="2026-10-01T00:00:00Z"),
        "beta": detected(last_update="2026-10-02T00:00:00Z"),
    }
    votes = {"beta": {"votes": 5}, "alpha": {"votes": 1}}
    out = build_site(repo, tmp_path, det=det, votes=votes)

    index = index_of(out)
    assert [t["id"] for t in index["templates"]] == card_order(out)
    # 5 stars first, then 1 star
    assert [t["id"] for t in index["templates"]] == ["beta", "alpha"]
    assert [t["stars"] for t in index["templates"]] == [5, 1]


def test_command_matches_the_page_and_stars_match_cards(repo, tmp_path):
    det = {"alpha": detected(copier=True)}
    votes = {"alpha": {"votes": 3}}
    out = build_site(repo, tmp_path, det=det, votes=votes)

    from tests.test_build import page_of

    (code,) = page_of(out).by_class("cmd")[0].find("code")
    alpha = index_of(out)["templates"][0]
    assert alpha["command"] == code.text()
    assert alpha["command"] == "copier copy https://github.com/example/repo my-project"
    assert alpha["stars"] == 3


def test_detection_states(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": FAILED})

    alpha = index_of(out)["templates"][0]
    beta = index_of(out)["templates"][1]
    assert alpha["detection"] == "failed"
    assert beta["detection"] == "pending"
    for entry in (alpha, beta):
        assert entry["license"] is None
        assert entry["updated"] is None
        assert entry["commit"] is None
    assert alpha["command"] is None
    # a failed/pending detail has no tree or files
    for doc in (detail_of(out, "alpha"), detail_of(out, "beta")):
        template = doc["template"]
        assert template["tree"] is None
        assert template["files"] == []
        assert template["truncated"] is False


def test_ok_detection_values_and_detail_tree_files(repo, tmp_path):
    tree = ["main.md", "subrepos/", ".github/", ".github/ci.yml", "src/a.py"]
    out = build_site(
        repo, tmp_path, det={"alpha": detected(tree=tree, tree_truncated=True)}
    )

    alpha = index_of(out)["templates"][0]
    assert alpha["detection"] == "ok"
    assert alpha["license"] == "MIT"
    assert alpha["updated"] == "2026-10-01T09:30:00Z"
    assert alpha["commit"] == "0123456789abcdef0123456789abcdef01234567"

    detail = detail_of(out, "alpha")["template"]
    # the site's tree text hides dot-entries (render_tree), so .github is absent
    assert detail["tree"] == ".\n├── main.md\n├── src\n│   └── a.py\n└── subrepos"
    # files: non-directories only, dot-entries included, in detect order
    assert detail["files"] == ["main.md", ".github/ci.yml", "src/a.py"]
    assert detail["truncated"] is True
    assert detail["page"] == f"{BASE_URL}templates/alpha/"
    assert detail["detail"] == f"{BASE_URL}api/v1/templates/alpha.json"


def test_license_variants(repo, tmp_path):
    cases = {
        "unknown": "unknown",
        "CC0-1.0": "CC0-1.0",
        None: None,
        "<b>x</b>": None,
        12: None,
    }
    for license_value, expected in cases.items():
        out = build_site(
            repo,
            tmp_path / str(license_value),
            det={"alpha": detected(license=license_value)},
        )
        assert index_of(out)["templates"][0]["license"] == expected


def test_updated_and_commit_null_when_unreadable(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(last_update="yesterday", commit="nope")},
    )
    alpha = index_of(out)["templates"][0]
    assert alpha["updated"] is None
    assert alpha["commit"] is None


def test_kind_and_tags_resolved_like_the_site(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {
            **VALID_ENTRY,
            "name": "Alpha template",
            "kind": "research",
            "tags": ["phd", "thesis", "multi-repo"],
        },
    )
    out = build_site(repo, tmp_path)

    alpha = index_of(out)["templates"][0]
    # kind is separate, tags resolved (phd -> thesis) without the kind and deduped
    assert alpha["kind"] == "research"
    assert alpha["tags"] == ["thesis", "multi-repo"]


def test_invalid_ids_are_skipped(repo, tmp_path):
    (repo / "templates" / "Bad_Id.yml").write_text(
        "name: Whatever\nurl: https://github.com/x/y\n"
        "description: a long enough description\nkind: research\n",
        encoding="utf-8",
    )
    out = build_site(repo, tmp_path)

    ids = [t["id"] for t in index_of(out)["templates"]]
    assert ids == ["alpha", "beta"]
    assert not (out / "api" / "v1" / "templates" / "Bad_Id.json").exists()


# ------------------------------------------------------------- hostile values


def test_hostile_entry_yields_clean_valid_json(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {
            **VALID_ENTRY,
            "name": 'evil "name" </script>\x01',
            "description": 'desc </script> "\x7f more',
            "url": "https://github.com/example/repo",
            "tags": ["<b>tag</b>"],
            "kind": "research",
        },
    )
    tree = ["<script>alert(1)</script>", "a\nb", ".dot-file", ".hidden/"]
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(tree=tree, license="<i>x</i>")},
    )

    text = (out / "api" / "v1" / "templates.json").read_text(encoding="utf-8")
    json.loads(text)  # valid JSON, no string escaping or markup
    alpha = index_of(out)["templates"][0]
    assert alpha["name"] == "evil \"name\" </script> "
    assert "1f" not in alpha["name"].encode().hex() and "\x01" not in alpha["name"]
    assert alpha["description"] == "desc </script> \"  more"
    assert alpha["tags"] == ["<b>tag</b>"]
    assert alpha["license"] is None  # "<i>x</i>" is not an SPDX id
    detail = detail_of(out, "alpha")["template"]
    assert detail["files"] == ["<script>alert(1)</script>", "a b", ".dot-file"]
    assert all(isinstance(item, str) for item in detail["files"])


# ------------------------------------------------------------------- llms.txt


def test_llms_txt_contains_only_the_gallery_own_text_and_urls(repo, tmp_path):
    out = build_site(repo, tmp_path)

    text = (out / "llms.txt").read_text(encoding="utf-8")
    assert text.startswith("# SpecsFirst gallery\n")
    assert "> " in text
    assert f"{BASE_URL}api/v1/templates.json" in text
    assert "schema/api-v1.schema.json" in text
    assert "issues/new?template=submit-template.yml" in text
    assert "CONTRIBUTING.md" in text
    assert "Trust" in text
    # no entry value leaks in
    for entry in (VALID_ENTRY["name"], VALID_ENTRY["description"], VALID_ENTRY["url"]):
        assert entry not in text
    assert "Alpha template" not in text


# ----------------------------------------------------------------- head link


def test_head_link_on_home_and_template_page(repo, tmp_path):
    out = build_site(repo, tmp_path)

    from tests.test_build import parse

    for path, expected in (
        (out / "index.html", "api/v1/templates.json"),
        (out / "templates/alpha/index.html", "../../api/v1/templates.json"),
    ):
        page = parse(path)
        links = page.find(
            "link", **{"rel": "alternate", "type": "application/json"}
        )
        assert links, path
        (link,) = links
        assert link.attrs["href"] == expected


def test_no_footer_added(repo, tmp_path):
    out = build_site(repo, tmp_path)
    for path in (out / "index.html", out / "templates/alpha/index.html"):
        assert "<footer" not in path.read_text(encoding="utf-8")
