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
    TagView,
    build,
    build_filters,
    copier_command,
    load_site_config,
    main,
    tag_positions,
)
from gallery.registry import load_taxonomy
from gallery.tree import render_tree
from tests.helpers import VALID_ENTRY, write_entry, write_registry

REPO_ROOT = Path(__file__).resolve().parents[1]

VOID = {"meta", "link", "br", "hr", "input", "img", "base", "path", "rect", "polygon"}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'"
)
URL = "https://github.com/example/repo"


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

    def words(self) -> str:
        return " ".join(self.text().split())

    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def by_class(self, cls: str) -> list[Element]:
        found = []
        for child in self.children:
            if cls in child.classes():
                found.append(child)
            found.extend(child.by_class(cls))
        return found

    def find(self, tag: str) -> list[Element]:
        found = []
        for child in self.children:
            if child.tag == tag:
                found.append(child)
            found.extend(child.find(tag))
        return found


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

    def handle_startendtag(self, tag, attrs):
        element = Element(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(element)
        self.elements.append(element)

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

    def cards(self) -> dict[str, Element]:
        found = [c for c in self.find("article") if "data-id" in c.attrs]
        return {c.attrs["data-id"]: c for c in found}

    def card_order(self) -> list[str]:
        return list(self.cards())


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
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out"
    det_file = write_json(tmp_path / "detected.json", det) if det is not None else None
    votes_file = (
        write_json(tmp_path / "votes.json", votes) if votes is not None else None
    )
    build(root, out, det_file, votes_file)
    return out


def card_of(out: Path, entry_id: str = "alpha") -> Element:
    return parse(out / "index.html").cards()[entry_id]


def page_of(out: Path, entry_id: str = "alpha") -> Page:
    return parse(out / "templates" / entry_id / "index.html")


def page_card(out: Path, entry_id: str = "alpha") -> Element:
    return page_of(out, entry_id).by_class("card")[0]


def meta(element: Element) -> dict[str, Element]:
    """The <dl> of a card or page, term -> <dd>."""
    (dl,) = element.find("dl")
    terms = [c.text() for c in dl.children if c.tag == "dt"]
    values = [c for c in dl.children if c.tag == "dd"]
    return dict(zip(terms, values, strict=True))


def star_text(element: Element) -> list[str]:
    return [c.text() for c in star_of(element).children if c.tag == "span"]


def star_of(element: Element) -> Element:
    (star,) = element.by_class("star")
    return star


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


def test_fonts_and_their_license_are_copied(repo, tmp_path):
    out = build_site(repo, tmp_path)

    expected = {
        "inter-latin-400-normal.woff2",
        "inter-latin-500-normal.woff2",
        "inter-latin-600-normal.woff2",
        "jetbrains-mono-latin-400-normal.woff2",
        "jetbrains-mono-latin-500-normal.woff2",
        "OFL.txt",
    }
    shipped = {p.name for p in (out / "assets" / "fonts").iterdir()}
    assert shipped == expected
    for name in expected:
        source = REPO_ROOT / "site" / "assets" / "fonts" / name
        assert (out / "assets" / "fonts" / name).read_bytes() == source.read_bytes()
    for name in expected - {"OFL.txt"}:
        assert (out / "assets" / "fonts" / name).read_bytes()[:4] == b"wOF2"
    assert "SIL OPEN FONT LICENSE Version 1.1" in (
        out / "assets" / "fonts" / "OFL.txt"
    ).read_text(encoding="utf-8")


def test_fonts_are_declared_for_each_weight_with_swap():
    css = (REPO_ROOT / "site" / "assets" / "site.css").read_text(encoding="utf-8")

    faces = re.findall(r"@font-face\s*\{(.*?)\}", css, re.DOTALL)
    declared = set()
    for face in faces:
        family = re.search(r'font-family:\s*"([^"]+)"', face).group(1)
        weight = re.search(r"font-weight:\s*(\d+)", face).group(1)
        assert "font-display: swap" in face
        url = re.search(r'url\("([^"]+)"\)\s*format\("woff2"\)', face).group(1)
        assert (REPO_ROOT / "site" / "assets" / url).is_file()
        declared.add((family, weight))
    assert declared == {
        ("Inter", "400"),
        ("Inter", "500"),
        ("Inter", "600"),
        ("JetBrains Mono", "400"),
        ("JetBrains Mono", "500"),
    }
    assert "Inter, system-ui, sans-serif" in css
    assert 'ui-monospace, monospace' in css


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
        "title: Other title\n"
        "discussions:\n  repo: o/r\n  repo_id: RID\n  category: Cat\n"
        "  category_id: CID\n",
        encoding="utf-8",
    )
    out = build_site(repo, tmp_path)

    index = parse(out / "index.html")
    assert index.find("title")[0].text() == "Other title"
    hrefs = [a.attrs["href"] for a in index.find("a")]
    assert "https://github.com/o/r" in hrefs
    assert "https://github.com/o/r/issues/new?template=submit-template.yml" in hrefs
    star = star_of(index.cards()["alpha"])
    assert star.attrs["href"] == "https://github.com/o/r/discussions"


