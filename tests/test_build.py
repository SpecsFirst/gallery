"""Tests for the static site build. HTML is parsed, not only searched."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from gallery.build import (
    BuildError,
    build,
    copier_command,
    link_allowed,
    main,
    make_preview_renderer,
    render_preview,
    strip_front_matter,
)
from gallery.registry import load_taxonomy
from tests.helpers import VALID_ENTRY, write_entry, write_registry

REPO_ROOT = Path(__file__).resolve().parents[1]

VOID = {"meta", "link", "br", "hr", "input", "img", "base"}


class Element:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Element] = []
        self.chunks: list[str] = []

    def text(self) -> str:
        parts = list(self.chunks)
        for child in self.children:
            parts.append(child.text())
        return "".join(parts)

    def by_class(self, cls: str) -> list[Element]:
        found = []
        for child in self.children:
            if cls in (child.attrs.get("class") or "").split():
                found.append(child)
            found.extend(child.by_class(cls))
        return found

    def has_ancestor(self, tag: str) -> bool:
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return True
            node = node.parent
        return False


class Page(HTMLParser):
    """A tiny DOM: every element, with its attributes and text."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Element("#root", {}, None)
        self.elements: list[Element] = []
        self._stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(element)
        self.elements.append(element)
        if tag not in VOID:
            self._stack.append(element)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        self._stack[-1].chunks.append(data)

    def find(self, tag: str, **attrs) -> list[Element]:
        return [
            e
            for e in self.elements
            if e.tag == tag and all(e.attrs.get(k) == v for k, v in attrs.items())
        ]

    def by_id(self, ident: str) -> Element:
        (element,) = [e for e in self.elements if e.attrs.get("id") == ident]
        return element

    def by_class(self, cls: str) -> list[Element]:
        return self.root.by_class(cls)

    def text(self) -> str:
        return self.root.text()


def parse(path: Path) -> Page:
    return Page(path.read_text(encoding="utf-8"))


def all_pages(out: Path) -> list[Path]:
    return [out / "index.html", *sorted((out / "templates").glob("*/index.html"))]


def detected(**overrides) -> dict:
    base = {
        "status": "ok",
        "error": None,
        "license": "MIT",
        "copier": False,
        "tools": ["AGENTS.md", "CLAUDE.md"],
        "last_update": "2026-10-01T09:30:00Z",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "tree": ["main.md", "subrepos/"],
        "tree_truncated": False,
        "preview": "# Hello\n\nA preview.",
    }
    base.update(overrides)
    return base


def write_json(path: Path, templates: dict) -> Path:
    path.write_text(
        json.dumps({"generated_at": "2026-11-06T12:00:00Z", "templates": templates}),
        encoding="utf-8",
    )
    return path


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
            "language": "fr",
        },
    )
    return root


def build_site(root: Path, tmp_path: Path, det=None, votes=None) -> Path:
    out = tmp_path / "out"
    det_file = write_json(tmp_path / "detected.json", det) if det is not None else None
    votes_file = (
        write_json(tmp_path / "votes.json", votes) if votes is not None else None
    )
    build(root, out, det_file, votes_file)
    return out


# ---------------------------------------------------------------- structure


def test_outputs_and_byte_identical_schema(repo, tmp_path):
    out = build_site(repo, tmp_path)

    for rel in (
        "index.html",
        "templates/alpha/index.html",
        "templates/beta/index.html",
        "assets/site.css",
        "assets/site.js",
        ".nojekyll",
    ):
        assert (out / rel).is_file(), rel
    source = repo / "schema" / "template.schema.json"
    assert (out / "schema" / "template.schema.json").read_bytes() == source.read_bytes()


def test_cli_with_optional_files(repo, tmp_path, capsys):
    det = write_json(tmp_path / "d.json", {"alpha": detected()})
    out = tmp_path / "cli-out"

    code = main(["--root", str(repo), "--detected", str(det), "--output", str(out)])

    assert code == 0
    assert (out / "index.html").is_file()
    assert "2 template page(s)" in capsys.readouterr().out


