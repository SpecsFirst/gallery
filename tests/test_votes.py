"""Tests for the votes command. Offline: an injected transport, or a local server."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from gallery import votes
from gallery.votes import VotesError, fetch_votes, main, post_graphql
from tests.helpers import VALID_ENTRY, write_entry, write_registry

TOKEN = "ghs_SECRET_TOKEN_VALUE_123"  # noqa: S105 - a fake token for the tests
GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

SITE_YML = """\
title: Test
tagline: Test
giscus:
  repo: Owner/the-repo
  repo_id: R_abc
  category: Templates
  category_id: DIC_cat
"""

EXPECTED_QUERY = (
    "query($owner:String!,$name:String!,$cat:ID!,$after:String){repository("
    "owner:$owner,name:$name){discussions(first:100,after:$after,categoryId:$cat,"
    "orderBy:{field:CREATED_AT,direction:ASC}){pageInfo{hasNextPage endCursor} "
    "nodes{title url reactions(content:THUMBS_UP,first:1){totalCount}}}}}"
)


def node(title: str, count: Any = 1, url: str | None = None) -> dict[str, Any]:
    return {
        "title": title,
        "url": url
        or f"https://github.com/Owner/the-repo/discussions/{abs(hash(title)) % 999}",
        "reactions": {"totalCount": count},
    }


def page(nodes: list[Any], next_cursor: str | None = None) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {
                        "hasNextPage": next_cursor is not None,
                        "endCursor": next_cursor,
                    },
                    "nodes": nodes,
                }
            }
        }
    }


class FakeTransport:
    """Returns the given responses in order and records every request."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[dict[str, Any], str]] = []

    def __call__(self, body: bytes, token: str) -> tuple[int, bytes]:
        self.requests.append((json.loads(body), token))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, json.dumps(response).encode()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_registry(tmp_path)
    write_entry(tmp_path, "alpha", VALID_ENTRY)
    write_entry(tmp_path, "beta", VALID_ENTRY)
    (tmp_path / "site.yml").write_text(SITE_YML, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)


def run(
    root: Path, transport: Callable[[bytes, str], tuple[int, bytes]], out: Path
) -> int:
    return main(["--root", str(root), "--output", str(out)], transport)


# --- Counting ----------------------------------------------------------------


def test_query_is_the_one_run_against_the_real_api() -> None:
    assert votes.QUERY == EXPECTED_QUERY