def test_default_site_config_matches_the_spec():
    site = load_site_config(REPO_ROOT / "site.yml")

    assert site.discussions == {
        "repo": "SpecsFirst/gallery",
        "repo_id": "R_kgDOVlEEaQ",
        "category": "Templates",
        "category_id": "DIC_kwDOVlEEac4DKhwb",
    }
    assert site.title
    assert site.repo_url == "https://github.com/SpecsFirst/gallery"
    assert site.submit_url == (
        "https://github.com/SpecsFirst/gallery/issues/new?template=submit-template.yml"
    )
    assert "giscus" not in (REPO_ROOT / "site.yml").read_text(encoding="utf-8")
    assert "tagline" not in (REPO_ROOT / "site.yml").read_text(encoding="utf-8")


def test_invalid_site_config(tmp_path):
    path = tmp_path / "site.yml"
    path.write_text("title: x\n", encoding="utf-8")
    with pytest.raises(BuildError):
        load_site_config(path)
    path.write_text("title: x\ngiscus:\n  repo: a/b\n", encoding="utf-8")
    with pytest.raises(BuildError):
        load_site_config(path)
    path.write_text("title: x\ndiscussions:\n  repo: a/b\n", encoding="utf-8")
    with pytest.raises(BuildError):
        load_site_config(path)


# --------------------------------------------------------------- home page


def test_home_header_and_intro(repo, tmp_path):
    out = build_site(repo, tmp_path)

    index = parse(out / "index.html")
    (header,) = index.find("header")
    brand = header.by_class("brand")[0]
    assert brand.attrs["href"] == "./"
    assert brand.words() == "SpecsFirst"
    (logo,) = brand.find("svg")
    assert logo.attrs["aria-hidden"] == "true"
    assert 'fill="currentColor"' in (out / "index.html").read_text(encoding="utf-8")
    links = {a.words(): a.attrs["href"] for a in header.find("a")}
    assert links["GitHub"] == "https://github.com/SpecsFirst/gallery"
    submit = links["Submit a template"]
    assert submit == (
        "https://github.com/SpecsFirst/gallery/issues/new?template=submit-template.yml"
    )
    (h1,) = index.find("h1")
    assert h1.text() == "Spec templates for agent-driven projects."
    intro = index.by_class("intro")[0]
    assert intro.find("p")[0].text().startswith("A specs repo holds what a project")
    assert intro.find("p")[0].text().endswith("start yours with a single command.")
    assert not index.find("footer")
    assert len(index.find("main")) == 1


def test_logo_is_the_path_of_the_mockup_logo(repo, tmp_path):
    out = build_site(repo, tmp_path)

    html = (out / "index.html").read_text(encoding="utf-8")
    logo = re.search(r'<path fill="currentColor"[^>]*\sd="([^"]+)"', html).group(1)
    assert logo.startswith("m -106.4686,202.80829")
    assert logo.endswith("1.225336,-1.14387 z")
    template = (out / "templates/alpha/index.html").read_text(encoding="utf-8")
    assert logo in template


def test_card_structure_and_order_of_elements(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected()})

    card = card_of(out)
    head, dl, tree = card.children
    assert head.classes() == ["card-head"]
    text_block, star = head.children
    assert [c.tag for c in text_block.children] == ["h2", "p"]
    (title,) = text_block.children[0].children
    assert title.attrs["href"] == "templates/alpha/"
    assert title.text() == "Alpha template"
    assert text_block.children[1].text() == VALID_ENTRY["description"]
    assert star.attrs["href"].startswith("https://github.com/")
    assert dl.tag == "dl" and list(meta(card)) == [
        "License",
        "Updated",
        "Source",
        "Tags",
    ]
    assert tree.classes() == ["tree"]


def test_removed_elements_are_gone(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(copier=True)},
        votes={"alpha": {"votes": 3, "discussion_url": URL}},
    )

    for path in all_pages(out):
        html = path.read_text(encoding="utf-8")
        for gone in (
            "giscus",
            "Detected information",
            "Preview",
            "AGENTS.md",
            "CLAUDE.md",
            "A preview.",
            "0123456",
            "<footer",
            "<select",
            "<fieldset",
            "Language",
        ):
            assert gone not in html, (path, gone)


def test_no_template_message(tmp_path):
    root = write_registry(tmp_path / "empty")
    out = tmp_path / "out"
    build(root, out)

    index = parse(out / "index.html")
    assert "No template has been published yet." in index.text()
    assert not index.find("article")
    assert not index.find("button")


