"""Build the static gallery site.

``python -m gallery.build [--root DIR] [--detected FILE] [--votes FILE] --output DIR``
turns the registry, ``detected.json`` and ``votes.json`` into the site: the gallery
page and one page per template.

Everything shown from an entry or from ``detected.json`` comes from strangers
(ADR-002): Jinja2 autoescape is on for every template and nothing is passed as
markup. A missing data file, or an id missing from a data file, means "no data",
never an error (ADR-004).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from gallery.detect import MAX_TREE_ENTRIES
from gallery.registry import Entry, RegistryError, Taxonomy, load_registry
from gallery.tree import render_tree

# Where the packaged defaults (site/, site.yml) live when --root does not hold them.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPDX_LIKE = re.compile(r"^[A-Za-z0-9.+-]{1,40}$")

LINK_REL = "nofollow noopener noreferrer"
TAG_COLORS = 6
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

# The closed list of detect.py error codes, in plain words. Anything else, and the
# raw code itself, is never displayed.
ERROR_REASONS = {
    "timeout": "the repository took too long to fetch",
    "too_large": "the repository is too large to analyse",
    "unreachable": "the repository could not be fetched",
    "ref_not_found": "the requested ref was not found in the repository",
    "path_not_found": "the requested path was not found in the repository",
    "invalid_entry": "the path or ref of this entry is not usable",
}
GENERIC_FAILURE = "the analysis failed"
PROJECT_NAME = "my-project"


class BuildError(Exception):
    """Raised when the site cannot be built (bad configuration or data file)."""


# --------------------------------------------------------------------------
# Data files
# --------------------------------------------------------------------------


def load_data_file(path: Path | None) -> dict[str, dict[str, Any]]:
    """Per-template mapping of a detected.json or votes.json, ``{}`` if missing."""
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"{path}: cannot read data file: {exc}") from exc
    templates = raw.get("templates", {}) if isinstance(raw, dict) else None
    if not isinstance(templates, dict):
        raise BuildError(f"{path}: expected an object with a 'templates' object")
    return {k: v for k, v in templates.items() if isinstance(v, dict)}


@dataclass(frozen=True)
class SiteConfig:
    """The public configuration read from site.yml."""

    title: str
    base_url: str
    discussions: dict[str, str]

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.discussions['repo']}"

    @property
    def submit_url(self) -> str:
        return f"{self.repo_url}/issues/new?template=submit-template.yml"


def load_site_config(path: Path) -> SiteConfig:
    """Read site.yml: the title and the Discussions configuration."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BuildError(f"{path}: cannot read site configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise BuildError(f"{path}: site configuration must be a mapping")
    discussions = raw.get("discussions")
    if not isinstance(discussions, dict):
        raise BuildError(f"{path}: missing 'discussions' mapping")
    if not isinstance(raw.get("title"), str):
        raise BuildError(f"{path}: 'title' must be a string")
    if not _is_valid_base_url(raw.get("base_url")):
        raise BuildError(
            f"{path}: 'base_url' must be an https URL ending with '/' "
            "and no credentials"
        )
    for key in ("repo", "repo_id", "category", "category_id"):
        if not isinstance(discussions.get(key), str) or not discussions[key]:
            raise BuildError(f"{path}: 'discussions.{key}' must be a non-empty string")
    return SiteConfig(
        title=raw["title"],
        base_url=raw["base_url"],
        discussions={k: str(v) for k, v in discussions.items()},
    )


# --------------------------------------------------------------------------
# View models
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TagView:
    """A tag as shown: its name and the index of its color class (``c0``-``c5``)."""

    name: str
    color: int


@dataclass
class TemplateView:
    """Everything a card or a page shows about one template."""

    id: str
    name: str
    description: str
    tags: list[TagView]
    url: str
    repo_link: str | None
    source_text: str
    path: str | None
    ref: str | None
    votes: int
    star_url: str
    placement: str = "standalone"
    placement_text: str = "Own repo"
    license_text: str = "Unknown"
    license_class: str = "muted"  # "", "warn" or "muted"
    updated: datetime | None = None
    tree_text: str | None = None
    tree_summary: str = ""
    tree_truncated: bool = False
    tree_unavailable: str = "File tree unavailable: not analysed yet."
    command: str | None = None
    kind: str | None = None
    api_tags: tuple[str, ...] = ()

    @property
    def sort_name(self) -> str:
        return self.name.casefold()

    @property
    def updated_ts(self) -> int | None:
        return None if self.updated is None else int(self.updated.timestamp())

    @property
    def updated_iso(self) -> str | None:
        return None if self.updated is None else self.updated.date().isoformat()

    @property
    def updated_text(self) -> str | None:
        if self.updated is None:
            return None
        date = self.updated.date()
        return f"{MONTHS[date.month - 1]} {date.day}, {date.year}"

    @property
    def tag_names(self) -> list[str]:
        return [tag.name for tag in self.tags]

    @property
    def tree_note(self) -> str:
        return f"Only the first {MAX_TREE_ENTRIES} entries are listed."

    @property
    def star_label(self) -> str:
        noun = "star" if self.votes == 1 else "stars"
        return f"Star {self.name} on GitHub, {self.votes} {noun}"


