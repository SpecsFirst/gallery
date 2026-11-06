"""Detect information about every template of the registry, treating each as hostile.

``python -m gallery.detect [--root DIR] --output FILE [--jobs N]`` fetches every
template into a throw-away *bare* repository and writes ``detected.json``
(see ``architecture.md``, ADR-002 and "Data / interfaces").

Security model, in short:

* Nothing is ever checked out and no code from a template runs: only git
  *object reads* (``ls-tree``, ``cat-file``, ``log``, ``rev-parse``) happen after
  the fetch. No submodule, no LFS, no hook. Lazy fetching of missing blobs is
  disabled, so a blob that was filtered out counts as absent.
* Every git process gets an allowlisted environment (no runner token) and only
  the https protocol is allowed, redirects included.
* Every git command has a timeout, and the repository has a size limit watched
  while git runs.
* The registry is not trusted either: an invalid entry is refused before any
  fetch.
* git's stderr is never published. It only reaches the program's own log, with
  CR, LF and any leading ``::`` removed (the log runs in GitHub Actions).

The command exits 0 even if every template failed; it exits non-zero only when
the registry cannot be loaded or the output cannot be written.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gallery.registry import Entry, load_registry

# --- Limits (defaults; tests make them small through ``Limits``) ------------

GIT_TIMEOUT_SECONDS = 120
MAX_DISK_BYTES = 200 * 1024 * 1024
MAX_TREE_ENTRIES = 500
MAX_PREVIEW_BYTES = 64 * 1024

# Other bounds, not part of the contract but needed so that nothing is unbounded.
MAX_LICENSE_BYTES = 256 * 1024
MAX_LISTING_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 8 * 1024
MAX_LOG_CHARS = 300
BLOB_FETCH_LIMIT = "blob:limit=1m"
DEFAULT_JOBS = 4
POLL_SECONDS = 0.05

# --- Closed list of errors ---------------------------------------------------

ERROR_TIMEOUT = "timeout"
ERROR_TOO_LARGE = "too_large"
ERROR_UNREACHABLE = "unreachable"
ERROR_REF_NOT_FOUND = "ref_not_found"
ERROR_PATH_NOT_FOUND = "path_not_found"
ERROR_INVALID_ENTRY = "invalid_entry"
ERRORS = (
    ERROR_TIMEOUT,
    ERROR_TOO_LARGE,
    ERROR_UNREACHABLE,
    ERROR_REF_NOT_FOUND,
    ERROR_PATH_NOT_FOUND,
    ERROR_INVALID_ENTRY,
)

# --- Tables ------------------------------------------------------------------

# Tool-specific files; a trailing "/" means a directory.
TOOL_MARKERS = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".opencode/",
    ".claude/",
    ".cursor/",
    ".cursorrules",
    ".windsurfrules",
    ".aider.conf.yml",
    ".github/copilot-instructions.md",
)

COPIER_FILES = ("copier.yml", "copier.yaml")
PREVIEW_FILE = "main.md"
REGULAR_MODES = ("100644", "100755")
LICENSE_STEMS = ("license", "licence", "copying")
_LICENSE_NAME_RE = re.compile(r"^(license|licence|copying)(\.[a-z0-9]+)?$", re.I)

# git environment: nothing from the runner's environment except these.
ENV_PASSTHROUGH = ("PATH", "LANG")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FORBIDDEN_REF_CHARS_RE = re.compile(r"[\x00-\x20\x7f:?*\[\\^~]")


# --- Types -------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """Resource limits of one template's detection."""

    git_timeout: float = GIT_TIMEOUT_SECONDS
    max_disk_bytes: int = MAX_DISK_BYTES
    max_tree_entries: int = MAX_TREE_ENTRIES
    max_preview_bytes: int = MAX_PREVIEW_BYTES


def _stderr_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Options:
    """Internal knobs: not reachable from the command line.

    ``extra_protocols`` widens the allowed git protocols (and URL schemes);
    the command line never sets it, tests use it to fetch from local repos.
    """

    limits: Limits = field(default_factory=Limits)
    extra_protocols: tuple[str, ...] = ()
    tmp_root: Path | None = None
    keep_tmp: bool = False
    log: Callable[[str], None] = _stderr_log


