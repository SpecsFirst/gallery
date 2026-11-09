"""Check a template submitted through the issue form (``submit-template.yml``).

``python -m gallery.submit check --event FILE [--root DIR] --out DIR`` reads the
event of an ``issues`` workflow run, builds the entry the form describes,
validates it with the registry's own checks, detects what the repository holds,
and writes ``<out>/submission.json`` (see ``architecture.md``, ADR-008): the
Markdown of the one comment the ``comment`` job posts on the issue. Nothing here
writes to the repository, and nothing here needs a token.

Everything in the issue is hostile. The body is only ever parsed as text. A
value reaches the comment only inside a code span or the fenced block of the
entry, never in a heading, a link text or a log line, and the log prints no
issue content at all.

Exit status: 0 for a checked submission, ok or not (and for a closed issue,
which has nothing to do); 1 when the check itself cannot run (bad event file,
unreadable form or registry).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

from gallery.detect import Options, detect_entry
from gallery.registry import (
    Entry,
    RegistryError,
    Taxonomy,
    load_entry,
    load_registry,
)
from gallery.validate import ID_RE, entry_issues

FORM_PATH = Path(".github") / "ISSUE_TEMPLATE" / "submit-template.yml"
MARKER = "<!-- gallery-submission -->"
NO_RESPONSE = "_No response_"
DEFAULT_REPOSITORY = "SpecsFirst/gallery"
FORM_FIELDS = ("url", "name", "description", "placement", "tags", "command", "agnostic")
ENTRY_ORDER = ("name", "url", "description", "kind", "tags", "placement", "command")

# The form's "Where do the specs live?" choices, as the entry's ``placement``.
PLACEMENT_BY_CHOICE = {
    "In their own repo": "standalone",
    "In the code repo": "in-repo",
}

MAX_BODY_BYTES = 64 * 1024
MAX_COMMENT_CHARS = 60_000
MAX_ID_CHARS = 50
MAX_CODE_SPAN_CHARS = 80
MAX_LISTED_VALUES = 10

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FENCE_RE = re.compile(r"`+")

DETECT_REASONS = {
    "timeout": "the repository took too long to fetch",
    "too_large": "the repository is too large to inspect",
    "unreachable": (
        "the repository could not be fetched: check that the URL is correct "
        "and that the repository is public"
    ),
    "ref_not_found": "the repository has no such branch or tag",
    "path_not_found": "the repository has no such folder",
    "invalid_entry": "the URL is not accepted for fetching",
}


class SubmitError(Exception):
    """The check itself cannot run (not a problem with the submission)."""


@dataclass(frozen=True)
class Problem:
    """One thing to fix: the form field it concerns and a fixed sentence."""

    field: str
    text: str


@dataclass(frozen=True)
class Outcome:
    """What the check found for one submission."""

    id: str | None
    yaml_text: str | None
    detected: dict[str, Any] | None
    problems: tuple[Problem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


# --- Form and body -----------------------------------------------------------


def load_form_labels(root: Path) -> dict[str, str]:
    """Field id -> label, read from the issue form itself."""
    path = root / FORM_PATH
    try:
        form = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise SubmitError(f"cannot read the issue form {FORM_PATH}") from exc
    items = form.get("body") if isinstance(form, dict) else None
    labels: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        attributes = item.get("attributes")
        label = attributes.get("label") if isinstance(attributes, dict) else None
        if isinstance(label, str) and label.strip():
            labels[item["id"]] = label.strip()
    missing = [name for name in FORM_FIELDS if name not in labels]
    if missing:
        raise SubmitError(f"the issue form has no field(s): {', '.join(missing)}")
    return labels


def parse_body(body: str, labels: dict[str, str]) -> dict[str, str]:
    """Values of the form fields in an issue body (``### Label`` sections).

    A missing section and ``_No response_`` both give an empty string; headings
    the form does not define are ignored, and the first section of a label wins.
    """
    by_heading = {f"### {label}": field for field, label in labels.items()}
    lines: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("### "):
            field = by_heading.get(line.rstrip())
            current = field if field is not None and field not in lines else None
            if current is not None:
                lines[current] = []
        elif current is not None:
            lines[current].append(line)
    values: dict[str, str] = {}
    for field in labels:
        value = "\n".join(lines.get(field, [])).strip()
        values[field] = "" if value == NO_RESPONSE else value
    return values


# --- Normalization -----------------------------------------------------------


def collapse(value: str) -> str:
    return " ".join(value.split())


def slugify(name: str) -> str:
    """The id a name gives: ASCII, lowercase, hyphens, at most 50 characters."""
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    if len(slug) > MAX_ID_CHARS:
        cut = slug[:MAX_ID_CHARS]
        if slug[MAX_ID_CHARS] != "-" and "-" in cut:
            cut = cut.rsplit("-", 1)[0]
        slug = cut.strip("-")
    return slug


def url_key(url: str) -> str:
    """Compare urls without trailing ``/`` and ``.git``, host in lowercase."""
    text = url.strip()
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return text.lower()
    path = parts.path.rstrip("/").removesuffix(".git").rstrip("/")
    return f"{host}{path}"


def code_span(value: object, limit: int = MAX_CODE_SPAN_CHARS) -> str:
    """Markdown code span that holds ``value`` as plain text, whatever it contains."""
    text = _CONTROL_RE.sub(" ", str(value))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    if not text.strip():
        text = "(empty)"
    longest = max((len(run) for run in _FENCE_RE.findall(text)), default=0)
    fence = "`" * (longest + 1)
    if text[0] in "` " or text[-1] in "` ":
        text = f" {text} "  # a code span drops one space on each side
    return f"{fence}{text}{fence}"


def fenced_block(text: str, language: str = "") -> str:
    """Fenced code block whose fence is longer than any backtick run inside."""
    longest = max((len(run) for run in _FENCE_RE.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text.rstrip(chr(10))}\n{fence}"


def _scalar(value: str) -> str:
    """One YAML scalar, plain when the value allows it, quoted otherwise."""
    out = yaml.safe_dump(
        value, allow_unicode=True, width=2**31 - 1, default_flow_style=True
    )
    return out.removesuffix("\n...\n").rstrip("\n")


def render_entry(fields: dict[str, Any]) -> str:
    """The entry as the existing ``templates/*.yml`` files are written."""
    lines = []
    for key in ENTRY_ORDER:
        value = fields.get(key)
        if key == "tags":
            if value:
                lines.append(f"tags: [{', '.join(_scalar(t) for t in value)}]")
        elif value is not None:
            lines.append(f"{key}: {_scalar(value)}")
    return "\n".join(lines) + "\n"


# --- The check ---------------------------------------------------------------


def _spans(values: Sequence[object]) -> str:
    shown = [code_span(v, 40) for v in list(values)[:MAX_LISTED_VALUES]]
    more = ", …" if len(values) > MAX_LISTED_VALUES else ""
    return ", ".join(shown) + more


def split_tags(
    raw: str, taxonomy: Taxonomy, label: str
) -> tuple[str | None, list[str], list[Problem]]:
    """Split the tags field into the kind, the tags and the problems found."""
    items: list[str] = []
    for item in (part.strip().lower() for part in raw.split(",")):
        if item and item not in items:
            items.append(item)

    kinds = [i for i in items if i in taxonomy.kinds]
    tags: list[str] = []
    unknown: list[str] = []
    for item in items:
        if item in taxonomy.kinds:
            continue
        resolved = taxonomy.resolve_tag(item)
        if resolved is None:
            unknown.append(item)
        elif resolved not in tags:
            tags.append(resolved)

    valid_kinds = _spans(list(taxonomy.kinds))
    problems: list[Problem] = []
    if unknown:
        problems.append(
            Problem(
                "tags",
                f"**{label}**: not in the taxonomy: {_spans(unknown)}. "
                f"Valid kinds: {valid_kinds}. "
                f"Valid tags: {_spans(list(taxonomy.active_tags))}.",
            )
        )
    if not kinds:
        problems.append(
            Problem(
                "tags",
                f"**{label}**: add exactly one kind. Valid kinds: {valid_kinds}.",
            )
        )
    elif len(kinds) > 1:
        problems.append(
            Problem(
                "tags",
                f"**{label}**: use exactly one kind, not {len(kinds)} "
                f"({_spans(kinds)}).",
            )
        )
    kind = kinds[0] if len(kinds) == 1 else None
    return kind, tags, problems


def _issue_sentence(issue: Any, schema: dict[str, Any], labels: dict[str, str]) -> str:
    """A fixed sentence for a validator issue (its own message quotes values)."""
    field = issue.field
    label = labels.get(field if field != "kind" else "tags", "The entry")
    props = schema.get("properties", {}).get(field, {})
    code = issue.code
    if code in ("schema:minLength", "schema:maxLength"):
        low, high = props.get("minLength"), props.get("maxLength")
        if low is not None and high is not None:
            return f"**{label}**: must have {low} to {high} characters."
    if code == "schema:pattern" and field == "url":
        return (
            f"**{label}**: must be a public address starting with `https://`, "
            "without spaces."
        )
    if code == "schema:maxItems" and field == "tags":
        limit = props.get("maxItems")
        return f"**{label}**: use at most {limit} tags besides the kind."
    if code == "schema:pattern" and field == "command":
        return f"**{label}**: must be a single line."
    if code == "command":
        return f"**{label}**: must have no control characters."
    if code == "url_credentials":
        return f"**{label}**: must not contain a user name or a password."
    if code == "url":
        return f"**{label}**: is not a valid address."
    if code == "id":
        return "**Name**: gives an id that is not valid; use letters and digits."
    if code in ("kind", "tag"):
        return f"**{label}**: has a kind or tag that is not in the taxonomy."
    return f"**{label}**: does not pass the registry checks."


def count_tree(tree: Sequence[str], truncated: bool) -> str:
    """Summary line like ``tree``'s: dot-entries hidden, only what is shown counted."""
    dirs = files = 0
    for name in tree:
        parts = [p for p in name.split("/") if p]
        if not parts or any(p.startswith(".") for p in parts):
            continue
        if name.endswith("/"):
            dirs += 1
        else:
            files += 1
    line = (
        f"{dirs} {'directory' if dirs == 1 else 'directories'}, "
        f"{files} {'file' if files == 1 else 'files'}"
    )
    return line + (" (listing truncated)" if truncated else "")


DetectFn = Callable[[Entry], dict[str, Any]]


def _detect(entry: Entry) -> dict[str, Any]:
    return detect_entry(entry, Options())


def check_body(body: str, root: Path, detect: DetectFn = _detect) -> Outcome:
    """Build, validate and detect the entry an issue body describes."""
    labels = load_form_labels(root)
    try:
        registry = load_registry(root)
    except (RegistryError, OSError) as exc:
        raise SubmitError("cannot load the registry") from exc
    taxonomy, schema = registry.taxonomy, registry.schema

    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        problem = Problem(
            "",
            f"The issue is longer than {MAX_BODY_BYTES // 1024} KiB: "
            "keep only the form fields.",
        )
        return Outcome(None, None, None, (problem,))

    values = parse_body(body, labels)
    problems: list[Problem] = []
    skip: set[str] = set()  # entry fields whose validator issues are already said
    fields: dict[str, Any] = {}

    def refuse(field: str, text: str, entry_field: str | None = None) -> None:
        problems.append(Problem(field, f"**{labels[field]}**: {text}"))
        skip.add(entry_field or field)

    url = values["url"].strip().removesuffix("/")
    if not url:
        refuse("url", "this field is empty.")
    elif _CONTROL_RE.search(url):
        refuse("url", "contains control characters.")
    else:
        fields["url"] = url

    for field in ("name", "description"):
        value = collapse(values[field])
        if not value:
            refuse(field, "this field is empty.")
        elif _CONTROL_RE.search(value):
            refuse(field, "contains control characters.")
        else:
            fields[field] = value

    kind, tags, tag_problems = split_tags(values["tags"], taxonomy, labels["tags"])
    problems.extend(tag_problems)
    if kind is None:
        skip.add("kind")
    else:
        fields["kind"] = kind
    if tags:
        fields["tags"] = tags

    placement = PLACEMENT_BY_CHOICE.get(values["placement"].strip())
    if placement is None:
        refuse(
            "placement",
            "choose where the specs live: their own repo or the code repo.",
        )
    else:
        fields["placement"] = placement

    command = values["command"].strip()
    if command:
        if _CONTROL_RE.search(command):
            refuse("command", "contains control characters.")
        else:
            fields["command"] = command

    agnostic = values["agnostic"]
    checked = any(
        line.strip().lstrip("-").strip().lower().startswith("[x]")
        for line in agnostic.splitlines()
    )
    if not checked:
        refuse(
            "agnostic",
            "check the box to confirm the template is model- and provider-agnostic.",
        )

    entry_id = slugify(fields["name"]) if "name" in fields else ""
    if "name" in fields and not ID_RE.match(entry_id):
        refuse("name", "gives no id; use letters or digits.", "id")
    elif entry_id:
        if any(e.id == entry_id for e in registry.entries):
            refuse(
                "name",
                f"gives the id {code_span(entry_id)}, which the gallery already "
                "has; choose another name.",
                "id",
            )
    if "url" in fields:
        key = url_key(fields["url"])
        for existing in registry.entries:
            other = existing.fields.get("url")
            if isinstance(other, str) and url_key(other) == key:
                refuse(
                    "url",
                    f"this repository is already listed as {code_span(existing.id)}.",
                )
                break

    yaml_text = render_entry(fields)
    entry = None
    with tempfile.TemporaryDirectory(prefix="gallery-submit-") as tmp:
        file_id = entry_id if ID_RE.match(entry_id) else "entry"
        path = Path(tmp) / "templates" / f"{file_id}.yml"
        path.parent.mkdir()
        path.write_text(yaml_text if fields else "{}\n", encoding="utf-8")
        try:
            entry = load_entry(path, schema)
        except RegistryError as exc:
            raise SubmitError("the entry could not be read back") from exc
        if entry.fields != fields:
            raise SubmitError("the entry was not written as built")
        for issue in entry_issues(entry, Path(path.name), taxonomy, schema):
            if not issue.is_error or issue.field in skip:
                continue
            if issue.code == "id" and entry.id == "entry":
                continue
            problems.append(
                Problem(issue.field, _issue_sentence(issue, schema, labels))
            )

    detected = None
    if not problems and entry is not None:
        detected = detect(entry)
        if detected.get("status") != "ok":
            reason = DETECT_REASONS.get(
                str(detected.get("error")), "the repository could not be inspected"
            )
            problems.append(Problem("url", f"**{labels['url']}**: {reason}."))
            detected = None
        elif detected.get("tools"):
            # Each name comes from detect's fixed TOOL_MARKERS, never from the
            # repository's own strings (ADR-009): the gallery only lists
            # model- and provider-agnostic templates.
            problems.append(
                Problem(
                    "",
                    "The repository holds tool-specific files: "
                    f"{_spans(detected['tools'])}. "
                    "The gallery only lists model- and provider-agnostic templates.",
                )
            )
            detected = None

    ok = not problems
    return Outcome(
        entry_id if ok else None,
        yaml_text if ok else None,
        detected,
        tuple(problems),
    )


# --- The comment -------------------------------------------------------------


def repository() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return repo if _REPOSITORY_RE.match(repo) else DEFAULT_REPOSITORY


def propose_link(entry_id: str, yaml_text: str) -> str:
    """GitHub's "new file" page, prefilled with the entry (id and YAML are ours)."""
    if not ID_RE.match(entry_id):
        raise SubmitError("the id is not valid")
    return (
        f"https://github.com/{repository()}/new/main"
        f"?filename=templates/{entry_id}.yml&value={quote(yaml_text, safe='')}"
    )


def _fit(comment: str) -> str:
    if len(comment) <= MAX_COMMENT_CHARS:
        return comment
    note = "\n\n(The rest of this comment was cut: it was too long.)\n"
    return comment[: MAX_COMMENT_CHARS - len(note)] + note


def render_comment(issue: int, outcome: Outcome) -> str:
    if not outcome.ok:
        lines = [
            MARKER,
            "## The template cannot be proposed yet",
            "",
            *[f"- {problem.text}" for problem in outcome.problems],
            "",
            "Edit this issue to fix them: the check runs again.",
        ]
        return _fit("\n".join(lines) + "\n")

    assert outcome.id is not None and outcome.yaml_text is not None
    detected = outcome.detected or {}
    license_id = detected.get("license")
    license_text = code_span(license_id) if license_id else "none found"
    updated = detected.get("last_update")
    lines = [
        MARKER,
        "## Your template is ready to propose",
        "",
        "The checks pass. This is the entry that will be added:",
        "",
        fenced_block(outcome.yaml_text, "yaml"),
        "",
        "Detected from the repository:",
        "",
        f"- License: {license_text}",
        f"- Last update: {code_span(updated) if updated else 'unknown'}",
        f"- Copier support: {'yes' if detected.get('copier') else 'no'}",
        "- Files: "
        + code_span(
            count_tree(detected.get("tree") or [], bool(detected.get("tree_truncated")))
        ),
        "",
        "To propose it:",
        "",
        f"1. [Open the propose link]({propose_link(outcome.id, outcome.yaml_text)}).",
        "2. Click **Commit changes…**, then **Propose changes**.",
        f"3. In the pull request description, keep or add `Closes #{issue}`.",
        "",
        "A maintainer reviews and merges the pull request.",
    ]
    return _fit("\n".join(lines) + "\n")


# --- Command line ------------------------------------------------------------


def _log(message: str) -> None:
    """Print a message made only of our own words, with no issue content."""
    print(f"submit: {message}", file=sys.stderr, flush=True)


def read_event(path: Path) -> tuple[int, str, str]:
    """``(issue number, body, state)`` of an ``issues`` event file."""
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SubmitError("cannot read the event file") from exc
    issue = event.get("issue") if isinstance(event, dict) else None
    if not isinstance(issue, dict):
        raise SubmitError("the event has no issue")
    number, body, state = issue.get("number"), issue.get("body"), issue.get("state")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise SubmitError("the issue number is not valid")
    if body is not None and not isinstance(body, str):
        raise SubmitError("the issue body is not text")
    if state is not None and not isinstance(state, str):
        raise SubmitError("the issue state is not text")
    return number, body or "", state or "open"


def run_check(event: Path, root: Path, out: Path) -> int:
    number, body, state = read_event(event)
    if state == "closed":
        _log("issue is closed: nothing to do")
        return 0
    outcome = check_body(body, root)
    submission = {
        "issue": number,
        "ok": outcome.ok,
        "id": outcome.id,
        "comment": render_comment(number, outcome),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "submission.json").write_text(
        json.dumps(submission, indent=2) + "\n", encoding="utf-8"
    )
    _log(
        f"issue #{number}: "
        + ("ok" if outcome.ok else f"{len(outcome.problems)} problem(s)")
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a template submission.")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check the issue of an event file")
    check.add_argument("--event", type=Path, required=True, help="event JSON file")
    check.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root with taxonomy.yml, schema/, templates/ and .github/",
    )
    check.add_argument("--out", type=Path, required=True, help="output directory")
    args = parser.parse_args(argv)
    try:
        return run_check(args.event, args.root, args.out)
    except SubmitError as exc:
        _log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