def _clean(value: str) -> str:
    """Replace control characters, so a value cannot fake lines or terminals."""
    return _CONTROL_CHARS.sub(" ", value)


def _text(value: Any, default: str | None = None) -> str | None:
    return _clean(value) if isinstance(value, str) else default


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item) for item in value if isinstance(item, str)]


def _parse_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _is_https(url: str) -> bool:
    """A repository URL we accept to link to: https, no spaces, no credentials."""
    if not url.startswith("https://") or re.search(r"\s", url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.hostname) and parts.username is None and parts.password is None


def _is_valid_base_url(value: Any) -> bool:
    """The site's own URL: https, no credentials, and it ends with ``/``."""
    if not isinstance(value, str) or not value.endswith("/"):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and (
        parts.username is None and parts.password is None
    )


def _license_view(value: Any) -> tuple[str, str]:
    """Text and CSS class of a detected license (``None`` means no license file)."""
    if value is None:
        return "None", "warn"
    if isinstance(value, str) and value != "unknown" and _SPDX_LIKE.match(value):
        return value, ""
    return "Unknown", ""


def _source_text(url: str) -> str:
    """The url as shown: no scheme, no trailing slash."""
    return url.removeprefix("https://").rstrip("/")


def copier_command(
    url: str, path: str | None, ref: str | None, in_repo: bool = False
) -> str | None:
    """The command to use a template with Copier, as pasted into a terminal.

    Standalone copies into a new ``my-project`` folder; in-repo copies into the
    current directory (``.``), so the template lands in an existing code repo.
    Values are shell-quoted. ``None`` when a value could be read as an option or
    escape the checkout (validation refuses these too).
    """
    if not _is_https(url):
        return None
    if ref is not None and (not ref or ref.startswith("-")):
        return None
    if path is not None:
        segments = path.split("/")
        if not path or path.startswith("/") or ".." in segments:
            return None
    quote = shlex.quote
    if in_repo:
        destination = "."
        clone = "../template-src"
    else:
        destination = PROJECT_NAME
        clone = f"{PROJECT_NAME}-src"
    if path is None:
        if ref is None:
            return f"copier copy {quote(url)} {destination}"
        return f"copier copy --vcs-ref {quote(ref)} {quote(url)} {destination}"
    steps = [f"git clone {quote(url)} {clone}"]
    if ref is not None:
        steps.append(f"git -C {clone} checkout {quote(ref)}")
    steps.append(f"copier copy {clone}/{quote(path)} {destination}")
    return " && ".join(steps)


def resolved_tags(taxonomy: Taxonomy, tags: Any) -> list[str]:
    """Tags as shown: a deprecated tag becomes its target, without duplicates."""
    result: list[str] = []
    if not isinstance(tags, list):
        return result
    for tag in tags:
        if not isinstance(tag, str):
            continue
        name = _clean(taxonomy.resolve_tag(tag) or tag)
        if name not in result:
            result.append(name)
    return result


def tag_positions(taxonomy: Taxonomy) -> dict[str, int]:
    """Position of every kind, then every tag (deprecated included), in file order."""
    positions: dict[str, int] = {}
    for name in (*taxonomy.kinds, *taxonomy.tags):
        positions.setdefault(name, len(positions))
    return positions


def _star_url(votes: dict[str, Any] | None, repo_url: str) -> str:
    """The discussion to open, else the repository's Discussions page."""
    url = (votes or {}).get("discussion_url")
    if (
        isinstance(url, str)
        and url.startswith("https://github.com/")
        and _is_https(url)
    ):
        return url
    return f"{repo_url}/discussions"