def test_writes_votes_json(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "votes.json"
    out.parent.mkdir()
    transport = FakeTransport(
        page([node("alpha", 3, "https://github.com/Owner/the-repo/discussions/1")])
    )
    assert run(root, transport, out) == 0
    text = out.read_text(encoding="utf-8")
    document = json.loads(text)
    assert GENERATED_AT.match(document["generated_at"])
    assert document["templates"] == {
        "alpha": {
            "votes": 3,
            "discussion_url": "https://github.com/Owner/the-repo/discussions/1",
        }
    }
    assert text == json.dumps(document, indent=2, sort_keys=True) + "\n"


def test_request_uses_site_yml_and_token(root: Path, tmp_path: Path) -> None:
    transport = FakeTransport(page([]))
    assert run(root, transport, tmp_path / "v.json") == 0
    ((body, token),) = transport.requests
    assert token == TOKEN
    assert body["query"] == EXPECTED_QUERY
    assert body["variables"] == {
        "owner": "Owner",
        "name": "the-repo",
        "cat": "DIC_cat",
        "after": None,
    }
    assert TOKEN not in json.dumps(body)


def test_exact_title_match() -> None:
    transport = FakeTransport(page([node("alpha", 2), node("beta", 0)]))
    result = fetch_votes("o", "r", "c", ["alpha", "beta"], TOKEN, transport)
    assert {k: v["votes"] for k, v in result.items()} == {"alpha": 2, "beta": 0}


def test_title_match_is_case_sensitive_and_exact() -> None:
    transport = FakeTransport(
        page([node("Alpha"), node("ALPHA"), node("alpha "), node(" alpha")])
    )
    assert fetch_votes("o", "r", "c", ["alpha"], TOKEN, transport) == {}


def test_unknown_titles_are_ignored_and_never_logged(
    root: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "v.json"
    transport = FakeTransport(page([node("stranger-title-xyz", 9), node("beta", 4)]))
    assert run(root, transport, out) == 0
    assert list(json.loads(out.read_text())["templates"]) == ["beta"]
    captured = capfd.readouterr()
    assert "stranger-title-xyz" not in captured.out + captured.err


def test_oldest_duplicate_counts() -> None:
    transport = FakeTransport(
        page(
            [
                node("alpha", 5, "https://github.com/o/r/discussions/1"),
                node("alpha", 50, "https://github.com/o/r/discussions/2"),
            ]
        )
    )
    result = fetch_votes("o", "r", "c", ["alpha"], TOKEN, transport)
    assert result == {
        "alpha": {"votes": 5, "discussion_url": "https://github.com/o/r/discussions/1"}
    }


def test_oldest_duplicate_counts_across_pages() -> None:
    transport = FakeTransport(
        page([node("alpha", 1)], next_cursor="c1"), page([node("alpha", 70)])
    )
    result = fetch_votes("o", "r", "c", ["alpha"], TOKEN, transport)
    assert result["alpha"]["votes"] == 1


def test_id_without_discussion_is_absent(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "v.json"
    assert run(root, FakeTransport(page([])), out) == 0
    assert json.loads(out.read_text())["templates"] == {}


def test_several_pages_are_read_with_the_cursor() -> None:
    transport = FakeTransport(
        page([node("alpha", 1)], next_cursor="cursor-1"),
        page([node("beta", 2)], next_cursor="cursor-2"),
        page([node("gamma", 3)]),
    )
    result = fetch_votes("o", "r", "c", ["alpha", "beta", "gamma"], TOKEN, transport)
    assert sorted(result) == ["alpha", "beta", "gamma"]
    afters = [body["variables"]["after"] for body, _ in transport.requests]
    assert afters == [None, "cursor-1", "cursor-2"]


def test_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(votes, "MAX_PAGES", 3)
    transport = FakeTransport(*[page([], next_cursor=f"c{i}") for i in range(10)])
    with pytest.raises(VotesError):
        fetch_votes("o", "r", "c", ["alpha"], TOKEN, transport)
    assert len(transport.requests) == 3


def test_last_allowed_page_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(votes, "MAX_PAGES", 2)
    transport = FakeTransport(page([], next_cursor="c1"), page([node("alpha")]))
    assert "alpha" in fetch_votes("o", "r", "c", ["alpha"], TOKEN, transport)


def test_constants() -> None:
    assert votes.REQUEST_TIMEOUT == 30
    assert votes.API_URL == "https://api.github.com/graphql"
    assert votes.MAX_PAGES >= 1


# --- Failures: exit non-zero, no file ----------------------------------------

GOOD = node("alpha", 1)


def mutated(**changes: Any) -> dict[str, Any]:
    """A good page whose first node is replaced by ``changes`` applied to GOOD."""
    document = page([{**GOOD, **changes}])
    return document


def without(key: str) -> dict[str, Any]:
    bad = dict(GOOD)
    del bad[key]
    return page([bad])


FAILURES: dict[str, Any] = {
    "network error": OSError("boom"),
    "timeout": TimeoutError("slow"),
    "status 500": (500, b"{}"),
    "status 401": (401, b'{"message":"Bad credentials"}'),
    "status 204": (204, b""),
    "redirect": (302, b""),
    "invalid json": (200, b"not json"),
    "json not an object": (200, b"[]"),
    "errors": {**page([]), "errors": [{"message": "nope"}]},
    "null repository": {"data": {"repository": None}},
    "no data": {},
    "no discussions": {"data": {"repository": {}}},
    "no pageInfo": {"data": {"repository": {"discussions": {"nodes": []}}}},
    "no nodes": {
        "data": {
            "repository": {
                "discussions": {"pageInfo": {"hasNextPage": False, "endCursor": None}}
            }
        }
    },
    "hasNextPage not bool": {
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {"hasNextPage": 1, "endCursor": None},
                    "nodes": [],
                }
            }
        }
    },
    "next page without cursor": {
        "data": {
            "repository": {
                "discussions": {
                    "pageInfo": {"hasNextPage": True, "endCursor": None},
                    "nodes": [],
                }
            }
        }
    },
    "missing title": without("title"),
    "missing url": without("url"),
    "missing reactions": without("reactions"),
    "missing totalCount": page([{**GOOD, "reactions": {}}]),
    "title not a string": mutated(title=5),
    "count is a string": mutated(reactions={"totalCount": "3"}),
    "count is a float": mutated(reactions={"totalCount": 3.0}),
    "count is bool": mutated(reactions={"totalCount": True}),
    "count is negative": mutated(reactions={"totalCount": -1}),
    "count is null": mutated(reactions={"totalCount": None}),
    "url on another host": mutated(url="https://evil.example/x"),
    "url over http": mutated(url="http://github.com/o/r/discussions/1"),
    "url null": mutated(url=None),
    "node not an object": page(["alpha"]),
}