def test_unreadable_data_file_is_an_error_but_missing_is_not(repo, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    out = tmp_path / "o"

    assert (
        main(["--root", str(repo), "--detected", str(bad), "--output", str(out)]) == 1
    )
    assert "error:" in capsys.readouterr().err
    missing = tmp_path / "nope.json"
    assert (
        main(["--root", str(repo), "--votes", str(missing), "--output", str(out)]) == 0
    )
    with pytest.raises(BuildError):
        build(repo, out, bad)


def test_site_config_is_read_from_site_yml(repo, tmp_path):
    (repo / "site.yml").write_text(
        "title: Other title\ntagline: Other tagline\n"
        "giscus:\n  repo: o/r\n  repo_id: RID\n  category: Cat\n  category_id: CID\n",
        encoding="utf-8",
    )
    out = build_site(repo, tmp_path)

    index = parse(out / "index.html")
    assert index.find("h1")[0].text() == "Other title"
    assert "Other tagline" in index.text()
    (script,) = parse(out / "templates/alpha/index.html").find(
        "script", src="https://giscus.app/client.js"
    )
    assert script.attrs["data-repo"] == "o/r"
    assert script.attrs["data-repo-id"] == "RID"
    assert script.attrs["data-category"] == "Cat"
    assert script.attrs["data-category-id"] == "CID"
    contributing = [
        a.attrs["href"]
        for a in index.find("a")
        if a.attrs.get("href", "").endswith("CONTRIBUTING.md")
    ]
    assert contributing
    assert all(
        h == "https://github.com/o/r/blob/main/CONTRIBUTING.md" for h in contributing
    )


def test_default_site_config_matches_the_spec():
    from gallery.build import load_site_config

    site = load_site_config(REPO_ROOT / "site.yml")

    assert site.giscus == {
        "repo": "SpecsFirst/gallery",
        "repo_id": "R_kgDOVlEEaQ",
        "category": "Templates",
        "category_id": "DIC_kwDOVlEEac4DKhwb",
    }
    assert site.title and site.tagline


def test_invalid_site_config(tmp_path):
    from gallery.build import load_site_config

    path = tmp_path / "site.yml"
    path.write_text("title: x\ntagline: y\n", encoding="utf-8")
    with pytest.raises(BuildError):
        load_site_config(path)


# ----------------------------------------------------------------- gallery


def test_gallery_card_content(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {**VALID_ENTRY, "name": "Alpha template", "tags": ["thesis", "multi-repo"]},
    )
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(copier=True, license="Apache-2.0")},
        votes={"alpha": {"votes": 3, "discussion_url": "https://github.com/x/y"}},
    )

    index = parse(out / "index.html")
    cards = {c.attrs["data-id"]: c for c in index.find("li") if "data-id" in c.attrs}
    card = cards["alpha"]
    text = card.text()
    assert "Alpha template" in text
    assert VALID_ENTRY["description"] in text
    assert "research" in text and "thesis" in text and "multi-repo" in text
    assert "3 votes" in text
    assert "Apache-2.0" in text
    assert "Copier" in text
    assert "AGENTS.md, CLAUDE.md" in text
    (time,) = [e for e in preview_elements(card) if e.tag == "time"]
    assert time.attrs["datetime"].startswith("2026-10-01")
    assert time.text() == "2026-10-01"
    links = [e for e in index.find("a") if e.attrs.get("href") == "templates/alpha/"]
    assert links and links[0].text() == "Alpha template"
    assert card.attrs["data-kind"] == "research"
    assert json.loads(card.attrs["data-tags"]) == ["thesis", "multi-repo"]
    assert card.attrs["data-votes"] == "3"
    # no detected data: no license, no date, no copier on the other card
    other = cards["beta"].text()
    assert "0 votes" in other and "Copier" not in other and "MIT" not in other


def test_gallery_order_is_by_votes_then_name(repo, tmp_path):
    write_entry(repo, "gamma", {**VALID_ENTRY, "name": "gamma lowercase"})
    write_entry(repo, "delta", {**VALID_ENTRY, "name": "Delta template"})
    out = build_site(
        repo,
        tmp_path,
        votes={"gamma": {"votes": 5}, "beta": {"votes": 5}, "delta": {"votes": 1}},
    )

    index = parse(out / "index.html")
    order = [c.attrs["data-id"] for c in index.find("li") if "data-id" in c.attrs]

    # 5 votes: "Beta template" before "gamma lowercase" (case-insensitive name);
    # then 1 vote; then 0 votes ("Alpha template").
    assert order == ["beta", "gamma", "delta", "alpha"]