def build_view(
    entry: Entry,
    taxonomy: Taxonomy,
    detected: dict[str, Any] | None,
    votes: dict[str, Any] | None,
    site: SiteConfig,
) -> TemplateView:
    """Merge an entry with its detected data and its votes."""
    fields = entry.fields
    url = _text(fields.get("url"), "") or ""
    path = _text(fields.get("path")) or None
    ref = _text(fields.get("ref")) or None
    placement = _text(fields.get("placement")) or "standalone"
    if placement not in ("standalone", "in-repo"):
        placement = "standalone"
    positions = tag_positions(taxonomy)
    names: list[str] = []
    kind = _text(fields.get("kind"), "") or ""
    for name in ([kind] if kind else []) + resolved_tags(taxonomy, fields.get("tags")):
        if name not in names:
            names.append(name)
    linked = _is_https(url)
    view = TemplateView(
        id=entry.id,
        name=_text(fields.get("name")) or entry.id,
        description=_text(fields.get("description"), "") or "",
        tags=[TagView(n, positions.get(n, 0) % TAG_COLORS) for n in names],
        url=url,
        repo_link=url if linked else None,
        source_text=_source_text(url) if linked else url,
        path=path,
        ref=ref,
        votes=0,
        star_url=_star_url(votes, site.repo_url),
        placement=placement,
        placement_text="Own repo" if placement == "standalone" else "Code repo",
        kind=_text(fields.get("kind")) or None,
        api_tags=tuple(resolved_tags(taxonomy, fields.get("tags"))),
    )

    entry_command = _text(fields.get("command"))
    if entry_command is not None:
        # the entry's own command wins and shows even when detection failed
        view.command = entry_command

    count = (votes or {}).get("votes")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        view.votes = count

    status = (detected or {}).get("status")
    if detected is None or status not in ("ok", "failed"):
        return view
    if status == "failed":
        error = detected.get("error")
        reason = ERROR_REASONS.get(error) if isinstance(error, str) else None
        reason = reason or GENERIC_FAILURE
        view.tree_unavailable = f"File tree unavailable: {reason}."
        return view

    view.license_text, view.license_class = _license_view(detected.get("license"))
    view.updated = _parse_date(detected.get("last_update"))
    tree = detected.get("tree")
    if isinstance(tree, list):
        rendered = render_tree(_text_list(tree))
        view.tree_text = rendered.text
        view.tree_summary = rendered.summary
        view.tree_truncated = detected.get("tree_truncated") is True
    if detected.get("copier") is True and view.command is None:
        view.command = copier_command(url, path, ref, in_repo=placement == "in-repo")
    return view


def vote_order(view: TemplateView) -> tuple[int, int, int, str, str]:
    """Ranking: stars, then last update (unknown last), then name, then id."""
    ts = view.updated_ts
    return (
        -view.votes,
        1 if ts is None else 0,
        0 if ts is None else -ts,
        view.sort_name,
        view.id,
    )


def build_filters(views: Sequence[TemplateView], taxonomy: Taxonomy) -> list[TagView]:
    """The tags used by at least one card, in taxonomy order, others last."""
    positions = tag_positions(taxonomy)
    used: dict[str, TagView] = {}
    for view in views:
        for tag in view.tags:
            used.setdefault(tag.name, tag)
    last = len(positions)
    ordered = sorted(
        enumerate(used.values()),
        key=lambda item: (positions.get(item[1].name, last), item[0]),
    )
    return [tag for _, tag in ordered]


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def _resolve(root: Path, name: str) -> Path:
    """``root/name`` when it exists, else the packaged default."""
    candidate = root / name
    return candidate if candidate.exists() else PACKAGE_ROOT / name


def build(
    root: Path,
    output: Path,
    detected_file: Path | None = None,
    votes_file: Path | None = None,
) -> int:
    """Write the site under ``output``; return the number of template pages."""
    registry = load_registry(root)
    site = load_site_config(_resolve(root, "site.yml"))
    site_dir = _resolve(root, "site")
    detected = load_data_file(detected_file)
    votes = load_data_file(votes_file)

    env = Environment(
        loader=FileSystemLoader(site_dir),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    views: list[TemplateView] = []
    for entry in registry.entries:
        if not ID_PATTERN.match(entry.id):
            print(f"skipping {entry.id!r}: not a valid template id", file=sys.stderr)
            continue
        views.append(
            build_view(
                entry,
                registry.taxonomy,
                detected.get(entry.id),
                votes.get(entry.id),
                site,
            )
        )

    context = {"site": site}
    (output / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (output / "schema").mkdir(parents=True, exist_ok=True)

    index = env.get_template("index.html").render(
        **context,
        root="",
        cards=sorted(views, key=vote_order),
        filters=build_filters(views, registry.taxonomy),
    )
    (output / "index.html").write_text(index, encoding="utf-8")

    page = env.get_template("template.html")
    for view in views:
        page_dir = output / "templates" / view.id
        page_dir.mkdir(parents=True, exist_ok=True)
        html = page.render(**context, root="../../", v=view)
        (page_dir / "index.html").write_text(html, encoding="utf-8")

    for asset in ("site.css", "site.js"):
        shutil.copyfile(site_dir / "assets" / asset, output / "assets" / asset)
    shutil.copytree(
        site_dir / "assets" / "fonts", output / "assets" / "fonts", dirs_exist_ok=True
    )
    shutil.copyfile(
        root / "schema" / "template.schema.json",
        output / "schema" / "template.schema.json",
    )
    (output / ".nojekyll").write_text("", encoding="utf-8")

    from gallery.api import write_documents

    write_documents(views, detected, site, output)
    return len(views)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static gallery site.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root containing taxonomy.yml, schema/ and templates/",
    )
    parser.add_argument("--detected", type=Path, help="detected.json (optional)")
    parser.add_argument("--votes", type=Path, help="votes.json (optional)")
    parser.add_argument(
        "--output", type=Path, required=True, help="directory to write the site to"
    )
    args = parser.parse_args(argv)
    try:
        count = build(args.root, args.output, args.detected, args.votes)
    except (RegistryError, BuildError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"built {count} template page(s) in {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
