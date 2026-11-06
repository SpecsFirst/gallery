"""Count the votes of every template from its GitHub Discussion.

``python -m gallery.votes [--root DIR] --output FILE`` writes ``votes.json`` (see
``architecture.md``, ADR-004 and "Data / interfaces"): for each registry id that has a
Discussion in the giscus category, the 👍 reaction count and the Discussion url.

Security model, in short:

* The token comes from the environment variable ``GITHUB_TOKEN`` only (never a
  command-line option) and goes only in the ``Authorization`` header of the one
  GraphQL endpoint. It is never logged.
* Redirects are never followed: urllib would resend the ``Authorization`` header.
* Titles come from strangers. A Discussion counts only if its title is exactly a
  registry id; other titles are ignored and never logged.
* All or nothing: any failure exits non-zero and writes no file. Error messages are
  fixed text plus at most the HTTP status, because they end up in public Actions logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gallery.build import PACKAGE_ROOT, load_site_config
from gallery.detect import write_output
from gallery.registry import load_registry

API_URL = "https://api.github.com/graphql"
TOKEN_ENV = "GITHUB_TOKEN"
REQUEST_TIMEOUT = 30  # seconds, for each request
MAX_PAGES = 20  # pages of 100 Discussions: more than that is an error
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DISCUSSION_URL_PREFIX = "https://github.com/"

QUERY = (
    "query($owner:String!,$name:String!,$cat:ID!,$after:String)"
    "{repository(owner:$owner,name:$name)"
    "{discussions(first:100,after:$after,categoryId:$cat,"
    "orderBy:{field:CREATED_AT,direction:ASC})"
    "{pageInfo{hasNextPage endCursor} "
    "nodes{title url reactions(content:THUMBS_UP,first:1){totalCount}}}}}"
)

# A transport sends the JSON body with the token and returns (status, body).
Transport = Callable[[bytes, str], tuple[int, bytes]]


class VotesError(Exception):
    """A failure whose message is safe to print (fixed text, at most a status)."""


# --- Transport ---------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the default handler would resend Authorization."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def post_graphql(body: bytes, token: str, url: str = API_URL) -> tuple[int, bytes]:
    """POST ``body`` to the GraphQL endpoint; return (status, body), no redirect."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "specsfirst-gallery-votes",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return status, b""


# --- Reading the API ---------------------------------------------------------


def _split_repo(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise VotesError("site.yml: giscus.repo must be 'owner/name'")
    return owner, name


def _request_page(
    transport: Transport, token: str, variables: dict[str, Any]
) -> dict[str, Any]:
    """Run the query once and return the checked ``discussions`` object."""
    body = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
    try:
        status, raw = transport(body, token)
    except (OSError, ValueError):
        # Network error, timeout (an OSError) or a refused header; never the detail.
        raise VotesError("request to the GitHub API failed") from None
    if status != 200:
        raise VotesError(f"unexpected HTTP status {status} from the GitHub API")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise VotesError("the GitHub API response is too large")
    try:
        document = json.loads(raw)
    except ValueError:
        raise VotesError("the GitHub API response is not valid JSON") from None
    return _check_page(document)


def _malformed() -> VotesError:
    return VotesError("the GitHub API response is not in the expected format")


def _check_page(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise _malformed()
    if document.get("errors"):
        raise VotesError("the GitHub API returned errors")
    data = document.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    if repository is None:
        raise VotesError("the repository was not found by the GitHub API")
    discussions = (
        repository.get("discussions") if isinstance(repository, dict) else None
    )
    if not isinstance(discussions, dict):
        raise _malformed()
    page_info = discussions.get("pageInfo")
    nodes = discussions.get("nodes")
    if not isinstance(page_info, dict) or not isinstance(nodes, list):
        raise _malformed()
    has_next = page_info.get("hasNextPage")
    cursor = page_info.get("endCursor")
    if not isinstance(has_next, bool) or not (
        cursor is None or isinstance(cursor, str)
    ):
        raise _malformed()
    if has_next and not cursor:
        raise _malformed()
    for node in nodes:
        _check_node(node)
    return discussions


def _check_node(node: Any) -> None:
    if not isinstance(node, dict):
        raise _malformed()
    title = node.get("title")
    url = node.get("url")
    reactions = node.get("reactions")
    if not isinstance(title, str) or not isinstance(url, str):
        raise _malformed()
    if not url.startswith(DISCUSSION_URL_PREFIX):
        raise _malformed()
    count = reactions.get("totalCount") if isinstance(reactions, dict) else None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise _malformed()


def fetch_votes(
    owner: str,
    name: str,
    category_id: str,
    ids: Sequence[str],
    token: str,
    transport: Transport = post_graphql,
) -> dict[str, dict[str, Any]]:
    """Return ``{id: {"votes", "discussion_url"}}`` for the ids that have a Discussion.

    Discussions are read oldest first; with two of the same title the oldest counts.
    """
    wanted = set(ids)
    found: dict[str, dict[str, Any]] = {}
    after: str | None = None
    for _ in range(MAX_PAGES):
        discussions = _request_page(
            transport,
            token,
            {"owner": owner, "name": name, "cat": category_id, "after": after},
        )
        for node in discussions["nodes"]:
            title = node["title"]
            if title in wanted and title not in found:
                found[title] = {
                    "votes": node["reactions"]["totalCount"],
                    "discussion_url": node["url"],
                }
        if not discussions["pageInfo"]["hasNextPage"]:
            return found
        after = discussions["pageInfo"]["endCursor"]
    raise VotesError(f"more than {MAX_PAGES} pages of Discussions")


# --- Command line ------------------------------------------------------------


def _site_config_path(root: Path) -> Path:
    candidate = root / "site.yml"
    return candidate if candidate.exists() else PACKAGE_ROOT / "site.yml"


def main(argv: Sequence[str] | None = None, transport: Transport = post_graphql) -> int:
    parser = argparse.ArgumentParser(
        description="Count the votes of every template from its GitHub Discussion."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root containing taxonomy.yml, schema/, templates/ and site.yml",
    )
    parser.add_argument("--output", type=Path, required=True, help="votes.json")
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(
            f"votes: the environment variable {TOKEN_ENV} is not set", file=sys.stderr
        )
        return 2

    try:
        registry = load_registry(args.root)
        site = load_site_config(_site_config_path(args.root))
        owner, name = _split_repo(site.giscus["repo"])
    except VotesError as exc:
        print(f"votes: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - any failure to load is fatal
        print("votes: cannot load the registry or site.yml", file=sys.stderr)
        return 2

    try:
        templates = fetch_votes(
            owner,
            name,
            site.giscus["category_id"],
            [entry.id for entry in registry.entries],
            token,
            transport,
        )
    except VotesError as exc:
        print(f"votes: {exc}", file=sys.stderr)
        return 1

    document = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "templates": templates,
    }
    try:
        write_output(document, args.output)
    except OSError:
        print("votes: cannot write the output file", file=sys.stderr)
        return 2

    print(f"votes: {len(templates)} template(s) with a Discussion", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