def test_votes_data_is_tolerant(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        votes={
            "alpha": {"votes": True},
            "beta": {"votes": "7"},
            "unknown": {"votes": 9},
        },
    )

    index = parse(out / "index.html")
    votes = {
        c.attrs["data-id"]: c.attrs["data-votes"]
        for c in index.find("li")
        if "data-id" in c.attrs
    }
    assert votes == {"alpha": "0", "beta": "0"}


def test_controls_are_hidden_without_javascript(repo, tmp_path):
    out = build_site(repo, tmp_path)

    index = parse(out / "index.html")
    assert "hidden" in index.by_id("filters").attrs
    assert "hidden" in index.by_id("count").attrs
    assert "hidden" in index.by_id("empty").attrs
    assert index.by_id("count").attrs["aria-live"] == "polite"
    assert index.by_id("empty-clear").tag == "button"
    # the full list is server-rendered and visible: no card is hidden
    cards = [c for c in index.find("li") if "data-id" in c.attrs]
    assert len(cards) == 2 and all("hidden" not in c.attrs for c in cards)
    # every control has a real label
    for control in index.find("input") + index.find("select"):
        wrapped = any(a.tag == "label" for a in _ancestors(control))
        labelled = any(
            lab.attrs.get("for") == control.attrs.get("id")
            for lab in index.find("label")
            if "for" in lab.attrs
        )
        assert wrapped or labelled, control.attrs


def _ancestors(element: Element):
    node = element.parent
    while node is not None:
        yield node
        node = node.parent


def test_semantic_landmarks_and_headings(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(preview="# Big\n\n## Sub")})

    for path in all_pages(out):
        page = parse(path)
        assert len(page.find("main")) == 1
        assert page.find("header") and page.find("footer")
        assert page.find("html")[0].attrs["lang"] == "en"
        assert len(page.find("h1")) == 1, path
    preview = parse(out / "templates/alpha/index.html").by_class("preview")[0]
    levels = [e.tag for e in preview.children]
    assert "h1" not in levels and "h2" not in levels
    assert levels[:2] == ["h3", "h4"]


def test_filters_come_only_from_taxonomy(tmp_path):
    taxonomy = """\
kinds:
  zine: Printed things.
  podcast: Audio things.

tags:
  alpha-tag: First tag.
  beta-tag: Second tag.
  loose-tag: In no group.
  old-tag:
    description: Retired.
    deprecated: true
    replaced_by: beta-tag

groups:
  Zeta group: [beta-tag, old-tag]
  Empty group: [old-tag]
  Alpha group: [alpha-tag]
"""
    root = write_registry(tmp_path / "other", taxonomy=taxonomy)
    write_entry(
        root,
        "x",
        {**VALID_ENTRY, "kind": "zine", "tags": ["old-tag", "alpha-tag"]},
    )
    out = tmp_path / "out"
    build(root, out)

    index = parse(out / "index.html")
    kinds = index.find("input", name="kind")
    assert [k.attrs["value"] for k in kinds] == ["", "zine", "podcast"]
    legends = [e.text() for e in index.find("legend")]
    assert legends == ["Kind", "Zeta group", "Alpha group", "Other"]
    tags = [t.attrs["value"] for t in index.find("input", name="tag")]
    assert tags == ["beta-tag", "alpha-tag", "loose-tag"]  # active only, file order
    labels = {
        e.attrs["title"]: e.text().strip()
        for e in index.find("label")
        if "title" in e.attrs
    }
    assert labels["Printed things."] == "zine"
    assert labels["Second tag."] == "beta-tag"
    assert labels["In no group."] == "loose-tag"
    # nothing from the repo's own taxonomy leaks in
    filters = index.by_id("filters")
    shown = filters.text() + " ".join(
        str(v) for e in preview_elements(filters) for v in e.attrs.values()
    )
    for name in ("thesis", "multi-repo", "research", "software", "Project", "Setup"):
        assert name not in shown