def test_controls_are_hidden_without_javascript(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    index = parse(out / "index.html")
    group = index.by_id("filters")
    assert group.attrs["role"] == "group"
    assert group.attrs["aria-label"] == "Filter by tag"
    buttons = group.find("button")
    assert buttons and all("hidden" in b.attrs for b in buttons)
    assert all(b.attrs["type"] == "button" for b in buttons)
    # nothing else is hidden: every card shows and the count is readable
    cards = index.find("article")
    assert len(cards) == 2 and all("hidden" not in c.attrs for c in cards)
    count = index.by_id("count")
    assert "hidden" not in count.attrs
    assert count.attrs["aria-live"] == "polite"
    assert count.text() == "2 templates, sorted by stars"
    copy = page_of(out).by_class("copy")[0]
    assert copy.tag == "button" and "hidden" in copy.attrs
    assert copy.attrs["type"] == "button"
    assert copy.attrs["aria-label"] == "Copy command"


def test_count_is_singular_for_one_template(tmp_path):
    root = write_registry(tmp_path / "one")
    write_entry(root, "solo", {**VALID_ENTRY, "name": "Solo template"})
    out = tmp_path / "out"
    build(root, out)

    assert parse(out / "index.html").by_id("count").text() == (
        "1 template, sorted by stars"
    )


def test_semantic_landmarks_and_headings(repo, tmp_path):
    out = build_site(repo, tmp_path)

    for path in all_pages(out):
        page = parse(path)
        assert len(page.find("main")) == 1
        assert page.find("html")[0].attrs["lang"] == "en"
        assert len(page.find("h1")) == 1, path
        assert page.find("meta", name="color-scheme")[0].attrs["content"] == (
            "light dark"
        )
    index = parse(out / "index.html")
    assert [h.tag for h in index.find("h2")] == ["h2", "h2"]


# --------------------------------------------------------- filters and tags

TAXONOMY = """\
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
  t5: Five.
  t6: Six.
  t7: Seven.

groups:
  Zeta group: [beta-tag, old-tag]
  Alpha group: [alpha-tag]
"""


def tag_site(tmp_path, entries):
    root = write_registry(tmp_path / "other", taxonomy=TAXONOMY)
    for ident, fields in entries.items():
        write_entry(root, ident, {**VALID_ENTRY, **fields})
    out = tmp_path / "out"
    build(root, out)
    return out


def test_filter_offers_only_used_tags_in_taxonomy_order(tmp_path):
    out = tag_site(
        tmp_path,
        {
            "x": {"kind": "podcast", "tags": ["loose-tag", "alpha-tag"]},
            "y": {"kind": "zine", "tags": ["old-tag", "t6"]},
        },
    )

    group = parse(out / "index.html").by_id("filters")
    names = [b.attrs["data-tag"] for b in group.find("button")]
    # kinds first (file order), then tags (file order); unused ones are absent;
    # the deprecated old-tag is shown as beta-tag, and nothing from the repo's own
    # taxonomy leaks in
    assert names == ["", "zine", "podcast", "alpha-tag", "beta-tag", "loose-tag", "t6"]
    labels = [b.words() for b in group.find("button")]
    assert labels == ["All", *names[1:]]
    assert group.find("button")[0].attrs["aria-pressed"] == "true"
    assert all(b.attrs["aria-pressed"] == "false" for b in group.find("button")[1:])
    for button in group.find("button")[1:]:
        (dot,) = button.by_class("dot")
        assert any(re.fullmatch(r"c[0-5]", c) for c in dot.classes())


def test_filter_leaves_out_kinds_and_tags_nobody_uses(tmp_path):
    out = tag_site(tmp_path, {"x": {"kind": "zine", "tags": []}})

    group = parse(out / "index.html").by_id("filters")
    names = [b.attrs["data-tag"] for b in group.find("button")]
    assert names == ["", "zine"]


def test_tag_color_is_the_taxonomy_position_modulo_six(tmp_path):
    out = tag_site(
        tmp_path,
        {"x": {"kind": "podcast", "tags": ["t7", "alpha-tag", "old-tag"]}},
    )

    # kinds: zine 0, podcast 1; tags: alpha 2, beta 3, loose 4, old 5, t5 6, t6 7,
    # t7 8 -> 8 % 6 = 2
    positions = tag_positions(load_taxonomy(tmp_path / "other" / "taxonomy.yml"))
    assert positions["podcast"] == 1 and positions["old-tag"] == 5
    assert positions["t7"] == 8
    card = parse(out / "index.html").cards()["x"]
    colors = {
        s.words(): [c for c in s.by_class("dot")[0].classes()]
        for s in meta(card)["Tags"].children
    }
    assert colors == {
        "podcast": ["dot", "c1"],
        "t7": ["dot", "c2"],
        "alpha-tag": ["dot", "c2"],
        "beta-tag": ["dot", "c3"],  # old-tag resolved to beta-tag
    }
    # the filter and the card agree
    group = parse(out / "index.html").by_id("filters")
    by_name = {
        b.attrs["data-tag"]: b.by_class("dot")[0].classes()
        for b in group.find("button")[1:]
    }
    assert by_name["t7"] == ["dot", "c2"] and by_name["podcast"] == ["dot", "c1"]


def test_unknown_tag_is_shown_last_with_the_first_color(tmp_path):
    out = tag_site(tmp_path, {"x": {"kind": "zine", "tags": ["nope", "t5"]}})

    index = parse(out / "index.html")
    names = [b.attrs["data-tag"] for b in index.by_id("filters").find("button")]
    assert names == ["", "zine", "t5", "nope"]
    tags = meta(index.cards()["x"])["Tags"]
    assert [s.words() for s in tags.children] == ["zine", "nope", "t5"]
    assert tags.children[1].by_class("dot")[0].classes() == ["dot", "c0"]


def test_kind_comes_first_without_duplicates(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {
            **VALID_ENTRY,
            "name": "Alpha template",
            "tags": ["thesis", "research", "thesis", "multi-repo"],
        },
    )
    out = build_site(repo, tmp_path)

    card = card_of(out)
    shown = [s.words() for s in meta(card)["Tags"].children]
    assert shown == ["research", "thesis", "multi-repo"]
    assert json.loads(card.attrs["data-tags"]) == shown
    page_tags = [s.words() for s in meta(page_card(out))["Tags"].children]
    assert page_tags == shown


def test_deprecated_tag_is_resolved_on_card_and_page(repo, tmp_path):
    write_entry(
        repo,
        "alpha",
        {**VALID_ENTRY, "name": "Alpha template", "tags": ["phd", "thesis"]},
    )
    write_entry(repo, "beta", {**VALID_ENTRY, "name": "Beta template", "tags": ["phd"]})
    out = build_site(repo, tmp_path)

    index = parse(out / "index.html")
    for ident in ("alpha", "beta"):
        assert json.loads(index.cards()[ident].attrs["data-tags"]) == [
            "research",
            "thesis",
        ]
        page = page_of(out, ident)
        assert [s.words() for s in meta(page.by_class("card")[0])["Tags"].children] == [
            "research",
            "thesis",
        ]
    assert "phd" not in index.text()
    assert "phd" not in [b.attrs["data-tag"] for b in index.find("button")]


def test_build_filters_unit(repo):
    taxonomy = load_taxonomy(repo / "taxonomy.yml")

    class V:
        def __init__(self, *names):
            self.tags = [TagView(n, 0) for n in names]

    assert [t.name for t in build_filters([V("thesis"), V("research")], taxonomy)] == [
        "research",
        "thesis",
    ]
    assert build_filters([], taxonomy) == []


# ---------------------------------------------------------------- the stars


def test_star_on_with_discussion_link(repo, tmp_path):
    discussion = "https://github.com/SpecsFirst/gallery/discussions/7"
    out = build_site(
        repo,
        tmp_path,
        votes={"alpha": {"votes": 3, "discussion_url": discussion}},
    )

    star = star_of(card_of(out))
    assert star.tag == "a"
    assert star.attrs["href"] == discussion
    assert star.attrs["target"] == "_blank"
    assert star.attrs["rel"] == "noopener"
    assert star.attrs["title"] == "Star on GitHub: opens the discussion, react with 👍"
    assert star.attrs["aria-label"] == "Star Alpha template on GitHub, 3 stars"
    assert "on" in star.classes()
    assert star.words() == "3"
    assert star.find("svg")[0].attrs["aria-hidden"] == "true"


def test_star_off_when_there_is_no_vote(repo, tmp_path):
    out = build_site(repo, tmp_path)

    star = star_of(card_of(out))
    assert "on" not in star.classes() and star.classes() == ["star"]
    assert star.words() == "0"
    assert star.attrs["aria-label"] == "Star Alpha template on GitHub, 0 stars"


def test_star_label_is_singular_for_one(repo, tmp_path):
    out = build_site(repo, tmp_path, votes={"alpha": {"votes": 1}})

    star = star_of(card_of(out))
    assert star.attrs["aria-label"] == "Star Alpha template on GitHub, 1 star"
    assert "on" in star.classes()


@pytest.mark.parametrize(
    "discussion_url",
    [
        None,
        "",
        42,
        "http://github.com/SpecsFirst/gallery/discussions/1",
        "https://evil.example/https://github.com/",
        "javascript:alert(1)",
        "https://github.com.evil.example/x",
        "https://github.com/a b",
    ],
)
def test_star_href_falls_back_to_the_discussions_page(repo, tmp_path, discussion_url):
    out = build_site(
        repo,
        tmp_path,
        votes={"alpha": {"votes": 2, "discussion_url": discussion_url}},
    )

    assert star_of(card_of(out)).attrs["href"] == (
        "https://github.com/SpecsFirst/gallery/discussions"
    )
    assert star_of(page_of(out)).attrs["href"] == (
        "https://github.com/SpecsFirst/gallery/discussions"
    )


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
    assert [star_of(c).words() for c in index.cards().values()] == ["0", "0"]


def test_template_page_star_button(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        votes={"alpha": {"votes": 4, "discussion_url": "https://github.com/o/r/discussions/3"}},
    )

    star = star_of(page_of(out))
    assert star.attrs["href"] == "https://github.com/o/r/discussions/3"
    assert star.attrs["aria-label"] == "Star Alpha template on GitHub, 4 stars"
    assert "on" in star.classes()
    assert [c.text() for c in star.children if c.tag == "span"] == ["Star", "4"]
    assert star.by_class("n")[0].text() == "4"


# ------------------------------------------------------ the data of a card


@pytest.mark.parametrize(
    ("license_value", "shown", "css"),
    [
        (None, "None", ["warn"]),
        ("unknown", "Unknown", []),
        ("CC0-1.0", "CC0-1.0", []),
        ("Apache-2.0", "Apache-2.0", []),
        ("<b>x y</b>", "Unknown", []),
        (12, "Unknown", []),
    ],
)
def test_license_values(repo, tmp_path, license_value, shown, css):
    out = build_site(repo, tmp_path, det={"alpha": detected(license=license_value)})

    for element in (card_of(out), page_card(out)):
        dd = meta(element)["License"]
        assert dd.text() == shown
        assert dd.classes() == css


@pytest.mark.parametrize("state", ["failed", "missing", "not-run"])
def test_license_is_unknown_and_muted_without_detection(repo, tmp_path, state):
    det = {"alpha": FAILED} if state == "failed" else None
    if state == "missing":
        det = {"beta": detected()}
    out = build_site(repo, tmp_path, det=det)

    dd = meta(card_of(out))["License"]
    assert dd.text() == "Unknown" and dd.classes() == ["muted"]
    updated = meta(card_of(out))["Updated"]
    assert updated.text() == "Unknown" and updated.classes() == ["muted"]
    assert not updated.find("time")


@pytest.mark.parametrize(
    ("stamp", "shown", "iso"),
    [
        ("2026-11-06T12:00:00Z", "Nov 6, 2026", "2026-11-06"),
        ("2026-01-01T00:00:00+00:00", "Jan 1, 2026", "2026-01-01"),
        ("2025-12-31T23:59:59Z", "Dec 31, 2025", "2025-12-31"),
        ("2026-03-09T10:00:00", "Mar 9, 2026", "2026-03-09"),
        ("2026-05-02T01:00:00+05:00", "May 1, 2026", "2026-05-01"),
    ],
)
def test_updated_date_format(repo, tmp_path, stamp, shown, iso):
    out = build_site(repo, tmp_path, det={"alpha": detected(last_update=stamp)})

    for element in (card_of(out), page_card(out)):
        (time,) = meta(element)["Updated"].find("time")
        assert time.text() == shown
        assert time.attrs["datetime"] == iso


@pytest.mark.parametrize("stamp", [None, "yesterday", 5])
def test_updated_is_unknown_when_absent_or_unreadable(repo, tmp_path, stamp):
    out = build_site(repo, tmp_path, det={"alpha": detected(last_update=stamp)})

    dd = meta(card_of(out))["Updated"]
    assert dd.text() == "Unknown" and dd.classes() == ["muted"]


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "https://github.com/SpecsFirst/template-research-thesis",
            "github.com/SpecsFirst/template-research-thesis",
        ),
        (
            "https://gitlab.inria.fr/dev-studio/specs-template/",
            "gitlab.inria.fr/dev-studio/specs-template",
        ),
    ],
)
def test_source_link_text(repo, tmp_path, url, shown):
    write_entry(repo, "alpha", {**VALID_ENTRY, "name": "Alpha template", "url": url})
    out = build_site(repo, tmp_path)

    for element in (card_of(out), page_card(out)):
        (link,) = meta(element)["Source"].find("a")
        assert link.attrs["href"] == url
        assert link.attrs["rel"] == "nofollow noopener noreferrer"
        assert link.text() == f"{shown} ↗"
        (arrow,) = link.find("span")
        assert arrow.text() == "↗" and arrow.attrs["aria-hidden"] == "true"


