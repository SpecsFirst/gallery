"""Tests for the submission check (``python -m gallery.submit check``)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from markdown_it import MarkdownIt

from gallery import submit
from gallery.registry import Entry
from gallery.submit import (
    MARKER,
    check_body,
    code_span,
    count_tree,
    fenced_block,
    load_form_labels,
    main,
    parse_body,
    propose_link,
    render_comment,
    render_entry,
    slugify,
)
from tests.helpers import VALID_ENTRY, write_entry, write_registry

REPO = Path(__file__).resolve().parents[1]
FORM = REPO / ".github" / "ISSUE_TEMPLATE" / "submit-template.yml"

DETECTED_OK: dict[str, Any] = {
    "status": "ok",
    "error": None,
    "license": "MIT",
    "copier": True,
    "tools": [],
    "last_update": "2026-10-01T09:30:00Z",
    "commit": "a" * 40,
    "tree": ["README.md", ".github/", ".github/ci.yml", "docs/", "docs/a.md"],
    "tree_truncated": False,
    "preview": None,
}


def detect_ok(entry: Entry) -> dict[str, Any]:
    return dict(DETECTED_OK)


def detect_failed(code: str):
    def detect(entry: Entry) -> dict[str, Any]:
        return {"status": "failed", "error": code}

    return detect


def never_detect(entry: Entry) -> dict[str, Any]:
    raise AssertionError("detect must not run when the entry has problems")


def body(
    url: str = "https://github.com/example/new-template",
    name: str = "Teaching lab notebook",
    description: str = "A specs repo for a lab course, shared by students and agents.",
    placement: str = "In their own repo",
    tags: str = "research, thesis",
    command: str = "",
    agnostic: bool = True,
) -> str:
    """A body in the format GitHub gives for the form."""
    agnostic_line = (
        "- [x] This template is model- and provider-agnostic"
        if agnostic
        else "- [ ] This template is model- and provider-agnostic"
    )
    command_value = command or "_No response_"
    return (
        f"### Repository URL\n\n{url}\n\n### Name\n\n{name}\n\n"
        f"### One-line description\n\n{description}\n\n"
        f"### Where do the specs live?\n\n{placement}\n\n"
        f"### Tags\n\n{tags}\n\n"
        f"### Install command\n\n{command_value}\n\n"
        f"### Model and provider agnosticism\n\n{agnostic_line}"
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_registry(tmp_path)
    write_entry(tmp_path, "listed", VALID_ENTRY)
    form_dir = tmp_path / ".github" / "ISSUE_TEMPLATE"
    form_dir.mkdir(parents=True)
    shutil.copy(FORM, form_dir / FORM.name)
    return tmp_path


@pytest.fixture
def labels(root: Path) -> dict[str, str]:
    return load_form_labels(root)


def texts(outcome) -> str:
    return "\n".join(p.text for p in outcome.problems)


# --- The form ----------------------------------------------------------------


def test_form_matches_the_contract() -> None:
    form = yaml.safe_load(FORM.read_text(encoding="utf-8"))
    assert form["name"] == "Submit a template"
    assert form["title"] == "Template: "
    assert form["labels"] == ["submission"]
    fields = [item for item in form["body"] if item["type"] != "markdown"]
    assert [f["id"] for f in fields] == [
        "url",
        "name",
        "description",
        "placement",
        "tags",
        "command",
        "agnostic",
    ]
    assert [f["type"] for f in fields] == [
        "input",
        "input",
        "input",
        "dropdown",
        "input",
        "input",
        "checkbox",
    ]
    for field in fields:
        if field["id"] != "command":
            assert field["validations"]["required"] is True
        else:
            assert field["validations"]["required"] is False
    assert "language" not in {f["id"] for f in fields}
    placement = fields[3]
    assert placement["attributes"]["label"] == "Where do the specs live?"
    assert placement["attributes"]["options"] == [
        "In their own repo",
        "In the code repo",
    ]
    checkbox = fields[6]
    assert checkbox["attributes"]["label"] == "Model and provider agnosticism"
    assert checkbox["attributes"]["options"] == [
        {"label": "This template is model- and provider-agnostic"}
    ]
    intro = form["body"][0]
    assert intro["type"] == "markdown"
    assert "👍" in intro["attributes"]["value"]
    assert "taxonomy.yml" in fields[4]["attributes"]["description"]


def test_labels_are_read_from_the_form(labels: dict[str, str]) -> None:
    assert labels == {
        "url": "Repository URL",
        "name": "Name",
        "description": "One-line description",
        "placement": "Where do the specs live?",
        "tags": "Tags",
        "command": "Install command",
        "agnostic": "Model and provider agnosticism",
    }


def test_unreadable_form_is_an_internal_failure(tmp_path: Path) -> None:
    write_registry(tmp_path)
    with pytest.raises(submit.SubmitError):
        load_form_labels(tmp_path)


# --- Parser ------------------------------------------------------------------


def test_parse_real_body(labels: dict[str, str]) -> None:
    assert parse_body(body(), labels) == {
        "url": "https://github.com/example/new-template",
        "name": "Teaching lab notebook",
        "description": "A specs repo for a lab course, shared by students and agents.",
        "placement": "In their own repo",
        "tags": "research, thesis",
        "command": "",
        "agnostic": "- [x] This template is model- and provider-agnostic",
    }


def test_no_response_is_empty(labels: dict[str, str]) -> None:
    values = parse_body(body(tags="_No response_"), labels)
    assert values["tags"] == ""
    values = parse_body(body(command="_No response_"), labels)
    assert values["command"] == ""


def test_missing_and_extra_headings(labels: dict[str, str]) -> None:
    text = "### Name\n\nOnly a name\n\n### Something else\n\nignored\n\n### Tags\n\nx"
    values = parse_body(text, labels)
    assert values == {
        "url": "",
        "name": "Only a name",
        "description": "",
        "placement": "",
        "tags": "x",
        "command": "",
        "agnostic": "",
    }


def test_crlf_body(labels: dict[str, str]) -> None:
    values = parse_body(body().replace("\n", "\r\n"), labels)
    assert values["name"] == "Teaching lab notebook"
    assert values["url"] == "https://github.com/example/new-template"
    assert values["placement"] == "In their own repo"


def test_first_section_of_a_label_wins(labels: dict[str, str]) -> None:
    values = parse_body("### Name\n\nfirst\n\n### Name\n\nsecond", labels)
    assert values["name"] == "first"


def test_heading_text_in_a_value_does_not_leak(labels: dict[str, str]) -> None:
    values = parse_body("### Name\n\nA\n### Unknown\nB\n", labels)
    assert values["name"] == "A"


def test_body_over_the_cap(root: Path) -> None:
    outcome = check_body("x" * (64 * 1024 + 1), root, never_detect)
    assert not outcome.ok
    assert "64 KiB" in texts(outcome)


def test_body_at_the_cap_is_parsed(root: Path) -> None:
    padded = body() + "\n\n### Extra\n\n" + "x" * (64 * 1024 - len(body()) - 20)
    assert len(padded.encode()) <= 64 * 1024
    assert check_body(padded, root, detect_ok).ok


# --- Entry: tags -------------------------------------------------------------


def test_ok_entry_yaml_byte_for_byte(root: Path) -> None:
    outcome = check_body(
        body(tags="Research, thesis, THESIS, , multi-repo"), root, detect_ok
    )
    assert outcome.ok, texts(outcome)
    assert outcome.id == "teaching-lab-notebook"
    assert outcome.yaml_text == (
        "name: Teaching lab notebook\n"
        "url: https://github.com/example/new-template\n"
        "description: A specs repo for a lab course, shared by students and agents.\n"
        "kind: research\n"
        "tags: [thesis, multi-repo]\n"
        "placement: standalone\n"
    )


def test_both_placements_are_mapped(root: Path) -> None:
    own = check_body(body(placement="In their own repo"), root, detect_ok)
    assert own.ok, texts(own)
    assert "placement: standalone\n" in (own.yaml_text or "")
    code = check_body(body(placement="In the code repo"), root, detect_ok)
    assert code.ok, texts(code)
    assert "placement: in-repo\n" in (code.yaml_text or "")


def test_missing_or_unknown_placement_is_refused(root: Path) -> None:
    outcome = check_body(body(placement="_No response_"), root, never_detect)
    assert not outcome.ok
    assert "choose where the specs live" in texts(outcome)
    assert len(outcome.problems) == 1
    outcome = check_body(body(placement="Somewhere else"), root, never_detect)
    assert not outcome.ok
    assert "choose where the specs live" in texts(outcome)


def test_command_is_written_only_when_filled(root: Path) -> None:
    absent = check_body(body(command=""), root, detect_ok)
    assert absent.ok, texts(absent)
    assert "command" not in (absent.yaml_text or "")
    present = check_body(
        body(command="copier copy https://github.com/example/new-template my-project"),
        root,
        detect_ok,
    )
    assert present.ok, texts(present)
    assert (
        "command: copier copy https://github.com/example/new-template my-project\n"
        in (present.yaml_text or "")
    )
    assert (present.yaml_text or "").index("placement:") < (
        present.yaml_text or ""
    ).index("command:")


@pytest.mark.parametrize(
    "command",
    [
        "x" * 301,
        "two\nlines",
        "bad\x00command",
        "bidi\u202eoverride",
    ],
)
def test_invalid_command_is_refused(root: Path, command: str) -> None:
    outcome = check_body(body(command=command), root, never_detect)
    assert not outcome.ok
    assert len(outcome.problems) == 1


def test_unchecked_agnostic_box_is_refused(root: Path) -> None:
    outcome = check_body(body(agnostic=False), root, never_detect)
    assert not outcome.ok
    assert "model- and provider-agnostic" in texts(outcome)
    assert len(outcome.problems) == 1
    outcome = check_body(
        body(agnostic=False, placement="_No response_"), root, never_detect
    )
    assert texts(outcome).count("model- and provider-agnostic") == 1


def test_tags_omitted_when_empty(root: Path) -> None:
    outcome = check_body(body(tags="research"), root, detect_ok)
    assert outcome.ok
    assert outcome.yaml_text is not None
    assert "tags" not in outcome.yaml_text
    assert outcome.yaml_text.endswith("kind: research\nplacement: standalone\n")


def test_kind_can_come_anywhere(root: Path) -> None:
    outcome = check_body(body(tags="thesis, software"), root, detect_ok)
    assert outcome.ok
    assert "kind: software\ntags: [thesis]\n" in (outcome.yaml_text or "")


def test_no_kind(root: Path) -> None:
    outcome = check_body(body(tags="thesis"), root, never_detect)
    assert not outcome.ok
    assert "exactly one kind" in texts(outcome)
    assert "`research`" in texts(outcome)
    assert texts(outcome).count("kind") >= 1
    assert len(outcome.problems) == 1  # the validator does not say it again


def test_two_kinds(root: Path) -> None:
    outcome = check_body(body(tags="research, software"), root, never_detect)
    assert "not 2" in texts(outcome)
    assert len(outcome.problems) == 1


def test_unknown_items_list_the_valid_ones(root: Path) -> None:
    outcome = check_body(body(tags="research, nope, thesis"), root, never_detect)
    text = texts(outcome)
    assert "`nope`" in text
    assert "Valid kinds: `research`, `software`" in text
    assert "Valid tags: `thesis`, `multi-repo`" in text
    assert "`phd`" not in text  # deprecated tags are not offered
    assert len(outcome.problems) == 1


def test_deprecated_tag_is_replaced(root: Path) -> None:
    outcome = check_body(body(tags="research, phd"), root, detect_ok)
    assert outcome.ok
    assert "tags: [thesis]" in (outcome.yaml_text or "")


def test_deprecated_and_replacement_are_deduplicated(root: Path) -> None:
    outcome = check_body(body(tags="research, phd, thesis"), root, detect_ok)
    assert "tags: [thesis]" in (outcome.yaml_text or "")


def test_more_than_four_tags(root: Path) -> None:
    extra = "".join(f"  extra{i}: An extra tag.\n" for i in range(3))
    text = (root / "taxonomy.yml").read_text().replace("\ngroups:", extra + "\ngroups:")
    (root / "taxonomy.yml").write_text(text, encoding="utf-8")
    four = check_body(
        body(tags="research, thesis, multi-repo, extra0, extra1"), root, detect_ok
    )
    assert four.ok, texts(four)
    five = check_body(
        body(tags="research, thesis, multi-repo, extra0, extra1, extra2"),
        root,
        never_detect,
    )
    assert not five.ok
    assert "at most 4 tags" in texts(five)


# --- Entry: id, names, urls ---------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Teaching lab notebook", "teaching-lab-notebook"),
        ("Café Résumé Ünïcode", "cafe-resume-unicode"),
        ("  A -- B!! C  ", "a-b-c"),
        ("v2.0: the_best", "v2-0-the-best"),
        ("!!!", ""),
        ("日本語", ""),
        ("x" * 60, "x" * 50),
        ("word " * 11, "word-word-word-word-word-word-word-word-word-word"),
        ("a" * 49 + "-" + "b" * 10, "a" * 49),
        ("a" * 50 + " bbb", "a" * 50),
    ],
)
def test_slugify(name: str, expected: str) -> None:
    assert slugify(name) == expected
    assert len(slugify(name)) <= 50


def test_name_with_only_symbols_gives_no_id(root: Path) -> None:
    outcome = check_body(body(name="!!!"), root, never_detect)
    assert not outcome.ok
    assert "gives no id" in texts(outcome)
    assert len(outcome.problems) == 1


def test_sixty_character_name_is_cut_at_a_hyphen(root: Path) -> None:
    name = "alpha " * 9 + "omega"  # 59 characters
    outcome = check_body(body(name=name), root, detect_ok)
    assert outcome.ok, texts(outcome)
    assert outcome.id is not None
    assert len(outcome.id) <= 50
    assert not outcome.id.endswith("-")
    assert outcome.id == "alpha-alpha-alpha-alpha-alpha-alpha-alpha-alpha"


def test_duplicate_id(root: Path) -> None:
    write_entry(root, "teaching-lab-notebook", VALID_ENTRY)
    outcome = check_body(body(), root, never_detect)
    assert "already has" in texts(outcome)
    assert "`teaching-lab-notebook`" in texts(outcome)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/repo",
        "https://github.com/example/repo/",
        "https://github.com/example/repo.git",
        "https://GitHub.com/example/repo.git/",
    ],
)
def test_duplicate_url_variants(root: Path, url: str) -> None:
    outcome = check_body(body(url=url), root, never_detect)
    assert not outcome.ok
    assert "already listed as `listed`" in texts(outcome)


def test_other_repo_of_same_owner_is_not_a_duplicate(root: Path) -> None:
    assert check_body(body(url="https://github.com/example/repo2"), root, detect_ok).ok


def test_trailing_slash_is_removed_from_the_url(root: Path) -> None:
    outcome = check_body(body(url="https://github.com/example/new/"), root, detect_ok)
    assert "url: https://github.com/example/new\n" in (outcome.yaml_text or "")


def test_description_and_name_whitespace_is_collapsed(root: Path) -> None:
    outcome = check_body(
        body(
            name="  Teaching\tlab \n notebook ",
            description="A  specs repo   for a lab course,\n shared by everyone.",
        ),
        root,
        detect_ok,
    )
    assert outcome.ok, texts(outcome)
    assert "name: Teaching lab notebook\n" in (outcome.yaml_text or "")
    assert "description: A specs repo for a lab course, shared by everyone.\n" in (
        outcome.yaml_text or ""
    )


def test_all_schema_errors_are_reported_at_once(root: Path) -> None:
    outcome = check_body(
        body(
            url="http://example.com/x", name="ab", description="short", tags="research"
        ),
        root,
        never_detect,
    )
    text = texts(outcome)
    assert (
        "**Repository URL**: must be a public address starting with `https://`" in text
    )
    assert "**Name**: must have 3 to 60 characters." in text
    assert "**One-line description**: must have 20 to 200 characters." in text
    assert len(outcome.problems) == 3


def test_empty_fields_are_each_reported(root: Path) -> None:
    outcome = check_body("", root, never_detect)
    fields = [p.field for p in outcome.problems]
    assert fields.count("url") == 1
    assert fields.count("name") == 1
    assert fields.count("description") == 1
    assert fields.count("tags") == 1
    assert fields.count("placement") == 1
    assert fields.count("agnostic") == 1
    assert all(
        "empty" in p.text
        or "kind" in p.text
        or "specs live" in p.text
        or "model- and provider-agnostic" in p.text
        for p in outcome.problems
    )


def test_url_with_credentials(root: Path) -> None:
    outcome = check_body(body(url="https://user:pw@github.com/e/r"), root, never_detect)
    assert "user name or a password" in texts(outcome)
    assert "pw" not in texts(outcome)


def test_control_characters_are_refused(root: Path) -> None:
    outcome = check_body(body(name="Bad\x00name here"), root, never_detect)
    assert "control characters" in texts(outcome)
    outcome = check_body(
        body(description="A‮bidi description of a template"), root, never_detect
    )
    assert "control characters" in texts(outcome)


def test_detect_failure_says_why(root: Path) -> None:
    outcome = check_body(body(), root, detect_failed("unreachable"))
    assert not outcome.ok
    assert "could not be fetched" in texts(outcome)
    assert outcome.id is None and outcome.yaml_text is None
    outcome = check_body(body(), root, detect_failed("too_large"))
    assert "too large" in texts(outcome)
    outcome = check_body(body(), root, detect_failed("timeout"))
    assert "too long" in texts(outcome)


def test_detect_receives_the_built_entry(root: Path) -> None:
    seen: list[Entry] = []

    def detect(entry: Entry) -> dict[str, Any]:
        seen.append(entry)
        return dict(DETECTED_OK)

    assert check_body(body(), root, detect).ok
    assert seen[0].id == "teaching-lab-notebook"
    assert seen[0].fields["url"] == "https://github.com/example/new-template"
    assert seen[0].language == "en"


# --- YAML --------------------------------------------------------------------


def test_render_matches_existing_entries() -> None:
    for path in sorted((REPO / "templates").glob("*.yml")):
        fields = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert render_entry(fields) == path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize(
    "value",
    ["a: b", "# hash", "- dash", "yes", "123", "it's", 'say "hi"', "x: [y]", "é — ü"],
)
def test_render_round_trips_awkward_values(value: str) -> None:
    fields = {
        "name": value,
        "url": "https://e.com/x",
        "description": value,
        "kind": "k",
    }
    assert yaml.safe_load(render_entry(fields)) == fields


# --- Comment -----------------------------------------------------------------


def ok_outcome(root: Path, **kwargs: str):
    outcome = check_body(body(**kwargs), root, detect_ok)
    assert outcome.ok, texts(outcome)
    return outcome


def test_ok_comment(root: Path) -> None:
    comment = render_comment(12, ok_outcome(root))
    assert comment.startswith(MARKER + "\n")
    assert "```yaml\nname: Teaching lab notebook\n" in comment
    assert "- License: `MIT`" in comment
    assert "- Last update: `2026-10-01T09:30:00Z`" in comment
    assert "- Copier support: yes" in comment
    assert "`1 directory, 2 files`" in comment
    assert (
        "1. [Open the propose link](https://github.com/SpecsFirst/gallery/new/main?"
        in comment
    )
    assert "**Commit changes…**" in comment and "**Propose changes**" in comment
    assert "`Closes #12`" in comment


def test_failed_comment(root: Path) -> None:
    comment = render_comment(3, check_body(body(name="ab"), root, never_detect))
    assert comment.startswith(MARKER + "\n")
    assert "- **Name**: must have 3 to 60 characters." in comment
    assert "Edit this issue to fix them: the check runs again." in comment
    assert "new/main" not in comment


def detect_tools(tools):
    def detect(entry: Entry) -> dict[str, Any]:
        result = dict(DETECTED_OK)
        result["tools"] = tools
        return result

    return detect


def test_clean_repository_gives_the_propose_link(root: Path) -> None:
    outcome = check_body(body(), root, detect_ok)
    assert outcome.ok, texts(outcome)
    comment = render_comment(9, outcome)
    assert "new/main" in comment
    assert "tool-specific" not in comment


def test_tool_specific_files_list_them_and_give_no_link(root: Path) -> None:
    tools = [".claude/", ".github/prompts/", "CLAUDE.md"]
    outcome = check_body(body(), root, detect_tools(tools))
    assert not outcome.ok
    comment = render_comment(4, outcome)
    assert "model- and provider-agnostic" in comment
    assert "tool-specific files" in comment
    assert "`CLAUDE.md`" in comment
    assert "`.claude/`" in comment
    assert "`.github/prompts/`" in comment
    assert "new/main" not in comment
    assert "propose link" not in comment.lower()


HOSTILE = [
    "@octocat",
    "<img src=x onerror=alert(1)>",
    "a`b``c```d",
    "](javascript:alert(1))",
    "::error::boom",
    "![x](https://evil.example/p.png)",
    "<script>x</script>",
    "[click](https://evil.example)",
]


PROPOSE_PREFIX = "https://github.com/SpecsFirst/gallery/new/main?"


def render_tokens(comment: str) -> list:
    """Every token of the rendered comment, inline children included."""
    found = []
    for token in MarkdownIt("commonmark").parse(comment):
        found.append(token)
        found.extend(token.children or [])
    return found


def assert_inert(comment: str) -> None:
    """No raw HTML, image, mention or foreign link outside code."""
    for token in render_tokens(comment):
        if token.type == "html_block" and token.content.strip() == MARKER:
            continue  # the one HTML we write ourselves, and only first
        assert token.type not in ("html_inline", "html_block", "image")
        if token.type == "link_open":
            assert token.attrGet("href").startswith(PROPOSE_PREFIX)
        if token.type == "text":
            assert "@" not in token.content
            assert not token.content.lstrip().startswith("::")
    assert not any(line.startswith("::") for line in comment.splitlines())


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_values_stay_inert_in_problems(root: Path, hostile: str) -> None:
    comment = render_comment(
        1, check_body(body(tags=f"research, {hostile}"), root, never_detect)
    )
    assert comment.startswith(MARKER)
    assert_inert(comment)
    codes = [t.content for t in render_tokens(comment) if t.type == "code_inline"]
    assert hostile.lower() in codes  # shown, as the whole content of a code span


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_values_stay_inert_in_ok_comment(root: Path, hostile: str) -> None:
    outcome = check_body(
        body(
            name=f"Name {hostile}"[:60],
            description=f"A description with {hostile} inside it, long enough.",
        ),
        root,
        detect_ok,
    )
    assert outcome.ok, texts(outcome)
    comment = render_comment(1, outcome)
    assert_inert(comment)
    assert comment.count(MARKER) == 1
    fences = [t for t in render_tokens(comment) if t.type == "fence"]
    assert len(fences) == 1 and fences[0].info == "yaml"
    assert yaml.safe_load(fences[0].content)["description"].count(hostile) == 1


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_values_stay_inert_when_the_url_is_hostile(
    root: Path, hostile: str
) -> None:
    comment = render_comment(1, check_body(body(url=hostile), root, never_detect))
    assert_inert(comment)
    assert hostile not in comment  # the url problem is a fixed sentence


@pytest.mark.parametrize("text", HOSTILE)
def test_code_span_holds_exactly_the_text(text: str) -> None:
    tokens = render_tokens(f"- a {code_span(text)} b")
    codes = [t.content for t in tokens if t.type == "code_inline"]
    assert codes == [text]
    assert not [t for t in tokens if t.type.startswith("html") or t.type == "image"]


@pytest.mark.parametrize("text", ["`", " x ", "`x`", "  ", "x ", "a``b"])
def test_code_span_edge_cases(text: str) -> None:
    codes = [
        t.content for t in render_tokens(code_span(text, 80)) if t.type == "code_inline"
    ]
    assert codes == [text if text.strip() else "(empty)"]


def test_code_span_flattens_newlines_and_truncates() -> None:
    span = code_span("a\nb\r\n" + "x" * 500)
    assert "\n" not in span and "\r" not in span
    assert len(span) < 100


def test_fenced_block_is_longer_than_inner_backticks() -> None:
    block = fenced_block("name: a ```` b\n", "yaml")
    assert block.startswith("`````yaml\n")
    assert block.endswith("\n`````")


def test_hostile_yaml_stays_inside_the_fence(root: Path) -> None:
    outcome = check_body(
        body(description="A description with ``` ``` and @user and <b>x</b> in it."),
        root,
        detect_ok,
    )
    assert outcome.ok, texts(outcome)
    comment = render_comment(5, outcome)
    assert "````yaml\n" in comment
    assert comment.count("````") == 2


def test_comment_size_cap(root: Path) -> None:
    taxonomy = (root / "taxonomy.yml").read_text()
    many = "".join(f"  tag{i}: Some tag.\n" for i in range(5000))
    (root / "taxonomy.yml").write_text(
        taxonomy.replace("\ngroups:", many + "\ngroups:"), encoding="utf-8"
    )
    outcome = check_body(body(tags="research, nope"), root, never_detect)
    comment = render_comment(1, outcome)
    assert len(comment) <= 60_000
    assert comment.startswith(MARKER)


def test_propose_link_decodes_back(root: Path) -> None:
    outcome = ok_outcome(root)
    link = propose_link(outcome.id or "", outcome.yaml_text or "")
    parts = urlsplit(link)
    assert parts.netloc == "github.com"
    assert parts.path == "/SpecsFirst/gallery/new/main"
    query = parse_qs(parts.query, keep_blank_values=True)
    assert query["filename"] == ["templates/teaching-lab-notebook.yml"]
    assert query["value"] == [outcome.yaml_text]
    assert "\n" not in link and " " not in link


def test_propose_link_uses_the_repository_of_the_run(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/fork")
    assert propose_link("a-b", "x: y\n").startswith(
        "https://github.com/me/fork/new/main?"
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "bad repo/../x")
    assert propose_link("a-b", "x: y\n").startswith(
        "https://github.com/SpecsFirst/gallery/"
    )


def test_propose_link_refuses_a_bad_id() -> None:
    with pytest.raises(submit.SubmitError):
        propose_link("../x", "a: b\n")


def test_count_tree_hides_dot_entries() -> None:
    assert count_tree(["a/", "a/b.md", ".git/", ".git/x", ".env", "c"], False) == (
        "1 directory, 2 files"
    )
    assert count_tree([], True) == "0 directories, 0 files (listing truncated)"
    assert count_tree(["a/", "b/"], False) == "2 directories, 0 files"


# --- Command line --------------------------------------------------------------


def event(
    tmp: Path, issue_body: str | None, number: int = 7, state: str = "open"
) -> Path:
    path = tmp / "event.json"
    path.write_text(
        json.dumps({"issue": {"number": number, "body": issue_body, "state": state}}),
        encoding="utf-8",
    )
    return path


def test_cli_writes_submission_json(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(submit, "detect_entry", lambda entry, options: detect_ok(entry))
    out = tmp_path / "out"
    code = main(
        [
            "check",
            "--event",
            str(event(tmp_path, body())),
            "--root",
            str(root),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads((out / "submission.json").read_text(encoding="utf-8"))
    assert set(data) == {"issue", "ok", "id", "comment"}
    assert data["issue"] == 7 and data["ok"] is True
    assert data["id"] == "teaching-lab-notebook"
    assert data["comment"].startswith(MARKER)


def test_cli_not_ok_exits_zero_with_null_id(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        submit, "detect_entry", lambda entry, options: never_detect(entry)
    )
    out = tmp_path / "out"
    code = main(
        [
            "check",
            "--event",
            str(event(tmp_path, body(tags="x"))),
            "--root",
            str(root),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads((out / "submission.json").read_text(encoding="utf-8"))
    assert data["ok"] is False and data["id"] is None
    assert "Edit this issue" in data["comment"]


def test_cli_closed_issue_does_nothing(
    root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    code = main(
        [
            "check",
            "--event",
            str(event(tmp_path, body(), state="closed")),
            "--root",
            str(root),
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert not out.exists()
    assert capsys.readouterr().out == ""


def test_cli_null_body_is_a_submission_with_problems(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        submit, "detect_entry", lambda entry, options: never_detect(entry)
    )
    out = tmp_path / "out"
    assert (
        main(
            [
                "check",
                "--event",
                str(event(tmp_path, None)),
                "--root",
                str(root),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert json.loads((out / "submission.json").read_text())["ok"] is False


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        "{}",
        '{"issue": {"number": "7", "body": "x"}}',
        '{"issue": {"number": 0}}',
        '{"issue": {"number": true}}',
        '{"issue": {"number": 1, "body": 5}}',
    ],
)
def test_cli_bad_event_is_an_internal_failure(
    root: Path, tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "event.json"
    path.write_text(content, encoding="utf-8")
    out = tmp_path / "out"
    assert (
        main(["check", "--event", str(path), "--root", str(root), "--out", str(out)])
        == 1
    )
    assert not out.exists()


def test_cli_missing_event_and_missing_form(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert (
        main(
            [
                "check",
                "--event",
                str(tmp_path / "none.json"),
                "--root",
                str(root),
                "--out",
                str(out),
            ]
        )
        == 1
    )
    (root / ".github" / "ISSUE_TEMPLATE" / FORM.name).unlink()
    assert (
        main(
            [
                "check",
                "--event",
                str(event(tmp_path, body())),
                "--root",
                str(root),
                "--out",
                str(out),
            ]
        )
        == 1
    )


def test_cli_log_has_no_issue_content(
    root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        submit, "detect_entry", lambda entry, options: never_detect(entry)
    )
    hostile = "::error::pwned"
    main(
        [
            "check",
            "--event",
            str(event(tmp_path, body(name=hostile, tags=hostile))),
            "--root",
            str(root),
            "--out",
            str(tmp_path / "o"),
        ]
    )
    captured = capsys.readouterr()
    assert "pwned" not in captured.out + captured.err
    assert not any(
        line.startswith("::") for line in (captured.out + captured.err).splitlines()
    )