def test_no_taxonomy_name_is_hard_coded():
    taxonomy = load_taxonomy(REPO_ROOT / "taxonomy.yml")
    names = [
        *taxonomy.kinds,
        *taxonomy.tags,
        *(group.name for group in taxonomy.groups),
    ]
    files = [REPO_ROOT / "src/gallery/build.py", *(REPO_ROOT / "site").rglob("*")]
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name in names:
            assert not re.search(rf"""["']{re.escape(name)}["']""", text), (path, name)


def test_deprecated_tag_is_resolved_on_card_and_page(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {**VALID_ENTRY, "name": "Alpha template", "tags": ["phd", "thesis"]},
    )
    write_entry(repo, "beta", {**VALID_ENTRY, "name": "Beta template", "tags": ["phd"]})
    out = build_site(repo, tmp_path)

    index = parse(out / "index.html")
    cards = {c.attrs["data-id"]: c for c in index.find("li") if "data-id" in c.attrs}
    for ident in ("alpha", "beta"):
        assert json.loads(cards[ident].attrs["data-tags"]) == ["thesis"]
        chips = [e.text() for e in cards[ident].by_class("chip")]
        assert chips == ["research", "thesis"]
        page = parse(out / "templates" / ident / "index.html")
        assert [e.text() for e in page.by_class("chip")] == ["research", "thesis"]
    # never offered as a filter
    assert "phd" not in [e.attrs["value"] for e in index.find("input", name="tag")]
    assert "phd" not in index.text()


def test_unknown_tag_is_shown_as_is(repo, tmp_path):
    write_entry(
        repo, "alpha", {**VALID_ENTRY, "name": "Alpha template", "tags": ["nope"]}
    )
    out = build_site(repo, tmp_path)

    page = parse(out / "templates/alpha/index.html")
    assert [e.text() for e in page.by_class("chip")] == ["research", "nope"]


# ---------------------------------------------------------- template pages


def test_template_page_content(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {
            **VALID_ENTRY,
            "name": "Alpha template",
            "path": "sub/dir",
            "ref": "v1.2",
            "language": "fr",
        },
    )
    out = build_site(
        repo,
        tmp_path,
        det={
            "alpha": detected(
                copier=True,
                tree=["main.md", "sub/"],
                tree_truncated=True,
                preview="---\nproject: x\n---\n\n# Titre\n\nUn aperçu.",
            )
        },
        votes={"alpha": {"votes": 1}},
    )

    page = parse(out / "templates/alpha/index.html")
    assert page.find("h1")[0].text() == "Alpha template"
    text = page.text()
    assert VALID_ENTRY["description"] in text
    assert "research" in text and "thesis" in text
    assert "1 vote" in text and "1 votes" not in text
    # repo link: https url, text is the url; ref and path are shown as text
    repo_links = [a for a in page.find("a") if a.attrs["href"] == VALID_ENTRY["url"]]
    assert len(repo_links) == 1
    assert repo_links[0].text() == VALID_ENTRY["url"]
    assert repo_links[0].attrs["rel"] == "nofollow noopener noreferrer"
    codes = [c.text() for c in page.find("code")]
    assert "sub/dir" in codes and "v1.2" in codes
    # preview in the entry's language, without the front matter
    preview = page.by_class("preview")[0]
    assert preview.attrs["lang"] == "fr"
    assert "Un aperçu." in preview.text()
    assert "project: x" not in text
    # detected info
    assert "MIT" in text
    assert "AGENTS.md, CLAUDE.md" in text
    assert "0123456" in codes and "0123456789" not in text
    assert "2026-10-01" in text
    tree = page.by_class("tree")[0]
    assert tree.text() == "main.md\nsub/"
    assert "truncated" in text
    # giscus voting note
    assert "GitHub account" in text and "\U0001f44d" in text


def test_template_page_without_truncation_has_no_note(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected()})

    assert "truncated" not in parse(out / "templates/alpha/index.html").text()