class DetectError(Exception):
    """A template's detection failed with one of the closed ``ERRORS``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        assert code in ERRORS
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GitResult:
    returncode: int | None  # None when the output cap killed the process
    stdout: bytes
    stderr: bytes
    cut: bool  # stdout reached its cap


@dataclass(frozen=True)
class TreeItem:
    mode: str
    type: str
    sha: str

    @property
    def is_regular_file(self) -> bool:
        return self.type == "blob" and self.mode in REGULAR_MODES


# --- Logging -----------------------------------------------------------------


def sanitize_log(text: str, limit: int = MAX_LOG_CHARS) -> str:
    """Make ``text`` safe for a GitHub Actions log: one line, no leading ``::``."""
    text = _CONTROL_RE.sub(" ", text).strip()
    while text.startswith("::"):
        text = text[2:].lstrip()
    return text[:limit]


# --- Entry validation --------------------------------------------------------


@dataclass(frozen=True)
class Target:
    url: str
    ref: str | None
    path: str | None


def parse_target(fields: dict[str, Any], extra_protocols: Sequence[str]) -> Target:
    """Check an entry's url, ref and path; raise ``invalid_entry`` if unsafe."""

    def invalid(reason: str) -> DetectError:
        return DetectError(ERROR_INVALID_ENTRY, reason)

    url = fields.get("url")
    if not isinstance(url, str) or _CONTROL_RE.search(url) or " " in url:
        raise invalid("url is not a clean string")
    parts = urlsplit(url)
    allowed = {"https", *extra_protocols}
    if parts.scheme.lower() not in allowed:
        raise invalid("url is not https")
    if url.startswith("-"):
        raise invalid("url starts with '-'")

    ref = fields.get("ref")
    if ref is not None:
        if not isinstance(ref, str) or not ref:
            raise invalid("ref is not a string")
        if ref.startswith("-"):
            raise invalid("ref starts with '-'")
        # Characters git forbids in ref names anyway; ':' would turn the ref
        # into a refspec, '*' into a pattern.
        if _FORBIDDEN_REF_CHARS_RE.search(ref) or ".." in ref:
            raise invalid("ref has forbidden characters")

    path = fields.get("path")
    if path is not None:
        if not isinstance(path, str):
            raise invalid("path is not a string")
        if _CONTROL_RE.search(path):
            raise invalid("path has control characters")
        if path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", path):
            raise invalid("path is absolute")
        segments = [s for s in re.split(r"[/\\]", path) if s not in ("", ".")]
        if ".." in segments:
            raise invalid("path contains '..'")
        path = "/".join(segments) or None

    return Target(url=url, ref=ref, path=path)


# --- Running git -------------------------------------------------------------