def test_repo_link_requires_https(repo, tmp_path):
    bad = {"alpha": "javascript:alert(1)", "beta": "http://example.org/r"}
    for entry_id, url in bad.items():
        write_entry(
            repo, entry_id, {**VALID_ENTRY, "name": f"{entry_id} tpl", "url": url}
        )
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(copier=True), "beta": detected(copier=True)},
    )

    for entry_id, url in bad.items():
        for element in (card_of(out, entry_id), page_card(out, entry_id)):
            source = meta(element)["Source"]
            assert source.text() == url
            assert not source.find("a")
        page = page_of(out, entry_id)
        assert not [a for a in page.find("a") if a.attrs["href"] == url]
        assert not page.by_class("cmd")


# ----------------------------------------------------------------- the order


def test_order_is_stars_then_update_then_name_then_id(tmp_path):
    root = write_registry(tmp_path / "order")
    for ident, name in {
        "a1": "Same name",
        "a2": "Same name",
        "b": "bravo",
        "c": "Charlie",
        "d": "Delta",
        "e": "Echo",
        "f": "Foxtrot",
        "g": "Golf",
    }.items():
        write_entry(root, ident, {**VALID_ENTRY, "name": name})
    det = {
        "a1": detected(last_update="2026-10-01T00:00:00Z"),
        "a2": detected(last_update="2026-10-01T00:00:00Z"),
        "b": detected(last_update="2026-10-01T00:00:00Z"),
        "c": detected(last_update="2026-10-01T00:00:00Z"),
        "d": detected(last_update="2026-10-03T00:00:00Z"),
        "e": detected(last_update="2026-10-02T00:00:00Z"),
        "f": FAILED,
        # g: no detected data
    }
    votes = {"c": {"votes": 5}, "d": {"votes": 1}, "e": {"votes": 1}, "f": {"votes": 1}}
    out = tmp_path / "out"
    build(
        root,
        out,
        write_json(tmp_path / "det.json", det),
        write_json(tmp_path / "votes.json", votes),
    )

    order = parse(out / "index.html").card_order()

    # 5 stars: c. 1 star: d (Oct 3), e (Oct 2), f (unknown date, last). 0 stars: the
    # same update date, then name case-insensitively ("bravo" before "Same name"),
    # then id; unknown date last, "g" and "f" never ahead of a known one.
    assert order == ["c", "d", "e", "f", "b", "a1", "a2", "g"]