def test_no_data_at_all_still_builds(repo, tmp_path):
    out = build_site(repo, tmp_path)

    page = parse(out / "templates/alpha/index.html")
    assert page.find("h1")[0].text() == "Alpha template"
    assert VALID_ENTRY["description"] in page.text()
    assert "0 votes" in page.text()
    assert "not been analysed" in page.by_class("notice")[0].text()
    assert not page.by_class("command") and not page.by_class("preview")
    assert page.find("script", src="https://giscus.app/client.js")
    assert [a for a in page.find("a") if a.attrs["href"] == VALID_ENTRY["url"]]


def test_id_missing_from_files_is_no_data(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected()},
        votes={"alpha": {"votes": 2}},
    )

    page = parse(out / "templates/beta/index.html")
    assert "0 votes" in page.text()
    assert "not been analysed" in page.by_class("notice")[0].text()
    assert "MIT" not in page.text()


@pytest.mark.parametrize(
    "error",
    [
        "timeout",
        "too_large",
        "unreachable",
        "ref_not_found",
        "path_not_found",
        "invalid_entry",
        "fatal: could not read Username for 'https://x': <b>secret</b>",
        None,
    ],
)
def test_failed_template_shows_a_neutral_notice(repo, tmp_path, error):
    failed = {
        "status": "failed",
        "error": error,
        "license": None,
        "copier": None,
        "tools": None,
        "last_update": None,
        "commit": None,
        "tree": None,
        "tree_truncated": None,
        "preview": None,
    }
    out = build_site(repo, tmp_path, det={"alpha": failed})

    page = parse(out / "templates/alpha/index.html")
    notice = page.by_class("notice")[0].text()
    assert "not available" in notice
    html = (out / "templates/alpha/index.html").read_text(encoding="utf-8")
    if error is not None:
        assert error not in html and "secret" not in html
    assert page.find("h1")[0].text() == "Alpha template"
    assert VALID_ENTRY["description"] in page.text()
    assert not page.by_class("command") and not page.by_class("preview")
    assert "Detected information" not in page.text()
    assert page.find("script", src="https://giscus.app/client.js")
    card = [
        c
        for c in parse(out / "index.html").find("li")
        if c.attrs.get("data-id") == "alpha"
    ][0]
    assert "MIT" not in card.text() and "No license" not in card.text()


def test_failed_template_notice_uses_plain_words(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": {"status": "failed", "error": "timeout"}},
    )

    notice = parse(out / "templates/alpha/index.html").by_class("notice")[0].text()
    assert "took too long" in notice


@pytest.mark.parametrize(
    ("license_value", "shown"),
    [
        (None, "No license file"),
        ("unknown", "License not identified"),
        ("CC0-1.0", "CC0-1.0"),
        ("<b>x y</b>", "License not identified"),
    ],
)
def test_license_labels(repo, tmp_path, license_value, shown):
    out = build_site(repo, tmp_path, det={"alpha": detected(license=license_value)})

    card = [
        c
        for c in parse(out / "index.html").find("li")
        if c.attrs.get("data-id") == "alpha"
    ][0]
    assert shown in card.text()


# ---------------------------------------------------------- copier command

URL = "https://github.com/example/repo"


@pytest.mark.parametrize(
    ("path", "ref", "expected"),
    [
        (None, None, f"copier copy {URL} <name>"),
        (None, "v1.0", f"copier copy --vcs-ref v1.0 {URL} <name>"),
        (
            "sub/dir",
            None,
            f"git clone {URL} <name>-src && copier copy <name>-src/sub/dir <name>",
        ),
        (
            "sub/dir",
            "v1.0",
            f"git clone {URL} <name>-src && git -C <name>-src checkout v1.0"
            " && copier copy <name>-src/sub/dir <name>",
        ),
    ],
)
def test_copier_command_forms(repo, tmp_path, path, ref, expected):
    fields = {**VALID_ENTRY, "name": "Alpha template"}
    if path:
        fields["path"] = path
    if ref:
        fields["ref"] = ref
    write_entry(repo, "alpha", fields)
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    page = parse(out / "templates/alpha/index.html")
    (block,) = page.by_class("command")
    (code,) = block.children[0].children
    assert code.tag == "code" and code.text() == expected
    assert copier_command(URL, path, ref) == expected