def _spawn(cmd: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    """The single place where a process is started (tests spy on it)."""
    return subprocess.Popen(cmd, **kwargs)  # noqa: S603


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


class GitSession:
    """A throw-away bare repository, and the only way to run git against it."""

    def __init__(self, options: Options, url: str) -> None:
        self.options = options
        self.url = url
        self.root = Path(
            tempfile.mkdtemp(prefix="gallery-detect-", dir=options.tmp_root)
        )
        self.gitdir = self.root / "repo.git"
        self.home = self.root / "home"
        self.cwd = self.root / "cwd"
        self.io = self.root / "io"
        for directory in (self.home, self.cwd, self.io):
            directory.mkdir()
        self._counter = 0
        self.env = self._build_env()
        self.config_args = self._build_config_args()

    def close(self) -> None:
        if not self.options.keep_tmp:
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> GitSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _build_env(self) -> dict[str, str]:
        env = {name: os.environ[name] for name in ENV_PASSTHROUGH if name in os.environ}
        env.update(
            {
                "HOME": str(self.home),
                "LC_ALL": "C",  # stable messages, so errors can be classified
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_LITERAL_PATHSPECS": "1",
            }
        )
        return env

    def _build_config_args(self) -> list[str]:
        settings = [
            "protocol.allow=never",
            "protocol.https.allow=always",
            *(f"protocol.{p}.allow=always" for p in self.options.extra_protocols),
            "http.followRedirects=initial",
            "credential.helper=",
            "core.hooksPath=/dev/null",
            "core.fsmonitor=false",
            "gc.auto=0",
            "maintenance.auto=false",
            "fetch.recurseSubmodules=false",
            "submodule.recurse=false",
        ]
        args: list[str] = []
        for setting in settings:
            args += ["-c", setting]
        return args

    def git(
        self,
        *args: str,
        max_stdout: int = MAX_LISTING_BYTES,
        in_repo: bool = True,
    ) -> GitResult:
        """Run one git command with the timeout, disk and output limits."""
        limits = self.options.limits
        self._counter += 1
        out_path = self.io / f"out-{self._counter}"
        err_path = self.io / f"err-{self._counter}"
        prefix = ["git", *self.config_args]
        if in_repo:
            prefix.append(f"--git-dir={self.gitdir}")
        cmd = [*prefix, *args]

        returncode: int | None
        cut = False
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = _spawn(
                cmd,
                cwd=self.cwd,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            deadline = time.monotonic() + limits.git_timeout
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    try:
                        returncode = proc.wait(
                            timeout=max(0.0, min(POLL_SECONDS, remaining))
                        )
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if remaining <= 0:
                        raise DetectError(ERROR_TIMEOUT, "git command timed out")
                    if self.disk_used() > limits.max_disk_bytes:
                        raise DetectError(ERROR_TOO_LARGE, "disk limit reached")
                    if out_path.stat().st_size > max_stdout:
                        returncode, cut = None, True
                        break
            except BaseException:
                _kill(proc)
                raise
            if cut:
                _kill(proc)
        if self.disk_used() > limits.max_disk_bytes:
            raise DetectError(ERROR_TOO_LARGE, "disk limit reached")

        with open(out_path, "rb") as out:
            stdout = out.read(max_stdout)
        with open(err_path, "rb") as err:
            stderr = err.read(MAX_STDERR_BYTES)
        return GitResult(returncode, stdout, stderr, cut)

    def disk_used(self) -> int:
        return _dir_size(self.gitdir) if self.gitdir.exists() else 0

    def note(self, text: bytes) -> str:
        """git's stderr, for the program's own log only (never published)."""
        message = text.decode("utf-8", "replace").replace(self.url, "<url>")
        return sanitize_log(message)


# --- Reading the repository --------------------------------------------------


def parse_ls_tree(data: bytes) -> dict[str, TreeItem]:
    """Parse ``git ls-tree -z`` output into name -> item."""
    items: dict[str, TreeItem] = {}
    for record in data.split(b"\0"):
        meta, sep, name = record.partition(b"\t")
        if not sep:
            continue
        try:
            mode, type_, sha = meta.decode("ascii").split()
        except (UnicodeDecodeError, ValueError):
            continue
        items[name.decode("utf-8", "replace")] = TreeItem(mode, type_, sha)
    return items


def _utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_missing_ref(stderr: bytes) -> bool:
    text = stderr.decode("utf-8", "replace").lower()
    return any(
        marker in text
        for marker in (
            "couldn't find remote ref",
            "not our ref",
            "no such remote ref",
            "invalid refspec",
            "no matching remote head",
        )
    )


def _fetch(session: GitSession, target: Target) -> str:
    """Fetch the ref and return the full sha of the commit to inspect."""
    init = session.git(
        "init", "--bare", "--quiet", "--template=", str(session.gitdir), in_repo=False
    )
    if init.returncode != 0:
        raise DetectError(ERROR_UNREACHABLE, session.note(init.stderr))
    config = session.git("config", "remote.origin.url", target.url)
    if config.returncode != 0:
        raise DetectError(ERROR_UNREACHABLE, session.note(config.stderr))

    if target.path is None:
        shape = ["--depth", "1", f"--filter={BLOB_FETCH_LIMIT}"]
    else:
        # Full history, trees only: last_update is that of the path's last commit.
        shape = ["--filter=blob:none"]
    result = session.git(
        "fetch",
        *shape,
        "--no-tags",
        "--no-recurse-submodules",
        "origin",
        target.ref or "HEAD",
    )
    if result.returncode != 0:
        code = ERROR_REF_NOT_FOUND if _is_missing_ref(result.stderr) else (
            ERROR_UNREACHABLE
        )
        raise DetectError(code, session.note(result.stderr))

    head = session.git("rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}")
    sha = head.stdout.decode("ascii", "replace").strip()
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise DetectError(ERROR_REF_NOT_FOUND, "ref is not a commit")
    return sha


def _resolve_tree(session: GitSession, commit: str, path: str | None) -> str:
    """The tree to inspect: the commit's root, or the one at ``path``."""
    spec = f"{commit}:{path}" if path else f"{commit}^{{tree}}"
    result = session.git("rev-parse", "--verify", "--quiet", spec)
    sha = result.stdout.decode("ascii", "replace").strip()
    if result.returncode != 0 or not sha:
        raise DetectError(ERROR_PATH_NOT_FOUND, "path not in tree")
    kind = session.git("cat-file", "-t", sha)
    if kind.stdout.decode("ascii", "replace").strip() != "tree":
        raise DetectError(ERROR_PATH_NOT_FOUND, "path is not a directory")
    return sha


def _last_update(session: GitSession, commit: str, path: str | None) -> str:
    args = ["log", "-1", "--format=%ct", "--no-renames", commit]
    if path:
        args += ["--", path]
    result = session.git(*args)
    text = result.stdout.decode("ascii", "replace").strip()
    if result.returncode != 0 or not text.isdigit():
        raise DetectError(ERROR_UNREACHABLE, "cannot read commit date")
    return _utc_iso(int(text))


def _list_tree(
    session: GitSession, tree: str, *, recursive: bool
) -> tuple[dict[str, TreeItem], bool]:
    flags = ["-r", "-t"] if recursive else []
    result = session.git("ls-tree", "-z", *flags, tree)
    if result.returncode not in (0, None):
        raise DetectError(ERROR_UNREACHABLE, "cannot list tree")
    return parse_ls_tree(result.stdout), result.cut


def _read_blob(session: GitSession, sha: str, cap: int) -> tuple[bytes, bool] | None:
    """The first ``cap`` bytes of a blob, and whether it was longer.

    ``None`` when the blob is missing (filtered out): never fetched lazily.
    """
    result = session.git("cat-file", "blob", sha, max_stdout=cap)
    if result.cut:
        return result.stdout[:cap], True
    if result.returncode != 0:
        return None
    return result.stdout, False


def _fetch_blobs(session: GitSession, shas: Sequence[str]) -> None:
    """Explicitly fetch the few blobs a path entry needs (history is blobless).

    A refusal from the server just leaves the blobs absent.
    """
    if shas:
        session.git(
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--filter=blob:none",
            "origin",
            *shas,
        )


def find_license_item(items: dict[str, TreeItem]) -> TreeItem | None:
    """The LICENSE / LICENCE / COPYING file among top-level ``items``."""
    candidates = []
    for name, item in items.items():
        match = _LICENSE_NAME_RE.match(name)
        if match and item.type == "blob":
            stem = LICENSE_STEMS.index(match.group(1).lower())
            candidates.append((stem, bool(match.group(2)), name, item))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[:3])[3]


# --- License identification --------------------------------------------------

_GNU_RE = re.compile(
    r"gnu (affero |lesser |library )?general public license"
    r"(?:,? (?:version |v)|"
    r" as published by the free software foundation[;,]? (?:either )?version )"
    r"(\d(?:\.\d)?)"
)
_GNU_IDS = {
    ("", "2"): "GPL-2.0",
    ("", "3"): "GPL-3.0",
    ("lesser ", "3"): "LGPL-3.0",
    ("affero ", "3"): "AGPL-3.0",
}
_MIT_RE = re.compile(
    r"permission is hereby granted, free of charge, to any person obtaining a copy"
)
_ISC_RE = re.compile(
    r"permission to use, copy, modify, (?:and/or|and) distribute this software "
    r"for any purpose with or without fee is hereby granted"
)


def identify_license(raw: str) -> str:
    """An SPDX id from distinctive phrases of a license text, else ``unknown``.

    The GNU family is read last and from its *earliest* title, because other
    licenses quote it (MPL-2.0 names the GPL, GPL-3.0 names the AGPL, LGPL-3.0
    names the GPL) and the GPL, LGPL and AGPL must not be confused.
    """
    text = " ".join(raw.lower().split())

    if "cc0 1.0 universal" in text:
        return "CC0-1.0"
    if "creative commons attribution-sharealike 4.0 international" in text:
        return "CC-BY-SA-4.0"
    if "creative commons attribution 4.0 international" in text:
        return "CC-BY-4.0"
    if "mozilla public license" in text and re.search(
        r"mozilla public license,? (?:version|v\.?) ?2\.0", text
    ):
        return "MPL-2.0"
    if "apache license" in text and re.search(r"version 2\.0", text):
        return "Apache-2.0"
    if "this is free and unencumbered software released into the public domain" in (
        text
    ):
        return "Unlicense"
    if _MIT_RE.search(text) and "without restriction" in text:
        return "MIT"
    if _ISC_RE.search(text):
        return "ISC"
    if "redistribution and use in source and binary forms" in text:
        if "all advertising materials mentioning features" in text:
            return "unknown"  # BSD-4-Clause
        if "endorse or promote products derived from this software" in text:
            return "BSD-3-Clause"
        if "redistributions of source code must retain" in text and (
            "redistributions in binary form must reproduce" in text
        ):
            return "BSD-2-Clause"
        return "unknown"

    match = _GNU_RE.search(text)
    if match:
        flavour, version = match.group(1) or "", match.group(2)
        version = version.removesuffix(".0")
        return _GNU_IDS.get((flavour, version), "unknown")
    return "unknown"


# --- Preview -----------------------------------------------------------------


def decode_preview(data: bytes, truncated: bool, limit: int) -> str:
    """UTF-8 text of at most ``limit`` bytes, cut on a character boundary.

    Invalid UTF-8 is replaced, never fatal.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text = decoder.decode(data[:limit], final=not truncated and len(data) <= limit)
    encoded = text.encode("utf-8")
    if len(encoded) > limit:  # replacement characters are longer than the bytes
        text = encoded[:limit].decode("utf-8", "ignore")
    return text


# --- Detection of one template -----------------------------------------------


def _failed(code: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "error": code,
        "license": None,
        "copier": None,
        "tools": None,
        "last_update": None,
        "commit": None,
        "tree": None,
        "tree_truncated": None,
        "preview": None,
    }


def _inspect(session: GitSession, target: Target) -> dict[str, Any]:
    limits = session.options.limits
    commit = _fetch(session, target)
    tree = _resolve_tree(session, commit, target.path)
    last_update = _last_update(session, commit, target.path)
    items, listing_cut = _list_tree(session, tree, recursive=True)

    top = {name: item for name, item in items.items() if "/" not in name}
    if target.path is None:
        root_top = top
    else:
        root_tree = _resolve_tree(session, commit, None)
        root_top, _ = _list_tree(session, root_tree, recursive=False)

    license_item = find_license_item(top)
    fallback_item = None if license_item else find_license_item(root_top)
    preview_item = top.get(PREVIEW_FILE)
    if preview_item is not None and not preview_item.is_regular_file:
        preview_item = None

    if target.path is not None:
        wanted = [
            item.sha
            for item in (license_item, fallback_item, preview_item)
            if item is not None and item.type == "blob"
        ]
        _fetch_blobs(session, sorted(set(wanted)))

    license_id: str | None = None
    chosen = license_item or fallback_item
    if chosen is not None:
        license_id = "unknown"
        if chosen.is_regular_file:
            blob = _read_blob(session, chosen.sha, MAX_LICENSE_BYTES)
            if blob is not None:
                license_id = identify_license(blob[0].decode("utf-8", "replace"))

    preview: str | None = None
    if preview_item is not None:
        blob = _read_blob(session, preview_item.sha, limits.max_preview_bytes)
        if blob is not None:
            preview = decode_preview(blob[0], blob[1], limits.max_preview_bytes)

    copier = any(
        (item := top.get(name)) is not None and item.is_regular_file
        for name in COPIER_FILES
    )

    tools = sorted(
        marker
        for marker in TOOL_MARKERS
        if _has_marker(items, marker)
    )

    names = sorted(
        name + "/" if item.type == "tree" else name for name, item in items.items()
    )
    truncated = listing_cut or len(names) > limits.max_tree_entries

    return {
        "status": "ok",
        "error": None,
        "license": license_id,
        "copier": copier,
        "tools": tools,
        "last_update": last_update,
        "commit": commit,
        "tree": names[: limits.max_tree_entries],
        "tree_truncated": truncated,
        "preview": preview,
    }


def _has_marker(items: dict[str, TreeItem], marker: str) -> bool:
    if marker.endswith("/"):
        item = items.get(marker[:-1])
        return item is not None and item.type == "tree"
    item = items.get(marker)
    return item is not None and item.type == "blob"


def detect_entry(entry: Entry, options: Options | None = None) -> dict[str, Any]:
    """Detect one template. Never raises: a failure is a ``failed`` result."""
    options = options or Options()
    try:
        target = parse_target(entry.fields, options.extra_protocols)
    except DetectError as exc:
        options.log(f"detect: {sanitize_log(entry.id)}: invalid entry: {exc.detail}")
        return _failed(exc.code)

    try:
        with GitSession(options, target.url) as session:
            return _inspect(session, target)
    except DetectError as exc:
        detail = f" ({sanitize_log(exc.detail)})" if exc.detail else ""
        options.log(f"detect: {sanitize_log(entry.id)}: failed: {exc.code}{detail}")
        return _failed(exc.code)
    except Exception as exc:  # noqa: BLE001 - one template must never kill the run
        kind = type(exc).__name__
        options.log(
            f"detect: {sanitize_log(entry.id)}: unexpected {kind}: "
            f"{sanitize_log(str(exc))}"
        )
        return _failed(ERROR_UNREACHABLE)


def detect_all(
    entries: Sequence[Entry],
    options: Options | None = None,
    jobs: int = DEFAULT_JOBS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Detect every entry (in parallel) and build the ``detected.json`` document."""
    options = options or Options()
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(lambda e: detect_entry(e, options), entries))
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "generated_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "templates": {entry.id: r for entry, r in zip(entries, results, strict=True)},
    }


# --- Command line ------------------------------------------------------------


def write_output(document: dict[str, Any], output: Path) -> None:
    """Write the JSON atomically, keys sorted, ids in order."""
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp = output.with_name(f".{output.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, output)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect information about every template of the registry."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root containing taxonomy.yml, schema/ and templates/",
    )
    parser.add_argument("--output", type=Path, required=True, help="detected.json")
    parser.add_argument(
        "--jobs",
        type=_positive_int,
        default=DEFAULT_JOBS,
        help="templates detected in parallel (default %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        registry = load_registry(args.root)
    except Exception as exc:  # noqa: BLE001 - any failure to load is fatal
        print(
            f"detect: cannot load the registry: {sanitize_log(str(exc))}",
            file=sys.stderr,
        )
        return 2

    document = detect_all(registry.entries, jobs=args.jobs)

    try:
        write_output(document, args.output)
    except OSError as exc:
        print(
            f"detect: cannot write {sanitize_log(str(args.output))}: "
            f"{sanitize_log(str(exc))}",
            file=sys.stderr,
        )
        return 2

    templates = document["templates"].values()
    ok = sum(1 for t in templates if t["status"] == "ok")
    print(f"detect: {ok}/{len(templates)} templates ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