# ----------------------------------------------------------------- the tree


def test_tree_block_from_the_detected_list(repo, tmp_path):
    tree = ["README.md", "docs/", "docs/guide.md", ".github/", ".github/ci.yml"]
    tree += ["src/", "src/a.py"]
    out = build_site(repo, tmp_path, det={"alpha": detected(tree=tree)})

    rendered = render_tree(tree)
    for element in (card_of(out), page_card(out)):
        block = element.by_class("tree")[0]
        (pre,) = block.find("pre")
        assert pre.text() == rendered.text
        assert pre.attrs["tabindex"] == "0"
        assert not pre.find("code") and not pre.children
        sums = block.by_class("sum")
        assert [s.text() for s in sums] == [rendered.summary]
        assert rendered.summary == "2 directories, 3 files"
        assert "github" not in pre.text()  # dot entries are hidden


def test_tree_truncation_note(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(tree_truncated=True)})

    sums = [s.text() for s in card_of(out).by_class("sum")]
    assert sums == ["1 directory, 1 file", "Only the first 500 entries are listed."]


def test_no_truncation_note_by_default(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected()})

    assert "Only the first" not in parse(out / "index.html").text()
    assert "Only the first" not in page_of(out).text()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("timeout", "the repository took too long to fetch"),
        ("too_large", "the repository is too large to analyse"),
        ("unreachable", "the repository could not be fetched"),
        ("ref_not_found", "the requested ref was not found in the repository"),
        ("path_not_found", "the requested path was not found in the repository"),
        ("invalid_entry", "the path or ref of this entry is not usable"),
        ("fatal: could not read Username: <b>secret</b>", "the analysis failed"),
        (None, "the analysis failed"),
        (["x"], "the analysis failed"),
    ],
)
def test_tree_unavailable_when_detection_failed(repo, tmp_path, error, reason):
    out = build_site(repo, tmp_path, det={"alpha": {**FAILED, "error": error}})

    expected = f"File tree unavailable: {reason}."
    for element in (card_of(out), page_card(out)):
        block = element.by_class("tree")[0]
        assert not block.find("pre")
        assert [s.text() for s in block.by_class("sum")] == [expected]
    html = (out / "templates/alpha/index.html").read_text(encoding="utf-8")
    assert "secret" not in html and "Username" not in html
    # the card still shows the entry's own fields
    assert page_of(out).find("h1")[0].text() == "Alpha template"
    assert VALID_ENTRY["description"] in page_of(out).text()
    assert meta(card_of(out))["Source"].find("a")