def test_no_command_when_copier_is_false(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=False)})

    page = parse(out / "templates/alpha/index.html")
    assert not page.by_class("command")
    assert "copier copy" not in page.text()


def test_copier_command_is_shell_quoted_and_refuses_unsafe_values():
    assert (
        copier_command(URL, None, "v1; rm -rf ~")
        == f"copier copy --vcs-ref 'v1; rm -rf ~' {URL} <name>"
    )
    assert "'a b'" in copier_command(URL, "a b", None)
    assert copier_command(URL, None, "--upload-pack=x") is None
    assert copier_command(URL, "../x", None) is None
    assert copier_command(URL, "/etc", None) is None
    assert copier_command("http://github.com/x/y", None, None) is None
    assert copier_command("https://user:pw@github.com/x/y", None, None) is None


# ------------------------------------------------------------------ giscus


def test_giscus_attributes(repo, tmp_path):
    out = build_site(repo, tmp_path)

    page = parse(out / "templates/beta/index.html")
    (script,) = page.find("script", src="https://giscus.app/client.js")
    attrs = script.attrs
    assert attrs["data-repo"] == "SpecsFirst/gallery"
    assert attrs["data-repo-id"] == "R_kgDOVlEEaQ"
    assert attrs["data-category"] == "Templates"
    assert attrs["data-category-id"] == "DIC_kwDOVlEEac4DKhwb"
    assert attrs["data-mapping"] == "specific"
    assert attrs["data-term"] == "beta"
    assert attrs["data-strict"] == "1"
    assert attrs["data-reactions-enabled"] == "1"
    assert attrs["data-input-position"] == "top"
    assert attrs["data-theme"] == "preferred_color_scheme"
    assert attrs["data-loading"] == "lazy"
    # the gallery page has no thread
    assert not parse(out / "index.html").find(
        "script", src="https://giscus.app/client.js"
    )


# ---------------------------------------------------- links, CSP, security


def test_links_are_relative_and_resolve(repo, tmp_path):
    write_entry(repo, "alpha", {**VALID_ENTRY, "name": "Alpha template"})
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    for path in all_pages(out):
        page = parse(path)
        assert not page.find("base")
        for element in page.elements:
            for attr in ("href", "src", "action"):
                value = element.attrs.get(attr)
                if value is None:
                    continue
                parts = urlsplit(value)
                if parts.scheme:
                    assert parts.scheme == "https", value
                    continue
                assert not value.startswith("/"), (path, value)
                if parts.path:
                    target = (path.parent / parts.path).resolve()
                    if target.is_dir():
                        target = target / "index.html"
                    assert target.is_file(), (path, value)
                    assert out.resolve() in target.parents
    index_links = [a.attrs["href"] for a in parse(out / "index.html").find("a")]
    assert "templates/alpha/" in index_links
    page_links = [
        a.attrs["href"] for a in parse(out / "templates/alpha/index.html").find("a")
    ]
    assert "../../" in page_links
    # relative references stay valid under any prefix
    assert urljoin("https://h/gallery/templates/alpha/", "../../assets/site.css") == (
        "https://h/gallery/assets/site.css"
    )


def test_every_page_has_a_content_security_policy(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected()})

    for path in all_pages(out):
        page = parse(path)
        (meta,) = page.find("meta", **{"http-equiv": "Content-Security-Policy"})
        directives = {
            d.split()[0]: d.split()[1:]
            for d in meta.attrs["content"].split(";")
            if d.strip()
        }
        assert directives["default-src"] == ["'none'"]
        assert directives["script-src"] == ["'self'", "https://giscus.app"]
        assert directives["style-src"] == ["'self'", "https://giscus.app"]
        assert directives["frame-src"] == ["https://giscus.app"]
        assert directives["base-uri"] == ["'none'"]
        assert directives["object-src"] == ["'none'"]
        assert "'unsafe-inline'" not in meta.attrs["content"]
        # the CSP comes before anything that could load a resource
        head = [e.tag for e in page.find("head")[0].children]
        assert head.index("meta") < head.index("link") < head.index("script")


