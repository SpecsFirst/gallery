"""Create the missing Discussion of each template, one per registry id.

``python -m gallery.discussions [--root DIR] [--dry-run]`` lists the Discussions of the
category (the query and checks of ``gallery.votes``) and creates, for each registry id
that has no Discussion titled exactly that id, one Discussion (see ``architecture.md``,
ADR-004 and ADR-006). The star button of a template links to it, and ``gallery.votes``
counts its 👍 reactions.

Security model, as ``gallery.votes``:

* The token comes from the environment variable ``GITHUB_TOKEN`` only and goes only in
  the ``Authorization`` header of the one GraphQL endpoint. It is never logged.
* Only the registry of the checked-out branch is read, never a template.
* Titles of other Discussions come from strangers: they are never logged.
* Error messages are fixed text plus at most the HTTP status (public Actions logs).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from gallery.build import load_site_config
from gallery.registry import Entry, load_registry
from gallery.votes import (
    DISCUSSION_URL_PREFIX,
    TOKEN_ENV,
    Transport,
    VotesError,
    list_discussions,
    malformed,
    post_checked,
    post_graphql,
    site_config_path,
    split_repo,
)

MAX_CREATE = 10  # Discussions created by one run: the rest wait for the next run

MUTATION = (
    "mutation($repo:ID!,$cat:ID!,$title:String!,$body:String!)"
    "{createDiscussion(input:{repositoryId:$repo,categoryId:$cat,title:$title,"
    "body:$body}){discussion{url}}}"
)


def gallery_url(owner: str, name: str) -> str:
    """Base url of the published gallery, e.g. ``https://specsfirst.github.io/gallery/``."""
    return f"https://{owner.lower()}.github.io/{name}/"


def _no_mention(text: str) -> str:
    """Write every ``@`` as ``@&#8203;`` (zero-width space) so it mentions no one."""
    return text.replace("@", "@&#8203;")


def discussion_body(entry: Entry, base_url: str) -> str:
    """The body of the Discussion of ``entry``."""
    quote = "\n".join(
        f"> {line}" for line in _no_mention(entry.description).splitlines() or [""]
    )
    return (
        f"Star **{_no_mention(entry.name)}** by reacting with 👍 to this post. "
        "One GitHub account, one star; the "
        "gallery counts them every few hours.\n"
        "\n"
        f"{quote}\n"
        "\n"
        f"- Gallery page: {base_url}templates/{entry.id}/\n"
        f"- Source: {entry.url}"
    )


def create_discussion(
    repo_id: str,
    category_id: str,
    title: str,
    body: str,
    token: str,
    transport: Transport = post_graphql,
) -> str:
    """Create one Discussion; return its url (checked to be on github.com)."""
    document = post_checked(
        transport,
        token,
        MUTATION,
        {"repo": repo_id, "cat": category_id, "title": title, "body": body},
    )
    if not isinstance(document, dict):
        raise malformed()
    if document.get("errors"):
        raise VotesError("the GitHub API returned errors")
    data = document.get("data")
    created = data.get("createDiscussion") if isinstance(data, dict) else None
    discussion = created.get("discussion") if isinstance(created, dict) else None
    url = discussion.get("url") if isinstance(discussion, dict) else None
    if not isinstance(url, str) or not url.startswith(DISCUSSION_URL_PREFIX):
        raise malformed()
    return url


def main(argv: Sequence[str] | None = None, transport: Transport = post_graphql) -> int:
    parser = argparse.ArgumentParser(
        description="Create the missing GitHub Discussion of each template."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root containing taxonomy.yml, schema/, templates/ and site.yml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the Discussions that would be created, create none",
    )
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(
            f"discussions: the environment variable {TOKEN_ENV} is not set",
            file=sys.stderr,
        )
        return 2

    try:
        registry = load_registry(args.root)
        site = load_site_config(site_config_path(args.root))
        owner, name = split_repo(site.giscus["repo"])
        repo_id = site.giscus["repo_id"]
        category_id = site.giscus["category_id"]
    except VotesError as exc:
        print(f"discussions: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - any failure to load is fatal
        print("discussions: cannot load the registry or site.yml", file=sys.stderr)
        return 2

    try:
        existing = {
            node["title"]
            for node in list_discussions(owner, name, category_id, token, transport)
        }
    except VotesError as exc:
        print(f"discussions: {exc}", file=sys.stderr)
        return 1

    missing: list[Entry] = sorted(
        (entry for entry in registry.entries if entry.id not in existing),
        key=lambda entry: entry.id,
    )
    batch = missing[:MAX_CREATE]
    left = len(missing) - len(batch)
    base_url = gallery_url(owner, name)

    created = 0
    for entry in batch:
        if args.dry_run:
            print(f"would create {entry.id}")
            created += 1
            continue
        try:
            url = create_discussion(
                repo_id,
                category_id,
                entry.id,
                discussion_body(entry, base_url),
                token,
                transport,
            )
        except VotesError as exc:
            print(
                f"discussions: creating the Discussion of {entry.id}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"created {entry.id} {url}")
        created += 1

    verb = "would create" if args.dry_run else "created"
    print(
        f"discussions: {verb} {created}, {len(registry.entries) - len(missing)} "
        f"already present, {left} left for the next run",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
