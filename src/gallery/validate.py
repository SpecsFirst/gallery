"""Validate the gallery registry and every template entry.

``python -m gallery.validate [--root DIR]`` checks ``templates/`` and
``taxonomy.yml``, reports every error (not only the first), and exits 0 when
valid (deprecated-tag warnings do not fail), 1 otherwise. Inside GitHub Actions
(``GITHUB_ACTIONS=true``) errors and warnings are also emitted as
``::error``/``::warning`` annotations, with values escaped so an entry cannot
inject workflow commands.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from gallery.registry import RegistryError, load_entry, load_schema, load_taxonomy

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Control and bidirectional-override characters: a command is pasted into terminals,
# and bidi controls can make it read differently from what it runs.
_COMMAND_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


@dataclass(frozen=True)
class Issue:
    """A single validation problem, attached to a file."""

    file: Path
    message: str
    level: str = "error"
    field: str = ""
    code: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == "error"


def _escape_data(value: str) -> str:
    """Escape a value so it cannot inject GitHub workflow commands (data)."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    """Escape a value for an annotation property (also ':' and ',')."""
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def _plain(value: str) -> str:
    """Neutralize CR/LF so a printed issue always stays on one line."""
    return value.replace("\r", "\\r").replace("\n", "\\n")


def _print_issue(issue: Issue, in_actions: bool) -> None:
    print(f"{_plain(str(issue.file))}: {issue.level}: {_plain(issue.message)}")
    if in_actions:
        print(
            f"::{issue.level} file={_escape_property(str(issue.file))}::"
            f"{_escape_data(issue.message)}"
        )


def entry_issues(entry, path: Path, taxonomy, schema) -> list[Issue]:
    """Every problem of one entry (schema and registry rules), warnings included.

    ``field`` is the entry field an issue is about (``""`` when it concerns the
    whole entry) and ``code`` a short stable name of the rule, so callers can
    word the problem without reusing ``message``, which quotes entry values.
    """
    issues: list[Issue] = []
    _check_entry(entry, path, taxonomy, schema, issues)
    return issues


def _check_entry(entry, path: Path, taxonomy, schema, issues: list[Issue]) -> None:
    data = entry.fields

    validator = Draft202012Validator(schema)
    for error in sorted(validator.iter_errors(data), key=str):
        field = "/".join(str(p) for p in error.absolute_path) or "<root>"
        top = str(error.absolute_path[0]) if error.absolute_path else ""
        if error.validator == "required":
            # one error per missing property; its name is only in the message
            for name in error.validator_value:
                if error.message == f"{name!r} is a required property":
                    top = str(name)
        issues.append(
            Issue(
                file=path,
                message=f"schema: {field}: {error.message}",
                field=top,
                code=f"schema:{error.validator}",
            )
        )

    if not ID_RE.match(entry.id):
        issues.append(
            Issue(
                file=path,
                message=f"id {entry.id!r} does not match ^[a-z0-9]+(-[a-z0-9]+)*$",
                field="id",
                code="id",
            )
        )

    kind = data.get("kind")
    if isinstance(kind, str) and kind not in taxonomy.kinds:
        issues.append(
            Issue(
                file=path,
                message=f"kind {kind!r} is not in the taxonomy",
                field="kind",
                code="kind",
            )
        )

    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        tax_tag = taxonomy.tags.get(tag)
        if tax_tag is None:
            issues.append(
                Issue(
                    file=path,
                    message=f"tag {tag!r} is not in the taxonomy",
                    field="tags",
                    code="tag",
                )
            )
        elif tax_tag.deprecated:
            replacement = tax_tag.replaced_by
            suffix = f", replaced by {replacement!r}" if replacement else ""
            issues.append(
                Issue(
                    file=path,
                    level="warning",
                    message=f"tag {tag!r} is deprecated{suffix}",
                    field="tags",
                    code="tag_deprecated",
                )
            )

    url = data.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
        except ValueError:
            issues.append(
                Issue(
                    file=path,
                    message=f"url is not a valid URL: {url!r}",
                    field="url",
                    code="url",
                )
            )
        else:
            if parts.username is not None or parts.password is not None:
                issues.append(
                    Issue(
                        file=path,
                        message="url must not contain credentials",
                        field="url",
                        code="url_credentials",
                    )
                )

    entry_path = data.get("path")
    if isinstance(entry_path, str):
        if os.path.isabs(entry_path) or ".." in Path(entry_path).parts:
            issues.append(
                Issue(
                    file=path,
                    message="path must be relative and must not contain '..'",
                    field="path",
                    code="path",
                )
            )

    ref = data.get("ref")
    if isinstance(ref, str) and ref.startswith("-"):
        issues.append(
            Issue(
                file=path,
                message="ref must not start with '-'",
                field="ref",
                code="ref",
            )
        )

    command = data.get("command")
    if isinstance(command, str) and _COMMAND_CONTROL_RE.search(command):
        issues.append(
            Issue(
                file=path,
                message="command must not contain control or bidi characters",
                field="command",
                code="command",
            )
        )


def _check_taxonomy(root: Path, issues: list[Issue]) -> Any:
    """Load the taxonomy and check its semantics. Returns the Taxonomy or None."""
    path = root / "taxonomy.yml"
    rel = Path("taxonomy.yml")
    try:
        taxonomy = load_taxonomy(path)
    except RegistryError as exc:
        issues.append(Issue(file=rel, message=str(exc)))
        return None

    for group in taxonomy.groups:
        for tag in group.tags:
            if tag not in taxonomy.tags:
                issues.append(
                    Issue(
                        file=rel,
                        message=f"group {group.name!r} names unknown tag {tag!r}",
                    )
                )

    for name, tag in taxonomy.tags.items():
        if tag.deprecated and tag.replaced_by is None:
            issues.append(
                Issue(
                    file=rel,
                    message=f"deprecated tag {name!r} has no replaced_by",
                )
            )
        if tag.replaced_by is not None and tag.replaced_by not in taxonomy.tags:
            issues.append(
                Issue(
                    file=rel,
                    message=(
                        f"replaced_by of {name!r} names unknown tag "
                        f"{tag.replaced_by!r}"
                    ),
                )
            )

    for name, tag in taxonomy.tags.items():
        if not tag.deprecated:
            continue
        seen: set[str] = set()
        current = tag.replaced_by
        while current is not None and current in taxonomy.tags:
            if current in seen:
                issues.append(
                    Issue(
                        file=rel,
                        message=f"replaced_by cycle involving tag {current!r}",
                    )
                )
                break
            seen.add(current)
            nxt = taxonomy.tags[current]
            if not nxt.deprecated:
                break
            current = nxt.replaced_by

    return taxonomy


def validate(root: Path, in_actions: bool = False) -> int:
    """Validate the registry under ``root`` and print issues. Returns exit code."""
    issues: list[Issue] = []

    taxonomy = _check_taxonomy(root, issues)

    template_dir = root / "templates"
    schema_path = root / "schema" / "template.schema.json"
    schema_rel = Path("schema") / "template.schema.json"

    try:
        schema = load_schema(schema_path)
    except RegistryError as exc:
        issues.append(Issue(file=schema_rel, message=str(exc)))
        schema = None

    if template_dir.exists():
        for path in sorted(template_dir.iterdir()):
            rel = path.relative_to(root)
            if path.is_symlink():
                issues.append(
                    Issue(file=rel, message="symlinks are not allowed in templates/")
                )
                continue
            if path.is_dir():
                issues.append(
                    Issue(file=rel, message="subfolders are not allowed in templates/")
                )
                continue
            if path.suffix != ".yml":
                issues.append(
                    Issue(
                        file=rel,
                        message="templates/ files must use the .yml extension",
                    )
                )
                continue
            try:
                entry = load_entry(path, schema)
            except RegistryError as exc:
                issues.append(Issue(file=rel, message=str(exc)))
                continue
            if taxonomy is not None and schema is not None:
                _check_entry(entry, rel, taxonomy, schema, issues)

    for issue in issues:
        _print_issue(issue, in_actions)

    return 1 if any(i.is_error for i in issues) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the gallery registry.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root containing taxonomy.yml, schema/ and templates/",
    )
    args = parser.parse_args(argv)

    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    return validate(args.root, in_actions=in_actions)


if __name__ == "__main__":
    sys.exit(main())