def test_no_inline_script_style_or_handler(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    for path in all_pages(out):
        page = parse(path)
        assert not page.find("style")
        for script in page.find("script"):
            assert script.attrs.get("src") and not script.text().strip()
        for element in page.elements:
            assert "style" not in element.attrs
            assert not [a for a in element.attrs if a.startswith("on")]


HOSTILE_PREVIEW = """\
---
title: <script>alert('front')</script>
---

# <script>alert(1)</script> Heading

<script>alert('raw')</script>

<img src="https://tracker.example/p.png" onerror="alert(1)">

<iframe src="https://evil.example"></iframe>

[js](javascript:alert(1)) and [JS](JaVaScRiPt:alert(2))

[data](data:text/html,<b>x</b>)

[rel](relative/page.md) and [root](/etc/passwd) and [proto](//evil.example/x)

[ok](https://example.org/page) and [mail](mailto:a@example.org) and [http](http://example.org/)

![remote alt](https://tracker.example/pixel.png) and ![](https://tracker.example/empty.png)

[![nested alt](https://tracker.example/n.png)](https://example.org/nested)

<https://example.org/auto> and <javascript:alert(3)>

| a | b |
|---|:-:|
| 1 | <b>2</b> |

```
<script>alert('fence')</script>
```
"""


def hostile_site(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {
            **VALID_ENTRY,
            "name": "<script>alert('name')</script>",
            "description": "<img src=x onerror=alert('desc')> long enough description",
            "url": "https://github.com/example/repo",
            "tags": ["<b>tag</b>"],
        },
    )
    tree = ["<script>alert('tree')</script>", "<img src=x onerror=alert(1)>", "a\nb"]
    return build_site(
        repo, tmp_path, det={"alpha": detected(tree=tree, preview=HOSTILE_PREVIEW)}
    )


def test_hostile_values_never_become_markup(repo, tmp_path):
    out = hostile_site(repo, tmp_path)

    for path in [out / "index.html", out / "templates/alpha/index.html"]:
        page = parse(path)
        scripts = [s.attrs.get("src") for s in page.find("script")]
        assert all(
            s
            in (
                "assets/site.js",
                "../../assets/site.js",
                "https://giscus.app/client.js",
            )
            for s in scripts
        ), scripts
        assert not page.find("img")
        assert not page.find("iframe")
        for element in page.elements:
            assert not [a for a in element.attrs if a.startswith("on")]
            href = (element.attrs.get("href") or "").strip().lower()
            assert not href.startswith(("javascript:", "data:", "vbscript:")), href
        # shown literally, as text
        assert "<script>alert('name')</script>" in page.text()
    page = parse(out / "templates/alpha/index.html")
    assert "<img src=x onerror=alert('desc')>" in page.text()
    tree = page.by_class("tree")[0].text()
    assert "<script>alert('tree')</script>" in tree
    assert "<img src=x onerror=alert(1)>" in tree
    assert tree.splitlines()[2:] == ["a b"]  # a newline in a name cannot fake a line
    assert "<b>tag</b>" in [e.text() for e in page.by_class("chip")]
    assert page.find("title")[0].text().startswith("<script>")


def test_hostile_preview_links_and_images(repo, tmp_path):
    out = hostile_site(repo, tmp_path)

    page = parse(out / "templates/alpha/index.html")
    preview = page.by_class("preview")[0]
    anchors = [
        e
        for e in page.elements
        if e.tag == "a" and e.has_ancestor("div") and _in(preview, e)
    ]
    hrefs = [a.attrs["href"] for a in anchors]
    assert hrefs == [
        "https://example.org/page",
        "mailto:a@example.org",
        "http://example.org/",
        "https://example.org/nested",
        "https://example.org/auto",
    ]
    for anchor in anchors:
        assert anchor.attrs["rel"] == "nofollow noopener noreferrer"
        assert set(anchor.attrs) == {"href", "rel"}
    text = preview.text()
    # refused links keep their text and have no anchor
    for label in ("js", "JS", "data", "rel", "root", "proto"):
        assert label in text
    for bad in ("javascript:", "relative/page.md", "/etc/passwd", "//evil.example"):
        assert not any(
            bad in str(v) for e in preview_elements(preview) for v in e.attrs.values()
        ), bad
    # images are their alt text; nothing remote is requested
    assert "remote alt" in text and "nested alt" in text
    assert not [e for e in preview_elements(preview) if e.tag == "img"]
    assert not [
        e
        for e in page.elements
        if any("tracker.example" in str(v) for v in e.attrs.values())
    ]
    assert "[js]" not in text and "[data]" not in text  # the text, not the markdown
    # raw HTML is text, never markup
    assert "<script>alert('raw')</script>" in text
    assert "<iframe" in text
    assert "<b>2</b>" in text and not [
        e for e in preview_elements(preview) if e.tag == "b"
    ]
    # the table is rendered, inside its own scrolling block
    (table,) = [e for e in preview_elements(preview) if e.tag == "table"]
    assert table.parent.attrs["class"] == "table-scroll"
    assert not [e for e in preview_elements(preview) if "style" in e.attrs]
    assert "<script>alert('fence')</script>" in preview.text()


def preview_elements(preview: Element):
    for child in preview.children:
        yield child
        yield from preview_elements(child)


def _in(container: Element, element: Element) -> bool:
    return any(a is container for a in _ancestors(element))


def test_repo_link_requires_https(repo, tmp_path):
    for entry_id, url in {
        "alpha": "javascript:alert(1)",
        "beta": "http://example.org/r",
    }.items():
        write_entry(
            repo, entry_id, {**VALID_ENTRY, "name": f"{entry_id} tpl", "url": url}
        )
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(copier=True), "beta": detected(copier=True)},
    )

    for entry_id, url in {
        "alpha": "javascript:alert(1)",
        "beta": "http://example.org/r",
    }.items():
        page = parse(out / "templates" / entry_id / "index.html")
        assert url in page.text()
        assert not [a for a in page.find("a") if a.attrs["href"] == url]
        assert not page.by_class("command")


