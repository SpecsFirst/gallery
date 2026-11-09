"""Render detect's ``tree`` list as ``LC_ALL=C tree`` text (no ``-a``).

Pure: no I/O, no logging, deterministic. Hostile names cannot break the
output: control characters are replaced with ``?`` so no line is split, and
the caller is responsible for escaping (Jinja autoescape).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# U+0000-U+001F and U+007F, the same control characters detect guards against.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_LAST = "└── "
_NOT_LAST = "├── "
_LAST_PREFIX = "    "
_NOT_LAST_PREFIX = "│   "


@dataclass(frozen=True)
class RenderedTree:
    """The tree text and its counts; ``summary`` is derived from the counts."""

    text: str
    directories: int
    files: int

    @property
    def summary(self) -> str:
        dir_word = "directory" if self.directories == 1 else "directories"
        file_word = "file" if self.files == 1 else "files"
        return f"{self.directories} {dir_word}, {self.files} {file_word}"


@dataclass
class _Node:
    name: str
    is_dir: bool = False
    children: dict[str, "_Node"] = field(default_factory=dict)

    def is_directory(self) -> bool:
        return self.is_dir or bool(self.children)


def _clean(name: str) -> str:
    return _CONTROL_RE.sub("?", name)


def render_tree(paths: Sequence[str]) -> RenderedTree:
    """Turn detect's ``tree`` list into the text of ``LC_ALL=C tree``.

    ``paths`` is the list from ``detected.json`` (directories end in ``/`` and
    precede their content, but may be incomplete in a truncated listing).
    Hidden entries (a segment whose own name starts with ``.``) are dropped
    with their whole subtree. The result is deterministic.
    """
    root = _Node(name="", is_dir=True)
    for raw in paths:
        is_dir = raw.endswith("/")
        stem = raw[:-1] if is_dir else raw
        segments = [s for s in stem.split("/") if s not in ("", ".", "..")]
        if not segments or any(s.startswith(".") for s in segments):
            continue

        node = root
        for index, segment in enumerate(segments):
            last = index == len(segments) - 1
            child = node.children.get(segment)
            if child is None:
                child = _Node(name=segment, is_dir=last and is_dir)
                node.children[segment] = child
            elif last:
                child.is_dir = child.is_dir or is_dir
            node = child

    lines: list[str] = ["."]
    counters = _Counters()
    _walk(root, (), lines, counters)
    return RenderedTree(
        text="\n".join(lines),
        directories=counters.directories,
        files=counters.files,
    )


@dataclass
class _Counters:
    directories: int = 0
    files: int = 0


def _walk(
    node: _Node, prefix_parts: tuple[str, ...], lines: list[str], counters: _Counters
) -> None:
    children = sorted(node.children.values(), key=lambda n: n.name.encode("utf-8"))
    for index, child in enumerate(children):
        last = index == len(children) - 1
        prefix = "".join(prefix_parts)
        connector = _LAST if last else _NOT_LAST
        lines.append(prefix + connector + _clean(child.name))
        if child.is_directory():
            counters.directories += 1
        else:
            counters.files += 1
        if child.children:
            part = _LAST_PREFIX if last else _NOT_LAST_PREFIX
            _walk(child, (*prefix_parts, part), lines, counters)
