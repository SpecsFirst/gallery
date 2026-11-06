"""Build the static gallery site.

``python -m gallery.build [--root DIR] [--detected FILE] [--votes FILE] --output DIR``
turns the registry, ``detected.json`` and ``votes.json`` into the site: the gallery
page and one page per template.

Everything shown from an entry or from ``detected.json`` comes from strangers
(ADR-002): Jinja2 autoescape is on for every template, and the only HTML that is not
escaped is the preview of ``main.md``, produced by a renderer that disables raw HTML,
keeps only safe links and replaces images by their alt text. A missing data file, or
an id missing from a data file, means "no data", never an error (ADR-004).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markupsafe import Markup

from gallery.registry import Entry, RegistryError, Taxonomy, load_registry

# Where the packaged defaults (site/, site.yml) live when --root does not hold them.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPDX_LIKE = re.compile(r"^[A-Za-z0-9.+-]{1,40}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_FRONT_MATTER = re.compile(
    r"\A﻿?---[ \t]*\r?\n(?:.*?\r?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)

LINK_REL = "nofollow noopener noreferrer"
SAFE_LINK_SCHEMES = ("http", "https", "mailto")

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


class BuildError(Exception):
    """Raised when the site cannot be built (bad configuration or data file)."""


# --------------------------------------------------------------------------
# Preview: markdown-it-py with raw HTML off, safe links only, no images.
# --------------------------------------------------------------------------


def strip_front_matter(text: str) -> str:
    """Remove a leading YAML front matter block."""
    return _FRONT_MATTER.sub("", text, count=1)


def link_allowed(href: str) -> bool:
    """True for an absolute http(s) link or a mailto link, false for the rest."""
    try:
        parts = urlsplit(href)
    except ValueError:
        return False
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        return bool(parts.netloc)
    if scheme == "mailto":
        return bool(parts.path)
    return False


def _render_image(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    """An image is rendered as its alt text: no request is ever made."""
    token = tokens[idx]
    if not token.children:
        return ""
    return escapeHtml(self.renderInlineAsText(token.children, options, env))


def _open_table(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    return '<div class="table-scroll">' + self.renderToken(tokens, idx, options, env)


def _close_table(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    return self.renderToken(tokens, idx, options, env) + "</div>"


def _sanitize_tokens(state: Any) -> None:
    """Core rule: restrict links, drop inline styles and shift heading levels."""
    for block in state.tokens:
        if block.type in ("heading_open", "heading_close"):
            # The page owns h1 and h2: the preview starts at h3.
            level = int(block.tag[1:])
            block.tag = f"h{min(level + 2, 6)}"
        if block.type in ("th_open", "td_open"):
            block.attrs.pop("style", None)
        if block.type != "inline" or not block.children:
            continue
        dropping = False
        for token in block.children:
            if token.type == "link_open":
                href = token.attrGet("href")
                if isinstance(href, str) and link_allowed(href):
                    token.attrs = {"href": href, "rel": LINK_REL}
                else:
                    token.hidden = True
                    dropping = True
            elif token.type == "link_close" and dropping:
                token.hidden = True
                dropping = False


def make_preview_renderer() -> MarkdownIt:
    """The sanitizing renderer used for every preview."""
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    # Every link is parsed, so that the sanitizer decides, and a refused one keeps
    # its text instead of showing its markdown source.
    md.validateLink = lambda url: True
    md.add_render_rule("image", _render_image)
    md.add_render_rule("table_open", _open_table)
    md.add_render_rule("table_close", _close_table)
    md.core.ruler.push("gallery_sanitize", _sanitize_tokens)
    return md


def render_preview(md: MarkdownIt, text: str) -> Markup:
    """Render ``main.md`` to safe HTML (the only value passed as Markup)."""
    return Markup(md.render(strip_front_matter(text)))


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
    tagline: str
    giscus: dict[str, str]

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.giscus['repo']}"

    @property
    def contributing_url(self) -> str:
        return f"{self.repo_url}/blob/main/CONTRIBUTING.md"


def load_site_config(path: Path) -> SiteConfig:
    """Read site.yml: title, tagline and the giscus configuration."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BuildError(f"{path}: cannot read site configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise BuildError(f"{path}: site configuration must be a mapping")
    giscus = raw.get("giscus")
    if not isinstance(giscus, dict):
        raise BuildError(f"{path}: missing 'giscus' mapping")
    for key in ("title", "tagline"):
        if not isinstance(raw.get(key), str):
            raise BuildError(f"{path}: '{key}' must be a string")
    for key in ("repo", "repo_id", "category", "category_id"):
        if not isinstance(giscus.get(key), str) or not giscus[key]:
            raise BuildError(f"{path}: 'giscus.{key}' must be a non-empty string")
    return SiteConfig(
        title=raw["title"],
        tagline=raw["tagline"],
        giscus={k: str(v) for k, v in giscus.items()},
    )


# --------------------------------------------------------------------------
# View models
# --------------------------------------------------------------------------