def test_invalid_ids_are_skipped(repo, tmp_path, capsys):
    (repo / "templates" / "Bad_Id.yml").write_text("name: Whatever\n", encoding="utf-8")
    out = build_site(repo, tmp_path)

    assert sorted(p.name for p in (out / "templates").iterdir()) == ["alpha", "beta"]
    assert "Bad_Id" in capsys.readouterr().err


# ----------------------------------------------------------- preview unit


def test_front_matter_is_removed():
    assert strip_front_matter("---\na: 1\n---\n# T") == "# T"
    assert strip_front_matter("---\r\na: 1\r\n---\r\n# T") == "# T"
    assert strip_front_matter("---\n---\nbody") == "body"
    assert (
        strip_front_matter("# T\n\n---\nnot: front\n---\n")
        == "# T\n\n---\nnot: front\n---\n"
    )
    assert strip_front_matter("---\nunclosed: 1\n# T") == "---\nunclosed: 1\n# T"


@pytest.mark.parametrize(
    ("href", "allowed"),
    [
        ("https://example.org", True),
        ("HTTP://example.org/x", True),
        ("mailto:a@b.c", True),
        ("javascript:alert(1)", False),
        ("data:text/html,x", False),
        ("relative.md", False),
        ("/abs/path", False),
        ("//host/path", False),
        ("https:///nohost", False),
        ("ftp://example.org", False),
        ("", False),
    ],
)
def test_link_allowed(href, allowed):
    assert link_allowed(href) is allowed


def test_render_preview_shape():
    html = str(
        render_preview(make_preview_renderer(), "[x](https://e.org)\n\n![a<b](u)")
    )

    assert '<a href="https://e.org" rel="nofollow noopener noreferrer">x</a>' in html
    assert "a&lt;b" in html and "<img" not in html


# ------------------------------------------------------------ the real data


def test_real_registry_builds_with_a_detected_file(tmp_path):
    out = tmp_path / "site"
    build(REPO_ROOT, out)

    assert (out / "index.html").is_file()
    assert (out / "templates" / "research-thesis" / "index.html").is_file()
