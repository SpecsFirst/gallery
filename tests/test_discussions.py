"""Tests for the discussions command. Offline: an injected transport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gallery import discussions
from gallery.discussions import discussion_body, gallery_url, main
from gallery.registry import load_registry
from tests.helpers import VALID_ENTRY, write_entry, write_registry
from tests.test_votes import FakeTransport, node, page

TOKEN = "ghs_SECRET_TOKEN_VALUE_123"  # noqa: S105 - a fake token for the tests

SITE_YML = """\
title: Test
base_url: https://example.org/gallery/
discussions:
  repo: Owner/the-repo
  repo_id: R_abc
  category: Templates
  category_id: DIC_cat
"""

EXPECTED_MUTATION = (
    "mutation($repo:ID!,$cat:ID!,$title:String!,$body:String!){createDiscussion("
    "input:{repositoryId:$repo,categoryId:$cat,title:$title,body:$body})"
    "{discussion{url}}}"
)


def created(url: str) -> dict[str, Any]:
    return {"data": {"createDiscussion": {"discussion": {"url": url}}}}


def url_of(number: int) -> str:
    return f"https://github.com/Owner/the-repo/discussions/{number}"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_registry(tmp_path)
    # Written in an order that differs from the id order.
    write_entry(tmp_path, "beta", {**VALID_ENTRY, "name": "Beta template"})
    write_entry(tmp_path, "alpha", {**VALID_ENTRY, "name": "Alpha template"})
    (tmp_path / "site.yml").write_text(SITE_YML, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


def mutations(transport: FakeTransport) -> list[dict[str, Any]]:
    return [r for r, _ in transport.requests if "mutation" in r["query"]]


# --- Creation ----------------------------------------------------------------


def test_nothing_missing_sends_no_mutation(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(page([node("alpha"), node("beta")]))
    assert main(["--root", str(root)], transport) == 0
    assert len(transport.requests) == 1
    assert mutations(transport) == []
    assert "created" not in capfd.readouterr().out


def test_two_missing_are_created_in_id_order(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(page([]), created(url_of(1)), created(url_of(2)))
    assert main(["--root", str(root)], transport) == 0
    first, second = mutations(transport)
    assert first["query"] == second["query"] == EXPECTED_MUTATION
    base = "https://owner.github.io/the-repo/"
    entries = {e.id: e for e in load_registry(root).entries}
    for sent, entry_id in ((first, "alpha"), (second, "beta")):
        assert sent["variables"] == {
            "repo": "R_abc",
            "cat": "DIC_cat",
            "title": entry_id,
            "body": discussion_body(entries[entry_id], base),
        }
    assert capfd.readouterr().out == (
        f"created alpha {url_of(1)}\ncreated beta {url_of(2)}\n"
    )
    assert all(token == TOKEN for _, token in transport.requests)


def test_body_text() -> None:
    entry = load_entry(
        name="Thesis kit",
        description="By @someone and @other-team.\nSecond line.",
        url="https://github.com/example/repo",
    )
    assert discussion_body(entry, "https://specsfirst.github.io/gallery/") == (
        "Star **Thesis kit** by reacting with 👍 to this post. One GitHub account, "
        "one star; the gallery counts them every few hours.\n"
        "\n"
        "> By @&#8203;someone and @&#8203;other-team.\n"
        "> Second line.\n"
        "\n"
        "- Gallery page: https://specsfirst.github.io/gallery/templates/thesis-kit/\n"
        "- Source: https://github.com/example/repo"
    )


def load_entry(**fields: str) -> Any:
    from gallery.registry import Entry

    return Entry(id="thesis-kit", fields={**VALID_ENTRY, **fields}, language="en")


def test_gallery_url_is_derived_from_the_repo() -> None:
    assert (
        gallery_url("SpecsFirst", "gallery") == "https://specsfirst.github.io/gallery/"
    )


def test_title_in_another_case_does_not_count(root: Path) -> None:
    transport = FakeTransport(
        page([node("Alpha"), node("BETA")]), created(url_of(1)), created(url_of(2))
    )
    assert main(["--root", str(root)], transport) == 0
    assert [m["variables"]["title"] for m in mutations(transport)] == ["alpha", "beta"]


def test_a_present_id_is_skipped(root: Path) -> None:
    transport = FakeTransport(page([node("alpha")]), created(url_of(2)))
    assert main(["--root", str(root)], transport) == 0
    assert [m["variables"]["title"] for m in mutations(transport)] == ["beta"]


def test_several_pages_are_read_before_creating(root: Path) -> None:
    transport = FakeTransport(
        page([node("alpha")], next_cursor="c1"), page([node("beta")])
    )
    assert main(["--root", str(root)], transport) == 0
    assert mutations(transport) == []
    assert transport.requests[1][0]["variables"]["after"] == "c1"


def test_max_create_is_respected(
    root: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    assert discussions.MAX_CREATE == 10
    monkeypatch.setattr(discussions, "MAX_CREATE", 1)
    transport = FakeTransport(page([]), created(url_of(1)))
    assert main(["--root", str(root)], transport) == 0
    assert [m["variables"]["title"] for m in mutations(transport)] == ["alpha"]
    captured = capfd.readouterr()
    assert captured.out == f"created alpha {url_of(1)}\n"
    assert "1 left for the next run" in captured.err


def test_default_max_create_with_many_templates(tmp_path: Path) -> None:
    write_registry(tmp_path)
    for i in range(12):
        write_entry(tmp_path, f"t{i:02d}", VALID_ENTRY)
    (tmp_path / "site.yml").write_text(SITE_YML, encoding="utf-8")
    transport = FakeTransport(page([]), *[created(url_of(i)) for i in range(10)])
    assert main(["--root", str(tmp_path)], transport) == 0
    titles = [m["variables"]["title"] for m in mutations(transport)]
    assert titles == [f"t{i:02d}" for i in range(10)]


# --- Dry run -----------------------------------------------------------------


def test_dry_run_sends_no_mutation(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(page([]))
    assert main(["--root", str(root), "--dry-run"], transport) == 0
    assert len(transport.requests) == 1
    assert mutations(transport) == []
    assert capfd.readouterr().out == "would create alpha\nwould create beta\n"


def test_dry_run_with_nothing_missing(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(page([node("alpha"), node("beta")]))
    assert main(["--root", str(root), "--dry-run"], transport) == 0
    assert capfd.readouterr().out == ""


# --- Failures ----------------------------------------------------------------

LISTING_FAILURES: dict[str, Any] = {
    "network error": OSError("boom"),
    "status 500": (500, b"{}"),
    "invalid json": (200, b"not json"),
    "errors": {**page([]), "errors": [{"message": "nope"}]},
    "null repository": {"data": {"repository": None}},
    "url on another host": page([node("zzz", url="https://evil.example/x")]),
}


@pytest.mark.parametrize("name", list(LISTING_FAILURES))
def test_listing_failure_exits_1_and_creates_nothing(root: Path, name: str) -> None:
    transport = FakeTransport(LISTING_FAILURES[name])
    assert main(["--root", str(root)], transport) == 1
    assert len(transport.requests) == 1


def test_listing_failure_on_a_later_page_creates_nothing(root: Path) -> None:
    transport = FakeTransport(page([], next_cursor="c1"), (500, b"{}"))
    assert main(["--root", str(root)], transport) == 1
    assert mutations(transport) == []


CREATION_FAILURES: dict[str, Any] = {
    "network error": OSError("boom"),
    "status 403": (403, b'{"message":"Resource not accessible"}'),
    "status 500": (500, b"{}"),
    "invalid json": (200, b"not json"),
    "json not an object": (200, b"[]"),
    "errors": {**created(url_of(1)), "errors": [{"message": "nope"}]},
    "data null with errors": {"data": None, "errors": [{"message": "nope"}]},
    "no data": {},
    "no createDiscussion": {"data": {"createDiscussion": None}},
    "no discussion": {"data": {"createDiscussion": {"discussion": None}}},
    "url missing": {"data": {"createDiscussion": {"discussion": {}}}},
    "url not a string": created(5),  # type: ignore[arg-type]
    "url on another host": created("https://evil.example/x"),
    "url over http": created("http://github.com/o/r/discussions/1"),
}


@pytest.mark.parametrize("name", list(CREATION_FAILURES))
def test_creation_failure_stops_at_once_with_exit_1(
    root: Path, name: str, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(page([]), CREATION_FAILURES[name])
    assert main(["--root", str(root)], transport) == 1
    assert len(mutations(transport)) == 1  # beta is never tried
    assert capfd.readouterr().out == ""


def test_failure_after_a_success_keeps_the_first(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(page([]), created(url_of(1)), (500, b"{}"))
    assert main(["--root", str(root)], transport) == 1
    assert len(mutations(transport)) == 2
    assert capfd.readouterr().out == f"created alpha {url_of(1)}\n"


def test_missing_token(
    root: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport()
    monkeypatch.delenv("GITHUB_TOKEN")
    assert main(["--root", str(root)], transport) == 2
    assert transport.requests == []
    assert "GITHUB_TOKEN" in capfd.readouterr().err


def test_empty_token(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "")
    transport = FakeTransport()
    assert main(["--root", str(root)], transport) == 2
    assert transport.requests == []


def test_no_token_command_line_option(root: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--root", str(root), "--token", "x"], FakeTransport())


def test_site_yml_without_valid_repo(root: Path) -> None:
    (root / "site.yml").write_text(
        SITE_YML.replace("Owner/the-repo", "no-slash"), encoding="utf-8"
    )
    transport = FakeTransport()
    assert main(["--root", str(root)], transport) == 2
    assert transport.requests == []


def test_broken_registry(tmp_path: Path) -> None:
    (tmp_path / "site.yml").write_text(SITE_YML, encoding="utf-8")
    transport = FakeTransport()
    assert main(["--root", str(tmp_path)], transport) == 2
    assert transport.requests == []


# --- Nothing secret or foreign in the output ---------------------------------


def test_token_and_stranger_titles_never_printed(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    transport = FakeTransport(
        page([node("stranger-title-xyz"), node("alpha")]), created(url_of(1))
    )
    assert main(["--root", str(root)], transport) == 0
    captured = capfd.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "stranger-title-xyz" not in captured.out + captured.err


def test_remote_text_is_not_echoed(
    root: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    echo = f"bad token {TOKEN} and a remote message"
    listing = (
        (401, echo.encode()),
        {**page([]), "errors": [{"message": echo}]},
        OSError(echo),
    )
    for response in listing:
        assert main(["--root", str(root)], FakeTransport(response)) == 1
    creation = (
        (403, echo.encode()),
        {"errors": [{"message": echo}]},
        (200, echo.encode()),
        OSError(echo),
        created(echo),  # type: ignore[arg-type]
    )
    for response in creation:
        assert main(["--root", str(root)], FakeTransport(page([]), response)) == 1
    captured = capfd.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "remote message" not in captured.out + captured.err