def test_tree_unavailable_when_not_analysed(repo, tmp_path):
    for det in (None, {"beta": detected()}):
        out = build_site(repo, tmp_path / str(bool(det)), det=det)

        block = card_of(out).by_class("tree")[0]
        assert [s.text() for s in block.by_class("sum")] == [
            "File tree unavailable: not analysed yet."
        ]


# ------------------------------------------------------------ template page


def test_template_page_content_and_order(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(copier=True)},
        votes={"alpha": {"votes": 1}},
    )

    page = page_of(out)
    (header,) = page.find("header")
    assert header.by_class("brand")[0].attrs["href"] == "../../"
    assert header.by_class("brand")[0].words() == "SpecsFirst"
    (back,) = header.find("nav")[0].find("a")
    assert back.attrs["href"] == "../../" and back.text() == "← All templates"
    (card,) = page.by_class("card")
    head, dl, tree, cmd = card.children
    assert head.classes() == ["head"]
    assert head.find("h1")[0].text() == "Alpha template"
    assert head.find("p")[0].text() == VALID_ENTRY["description"]
    assert star_of(head).attrs["aria-label"] == "Star Alpha template on GitHub, 1 star"
    assert dl.tag == "dl"
    assert list(meta(card)) == ["License", "Updated", "Source", "Tags"]
    assert tree.classes() == ["tree"]
    assert cmd.classes() == ["cmd"]
    assert page.find("title")[0].text() == "Alpha template - SpecsFirst gallery"


def test_command_block(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    page = page_of(out)
    (cmd,) = page.by_class("cmd")
    (line,) = cmd.by_class("line")
    prompt, code = line.children
    assert prompt.text() == "$ " and prompt.attrs["aria-hidden"] == "true"
    assert code.tag == "code"
    assert code.text() == f"copier copy {URL} my-project"
    assert line.attrs["tabindex"] == "0"
    (button,) = cmd.find("button")
    assert button.attrs["type"] == "button"
    assert button.attrs["aria-label"] == "Copy command"
    assert "hidden" in button.attrs
    assert [s.attrs["aria-hidden"] for s in button.find("svg")] == ["true", "true"]
    assert len(button.find("svg")) == 2
    # the home page has no command
    assert not parse(out / "index.html").by_class("cmd")


@pytest.mark.parametrize(
    ("path", "ref", "expected"),
    [
        (None, None, f"copier copy {URL} my-project"),
        (None, "v1.0", f"copier copy --vcs-ref v1.0 {URL} my-project"),
        (
            "sub/dir",
            None,
            f"git clone {URL} my-project-src"
            " && copier copy my-project-src/sub/dir my-project",
        ),
        (
            "sub/dir",
            "v1.0",
            f"git clone {URL} my-project-src && git -C my-project-src checkout v1.0"
            " && copier copy my-project-src/sub/dir my-project",
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

    (code,) = page_of(out).by_class("cmd")[0].find("code")
    assert code.text() == expected
    assert copier_command(URL, path, ref) == expected
    assert "<name>" not in expected


@pytest.mark.parametrize("copier", [False, None, "yes"])
def test_no_command_unless_detect_found_a_copier_yml(repo, tmp_path, copier):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=copier)})

    page = page_of(out)
    assert not page.by_class("cmd") and not page.find("button")
    assert "copier copy" not in page.text()


def test_no_command_without_detection(repo, tmp_path):
    for det in (None, {"alpha": FAILED}):
        out = build_site(repo, tmp_path / str(det is None), det=det)

        assert not page_of(out).by_class("cmd")


def test_copier_command_is_shell_quoted_and_refuses_unsafe_values():
    assert (
        copier_command(URL, None, "v1; rm -rf ~")
        == f"copier copy --vcs-ref 'v1; rm -rf ~' {URL} my-project"
    )
    assert "'a b'" in copier_command(URL, "a b", None)
    assert copier_command(URL, None, "--upload-pack=x") is None
    assert copier_command(URL, "../x", None) is None
    assert copier_command(URL, "/etc", None) is None
    assert copier_command("http://github.com/x/y", None, None) is None
    assert copier_command("https://user:pw@github.com/x/y", None, None) is None


def test_no_data_at_all_still_builds(repo, tmp_path):
    out = build_site(repo, tmp_path)

    page = page_of(out)
    assert page.find("h1")[0].text() == "Alpha template"
    assert VALID_ENTRY["description"] in page.text()
    assert star_text(page) == ["Star", "0"]
    assert [a for a in page.find("a") if a.attrs["href"] == VALID_ENTRY["url"]]


def test_id_missing_from_files_is_no_data(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected()},
        votes={"alpha": {"votes": 2}},
    )

    page = page_of(out, "beta")
    assert star_text(page) == ["Star", "0"]
    assert meta(page.by_class("card")[0])["License"].text() == "Unknown"
    assert "MIT" not in page.text()