@dataclass
class TemplateView:
    """Everything a card or a page shows about one template."""

    id: str
    name: str
    description: str
    kind: str
    tags: list[str]
    language: str
    url: str
    repo_link: str | None
    path: str | None
    ref: str | None
    votes: int = 0
    state: str = "none"  # "ok", "failed" or "none" (no detected data)
    notice: str | None = None
    license: str | None = None
    copier: bool = False
    tools: list[str] = field(default_factory=list)
    updated: datetime | None = None
    commit: str | None = None
    tree: list[str] = field(default_factory=list)
    tree_truncated: bool = False
    command: str | None = None
    preview: Markup | None = None

    @property
    def sort_name(self) -> str:
        return self.name.casefold()

    @property
    def updated_ts(self) -> int | None:
        return None if self.updated is None else int(self.updated.timestamp())

    @property
    def updated_iso(self) -> str | None:
        return None if self.updated is None else self.updated.isoformat()

    @property
    def updated_date(self) -> str | None:
        return None if self.updated is None else self.updated.date().isoformat()


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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_https(url: str) -> bool:
    """A repository URL we accept to link to: https, no spaces, no credentials."""
    if not url.startswith("https://") or re.search(r"\s", url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return bool(parts.hostname) and parts.username is None and parts.password is None


def _license_label(value: Any) -> str:
    if value is None:
        return "No license file"
    if isinstance(value, str) and value != "unknown" and _SPDX_LIKE.match(value):
        return value
    return "License not identified"


def copier_command(url: str, path: str | None, ref: str | None) -> str | None:
    """The command to use a template with Copier, ``<name>`` kept literal.

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
    if path is None:
        if ref is None:
            return f"copier copy {quote(url)} <name>"
        return f"copier copy --vcs-ref {quote(ref)} {quote(url)} <name>"
    steps = [f"git clone {quote(url)} <name>-src"]
    if ref is not None:
        steps.append(f"git -C <name>-src checkout {quote(ref)}")
    steps.append(f"copier copy <name>-src/{quote(path)} <name>")
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


def build_view(
    entry: Entry,
    taxonomy: Taxonomy,
    detected: dict[str, Any] | None,
    votes: dict[str, Any] | None,
    markdown: MarkdownIt,
) -> TemplateView:
    """Merge an entry with its detected data and its votes."""
    fields = entry.fields
    url = _text(fields.get("url"), "") or ""
    path = _text(fields.get("path")) or None
    ref = _text(fields.get("ref")) or None
    view = TemplateView(
        id=entry.id,
        name=_text(fields.get("name")) or entry.id,
        description=_text(fields.get("description"), "") or "",
        kind=_text(fields.get("kind"), "") or "",
        tags=resolved_tags(taxonomy, fields.get("tags")),
        language=_text(entry.language, "en") or "en",
        url=url,
        repo_link=url if _is_https(url) else None,
        path=path,
        ref=ref,
    )

    count = (votes or {}).get("votes")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        view.votes = count

    status = (detected or {}).get("status")
    if detected is None or status not in ("ok", "failed"):
        view.notice = (
            "This template has not been analysed yet. "
            "The information below comes from its entry."
        )
        return view
    if status == "failed":
        view.state = "failed"
        reason = ERROR_REASONS.get(detected.get("error"))
        view.notice = (
            "The automatic analysis of this template is not available"
            + (f": {reason}" if reason else "")
            + ". The information below comes from its entry."
        )
        return view

    view.state = "ok"
    view.license = _license_label(detected.get("license"))
    view.copier = detected.get("copier") is True
    view.tools = _text_list(detected.get("tools"))
    view.updated = _parse_date(detected.get("last_update"))
    commit = detected.get("commit")
    if isinstance(commit, str) and _COMMIT.match(commit):
        view.commit = commit[:7]
    view.tree = _text_list(detected.get("tree"))
    view.tree_truncated = detected.get("tree_truncated") is True
    preview = detected.get("preview")
    if isinstance(preview, str) and preview.strip():
        view.preview = render_preview(markdown, preview)
    if view.copier:
        view.command = copier_command(url, path, ref)
    return view


def vote_order(view: TemplateView) -> tuple[int, str, str]:
    """Ranking: votes descending, then name, then id."""
    return (-view.votes, view.sort_name, view.id)


def build_filters(taxonomy: Taxonomy) -> dict[str, Any]:
    """Filters come only from the taxonomy: kinds, then groups, then 'Other'."""
    kinds = [
        {"name": name, "description": desc} for name, desc in taxonomy.kinds.items()
    ]
    active = taxonomy.active_tags
    seen: set[str] = set()
    groups: list[dict[str, Any]] = []

    def add_group(name: str, tag_names: Sequence[str]) -> None:
        tags = []
        for tag in tag_names:
            if tag in active and tag not in seen:
                seen.add(tag)
                tags.append({"name": tag, "description": active[tag]})
        if tags:
            groups.append({"name": name, "tags": tags})

    for group in taxonomy.groups:
        add_group(group.name, group.tags)
    add_group("Other", list(taxonomy.ungrouped_active_tags))
    return {"kinds": kinds, "groups": groups}


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
    markdown = make_preview_renderer()

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
                markdown,
            )
        )

    context = {"site": site}
    (output / "assets").mkdir(parents=True, exist_ok=True)
    (output / "schema").mkdir(parents=True, exist_ok=True)

    index = env.get_template("index.html").render(
        **context,
        root="",
        cards=sorted(views, key=vote_order),
        filters=build_filters(registry.taxonomy),
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
    shutil.copyfile(
        root / "schema" / "template.schema.json",
        output / "schema" / "template.schema.json",
    )
    (output / ".nojekyll").write_text("", encoding="utf-8")
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