@pytest.mark.parametrize("name", list(FAILURES))
def test_failure_exits_non_zero_and_writes_nothing(
    name: str,
    root: Path,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "v.json"
    assert run(root, FakeTransport(FAILURES[name]), out) != 0
    assert not out.exists()
    assert not list(tmp_path.glob(".v.json*"))
    captured = capfd.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_failure_on_a_later_page_writes_nothing(root: Path, tmp_path: Path) -> None:
    out = tmp_path / "v.json"
    transport = FakeTransport(page([node("alpha")], next_cursor="c1"), (500, b""))
    assert run(root, transport, out) != 0
    assert not out.exists()


def test_page_cap_exits_non_zero_and_writes_nothing(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(votes, "MAX_PAGES", 2)
    out = tmp_path / "v.json"
    transport = FakeTransport(*[page([], next_cursor=f"c{i}") for i in range(5)])
    assert run(root, transport, out) != 0
    assert not out.exists()


def test_missing_token(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    transport = FakeTransport(page([]))
    out = tmp_path / "v.json"
    assert run(root, transport, out) != 0
    assert not out.exists()
    assert transport.requests == []
    assert "GITHUB_TOKEN" in capfd.readouterr().err


def test_empty_token(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "")
    out = tmp_path / "v.json"
    assert run(root, FakeTransport(page([])), out) != 0
    assert not out.exists()


def test_no_token_command_line_option(root: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            ["--root", str(root), "--output", str(tmp_path / "v.json"), "--token", "x"]
        )


def test_site_yml_without_valid_repo(root: Path, tmp_path: Path) -> None:
    (root / "site.yml").write_text(
        SITE_YML.replace("Owner/the-repo", "no-slash"), encoding="utf-8"
    )
    out = tmp_path / "v.json"
    assert run(root, FakeTransport(page([])), out) != 0
    assert not out.exists()


def test_broken_registry(tmp_path: Path) -> None:
    out = tmp_path / "v.json"
    assert run(tmp_path / "nowhere", FakeTransport(page([])), out) != 0
    assert not out.exists()


# --- The token stays secret --------------------------------------------------


def test_token_absent_from_output_on_success(
    root: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "v.json"
    assert run(root, FakeTransport(page([node("alpha")])), out) == 0
    captured = capfd.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert TOKEN not in out.read_text()


def test_remote_text_is_not_echoed(
    root: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    echo = f"bad token {TOKEN} and a remote message"
    for response in (
        (401, echo.encode()),
        {**page([]), "errors": [{"message": echo}]},
        (200, echo.encode()),
        OSError(echo),
    ):
        assert run(root, FakeTransport(response), tmp_path / "v.json") != 0
    captured = capfd.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "remote message" not in captured.out + captured.err


# --- Redirects, over a real local HTTP server --------------------------------


class Recorder(BaseHTTPRequestHandler):
    log: list[tuple[str, str, str | None]]
    behaviour: Callable[[BaseHTTPRequestHandler], None]

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.log.append((self.command, self.path, self.headers.get("Authorization")))
        self.behaviour(self)

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args: Any) -> None:
        pass


def serve(
    behaviour: Callable[[BaseHTTPRequestHandler], None],
) -> tuple[ThreadingHTTPServer, list[tuple[str, str, str | None]]]:
    log: list[tuple[str, str, str | None]] = []
    handler = type(
        "Handler", (Recorder,), {"log": log, "behaviour": staticmethod(behaviour)}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, log


def url_of(server: ThreadingHTTPServer, path: str = "/graphql") -> str:
    return f"http://127.0.0.1:{server.server_address[1]}{path}"


def test_redirect_is_not_followed_and_token_not_resent() -> None:
    target, target_log = serve(lambda h: (h.send_response(200), h.end_headers()))

    def redirect(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(307)
        handler.send_header("Location", url_of(target, "/stolen"))
        handler.end_headers()

    origin, origin_log = serve(redirect)
    try:
        status, _ = post_graphql(b"{}", TOKEN, url_of(origin))
        assert status == 307
        with pytest.raises(VotesError) as excinfo:
            fetch_votes(
                "o",
                "r",
                "c",
                ["alpha"],
                TOKEN,
                lambda body, token: post_graphql(body, token, url_of(origin)),
            )
        assert TOKEN not in str(excinfo.value)
    finally:
        origin.shutdown()
        target.shutdown()
    assert [entry[1] for entry in origin_log] == ["/graphql", "/graphql"]
    assert origin_log[0][2] == f"Bearer {TOKEN}"
    assert target_log == []


def test_real_http_roundtrip_sends_the_token_only_in_the_header() -> None:
    payload = json.dumps(page([node("alpha", 7)])).encode()

    def ok(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    server, log = serve(ok)
    try:
        result = fetch_votes(
            "o",
            "r",
            "c",
            ["alpha"],
            TOKEN,
            lambda body, token: post_graphql(body, token, url_of(server)),
        )
    finally:
        server.shutdown()
    assert result["alpha"]["votes"] == 7
    assert log == [("POST", "/graphql", f"Bearer {TOKEN}")]


def test_request_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    server, _ = serve(lambda h: release.wait(5))
    monkeypatch.setattr(votes, "REQUEST_TIMEOUT", 0.3)
    try:
        with pytest.raises(VotesError):
            fetch_votes(
                "o",
                "r",
                "c",
                ["alpha"],
                TOKEN,
                lambda body, token: post_graphql(body, token, url_of(server)),
            )
    finally:
        release.set()
        server.shutdown()


def test_oversized_response_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(votes, "MAX_RESPONSE_BYTES", 10)
    transport = FakeTransport((200, b"x" * 11))
    with pytest.raises(VotesError):
        fetch_votes("o", "r", "c", ["alpha"], TOKEN, transport)