# ---------------------------------------------------- links, CSP, security


def test_links_are_relative_and_resolve(repo, tmp_path):
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
    assert "../../" in [a.attrs["href"] for a in page_of(out).find("a")]
    assert urljoin("https://h/gallery/templates/alpha/", "../../assets/site.css") == (
        "https://h/gallery/assets/site.css"
    )


def test_every_page_has_the_exact_content_security_policy(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    for path in all_pages(out):
        page = parse(path)
        (meta_tag,) = page.find("meta", **{"http-equiv": "Content-Security-Policy"})
        assert meta_tag.attrs["content"] == CSP
        # the CSP comes before anything that could load a resource
        head = [e.tag for e in page.find("head")[0].children]
        assert head.index("meta") < head.index("link") < head.index("script")


def test_no_inline_script_style_or_handler(repo, tmp_path):
    out = build_site(repo, tmp_path, det={"alpha": detected(copier=True)})

    for path in all_pages(out):
        page = parse(path)
        assert not page.find("style")
        scripts = page.find("script")
        assert [s.attrs.get("src") for s in scripts] and all(
            s.attrs.get("src") and not s.text().strip() for s in scripts
        )
        for element in page.elements:
            assert "style" not in element.attrs
            assert not [a for a in element.attrs if a.startswith("on")]
        html = path.read_text(encoding="utf-8")
        assert "style=" not in html
        assert not re.search(r"<script(?![^>]*\ssrc=)", html)


def test_no_third_party_origin_anywhere_in_the_output(repo, tmp_path):
    out = build_site(
        repo,
        tmp_path,
        det={"alpha": detected(copier=True)},
        votes={"alpha": {"votes": 1, "discussion_url": "https://github.com/o/r/discussions/1"}},
    )

    pages = {p.read_text(encoding="utf-8") for p in all_pages(out)}
    for html in pages:
        for needle in ("giscus", "googleapis", "gstatic", "fonts.google"):
            assert needle not in html
        for url in re.findall(r"https?://[^\s\"'<>)]+", html):
            host = urlsplit(url).hostname
            assert host in {
                "github.com",
                "gitlab.inria.fr",
                None,
            } or url.startswith("https://github.com/example/"), url
    for asset in ("site.css", "site.js"):
        text = (out / "assets" / asset).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text
        assert "googleapis" not in text and "giscus" not in text
    assert "url(" in (out / "assets" / "site.css").read_text(encoding="utf-8")


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
    tree = ["<script>alert('tree')<script>", "<img src=x onerror=alert(1)>", "a\nb"]
    return build_site(
        repo,
        tmp_path,
        det={"alpha": detected(tree=tree, copier=True, license="<i>x</i>")},
        votes={"alpha": {"votes": 1, "discussion_url": "https://github.com/x/y?a=\"><script>"}},
    )


def test_hostile_values_never_become_markup(repo, tmp_path):
    out = hostile_site(repo, tmp_path)

    for path in [out / "index.html", out / "templates/alpha/index.html"]:
        page = parse(path)
        scripts = [s.attrs.get("src") for s in page.find("script")]
        assert all(s.endswith("assets/site.js") for s in scripts), scripts
        assert not page.find("img")
        assert not page.find("iframe")
        assert not page.find("b") and not page.find("i")
        for element in page.elements:
            assert not [a for a in element.attrs if a.startswith("on")]
            href = (element.attrs.get("href") or "").strip().lower()
            assert not href.startswith(("javascript:", "data:", "vbscript:")), href
        assert "<script>alert('name')</script>" in page.text()
    page = page_of(out)
    page_html = (out / "templates/alpha/index.html").read_text(encoding="utf-8")
    assert "<img src=x onerror=alert('desc')>" in page.text()
    tree = page.by_class("tree")[0].find("pre")[0].text()
    assert "<script>alert('tree')<script>" in tree
    assert "<img src=x onerror=alert(1)>" in tree
    assert tree.splitlines()[-1] == "└── a b"  # a newline in a name cannot fake a line
    assert "<b>tag</b>" in [
        s.words() for s in meta(page.by_class("card")[0])["Tags"].children
    ]
    assert page.find("title")[0].text().startswith("<script>")
    # a discussion url with a quote and a tag in it is escaped inside its attribute
    assert '"><script>' not in page_html
    assert "<i>x</i>" not in page.text()
    index = parse(out / "index.html")
    assert json.loads(index.cards()["alpha"].attrs["data-tags"]) == [
        "research",
        "<b>tag</b>",
    ]
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "<script>alert" not in html


def test_invalid_ids_are_skipped(repo, tmp_path, capsys):
    (repo / "templates" / "Bad_Id.yml").write_text("name: Whatever\n", encoding="utf-8")
    out = build_site(repo, tmp_path)

    assert sorted(p.name for p in (out / "templates").iterdir()) == ["alpha", "beta"]
    assert "Bad_Id" in capsys.readouterr().err


# ---------------------------------------------------------------- the style

TOKENS = {
    # name: (light, dark), from main.md § Design tokens
    "bg": ("#F7F6F2", "#151412"),
    "card": ("#FFFFFF", "#1F1E1B"),
    "border": ("#E7E4DC", "#34322D"),
    "text": ("#1C1B19", "#EDEBE4"),
    "muted": ("#6B6860", "#A8A49A"),
    "accent": ("#3B4FD8", "#8FA2FF"),
    "accent-text": ("#FFFFFF", "#151412"),
    "warn": ("#B45309", "#F0A04B"),
    "star-fill": ("#F5C542", "#F5C542"),
    "star-stroke": ("#B7791F", "#E0A030"),
    "star-bg": ("#FFFBEB", "#2B2414"),
    "star-border": ("#F3D98B", "#6B5417"),
    "term-bg": ("#1C1B19", "#0E0D0C"),
    "term-text": ("#EDEBE4", "#EDEBE4"),
    "term-prompt": ("#8FA2FF", "#8FA2FF"),
}
PALETTE = ["#0F766E", "#7C3AED", "#B45309", "#2F7D4F", "#3B4FD8", "#BE185D"]


def css_text() -> str:
    return (REPO_ROOT / "site" / "assets" / "site.css").read_text(encoding="utf-8")


def css_tokens() -> tuple[dict[str, str], dict[str, str]]:
    """(light, dark) custom properties parsed from site.css."""
    css = css_text()
    dark_start = css.index("@media (prefers-color-scheme: dark)")
    light = re.search(r":root\s*\{(.*?)\}", css[:dark_start], re.DOTALL).group(1)
    dark = re.search(r":root\s*\{(.*?)\}", css[dark_start:], re.DOTALL).group(1)

    def values(block: str) -> dict[str, str]:
        found = re.findall(r"--([a-z-]+):\s*(#[0-9A-Fa-f]{6})\s*;", block)
        return {name: value.upper() for name, value in found}

    return values(light), values(dark)


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_tokens_match_the_spec_in_both_themes():
    light, dark = css_tokens()

    for name, (expected_light, expected_dark) in TOKENS.items():
        assert light[name] == expected_light, name
        assert dark[name] == expected_dark, name
    assert set(light) - set(TOKENS) == set(dark) - set(TOKENS) == set()


def test_every_color_is_a_token_or_a_palette_color():
    css = css_text()
    allowed = {c for pair in TOKENS.values() for c in pair} | set(PALETTE)

    used = {c.upper() for c in re.findall(r"#[0-9A-Fa-f]{6}\b", css)}
    assert used <= allowed
    assert not re.search(r"rgba?\(|hsla?\(", css)
    for index, color in enumerate(PALETTE):
        assert re.search(rf"\.c{index}\s*\{{\s*background:\s*{color}", css, re.I)
    assert "@media (prefers-color-scheme: dark)" in css


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_token_pairs_reach_wcag_aa(theme):
    tokens = css_tokens()[0 if theme == "light" else 1]
    pairs = [
        (fg, bg)
        for fg in ("text", "muted", "accent", "warn")
        for bg in ("bg", "card")
    ]
    pairs += [("term-text", "term-bg"), ("term-prompt", "term-bg")]
    pairs += [("accent-text", "accent")]

    for fg, bg in pairs:
        ratio = contrast(tokens[fg], tokens[bg])
        assert ratio >= 4.5, (theme, fg, bg, round(ratio, 2))


def test_the_page_is_light_by_default_and_has_a_visible_focus_ring():
    css = css_text()

    assert css.index(":root") < css.index("@media (prefers-color-scheme: dark)")
    assert ":focus-visible" in css
    assert not re.search(r"outline:\s*(none|0)", css)
    for rule in ("min-height: 40px", "width: 40px", "height: 40px"):
        assert rule in css


# ------------------------------------------------------------ the script


def test_script_has_only_the_filter_and_the_copy_button():
    js = (REPO_ROOT / "site" / "assets" / "site.js").read_text(encoding="utf-8")

    assert "navigator.clipboard" in js and "aria-pressed" in js
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "localStorage",
        "sessionStorage",
        "history.",
        "location",
        "fetch(",
        "XMLHttpRequest",
        "sort(",
    ):
        assert forbidden not in js, forbidden


def test_no_taxonomy_name_is_hard_coded():
    taxonomy = load_taxonomy(REPO_ROOT / "taxonomy.yml")
    names = [
        *taxonomy.kinds,
        *taxonomy.tags,
        *(group.name for group in taxonomy.groups),
    ]
    files = [REPO_ROOT / "src/gallery/build.py", *(REPO_ROOT / "site").rglob("*")]
    for path in files:
        if not path.is_file() or path.suffix in (".woff2", ".txt"):
            continue
        text = path.read_text(encoding="utf-8")
        for name in names:
            assert not re.search(rf"""["']{re.escape(name)}["']""", text), (path, name)


# ------------------------------------------------------------ the real data


def test_real_registry_builds_with_a_detected_file(tmp_path):
    out = tmp_path / "site"
    build(REPO_ROOT, out)

    assert (out / "index.html").is_file()
    assert (out / "templates" / "research-thesis" / "index.html").is_file()
    assert (out / "assets" / "fonts" / "OFL.txt").is_file()
