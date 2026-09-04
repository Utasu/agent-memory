#!/usr/bin/env python3
"""Shared Markdown memory for several agents or processes working on one project.

Source files stay plain Markdown that a human can read and edit. `INDEX.md` and
`CURRENT.md` are small generated read paths so an agent can load one short file
instead of everything. Every write takes an exclusive lock and lands atomically,
so two agents writing at the same moment cannot silently overwrite each other.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import os
import re
import sys
from pathlib import Path
from typing import Iterator

GENERATED = ("CURRENT.md", "INDEX.md")
CHANGELOG = "changelog.md"
LOCK_NAME = ".agent-memory.lock"
ARCHIVE_DIR = "archive"
CURRENT_SIZE_LIMIT = 14_000
DEFAULT_KEEP_ENTRIES = 120

# Recording a credential in a file every agent reads is the one unrecoverable
# mistake here, so writes containing these shapes are refused outright.
SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|passwd|cookie|private[_-]?key|oauth|"
        r"refresh[_-]?token)\s*[:=]\s*\S+"
    ),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),          # Telegram-style bot tokens
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),         # GitHub tokens
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |)PRIVATE KEY-----"),
)

STARTER_FILES = {
    "README.md": (
        "# Shared memory\n\n"
        "## Rules for agents\n\n"
        "- Read `CURRENT.md` first; open a source file only when you need the detail.\n"
        "- After meaningful work, update the source file and add a `changelog` entry.\n"
        "- Never record secrets, tokens, credentials, or raw transcripts here.\n"
    ),
    "projects.md": "# Projects\n\n## Example project\n\n- What it is, where it lives, how it runs.\n",
    "decisions.md": "# Decisions\n\n## Example decision\n\n- What was decided and why.\n",
    "active-tasks.md": "# Active tasks\n\n## Waiting / next actions\n\n- Nothing yet.\n",
    CHANGELOG: "# Changelog\n",
}


class MemoryError_(RuntimeError):
    """Raised for every condition the caller is expected to handle."""


def today(root: Path) -> str:
    return now(root).strftime("%Y-%m-%d")


def now(root: Path) -> dt.datetime:
    """Local wall-clock time, or a fixed offset when the caller pins one.

    Agents on different hosts otherwise date the same entry differently.
    """
    offset = os.environ.get("AGENT_MEMORY_UTC_OFFSET")
    if offset:
        try:
            return dt.datetime.now(dt.timezone(dt.timedelta(hours=float(offset))))
        except ValueError as error:
            raise MemoryError_(f"invalid AGENT_MEMORY_UTC_OFFSET: {offset}") from error
    return dt.datetime.now().astimezone()


def check_secret(text: str) -> None:
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise MemoryError_(
                "refusing to write text that looks like a secret, token, or private key"
            )


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    """Hold an exclusive cross-process lock for the whole read-modify-write."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so readers never see half a file."""
    check_secret(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def resolve(root: Path, name: str) -> Path:
    """Map a file name onto the memory root, refusing anything outside it."""
    if not name.endswith(".md"):
        name = f"{name}.md"
    path = (root / name).resolve()
    if path.parent != root.resolve():
        raise MemoryError_("refusing a path outside the memory root")
    return path


def source_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.glob("*.md")
        if path.name not in GENERATED and not path.name.startswith(".")
    )


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split Markdown into (H2 heading, body) pairs."""
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.M))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.end() : end].strip()))
    return sections


def summarize(text: str, max_items: int = 4) -> str:
    """Keep the first few bullet lines of each section: enough to decide where to look."""
    chunks = []
    for title, body in split_sections(text):
        picked = []
        for line in body.splitlines():
            if not line.strip() or line.startswith("|") or line.startswith("#"):
                continue
            picked.append(line)
            if len(picked) >= max_items:
                break
        chunks.append(f"### {title}\n" + ("\n".join(picked) if picked else "_See the source file._"))
    return "\n\n".join(chunks)


def build_current(root: Path) -> str:
    stamp = now(root).strftime("%Y-%m-%d %H:%M %Z").strip()
    parts = [
        "# Current memory — hot path",
        "",
        f"Generated: {stamp}. Source files remain authoritative; this file is regenerated.",
        "",
        "## How to use",
        "",
        "- Read this file first.",
        f"- For detail run `{Path(sys.argv[0]).name} context <topic>` or open the named source file.",
        "- Do not load the changelog unless you need history.",
        "",
    ]
    for path in source_files(root):
        if path.name == CHANGELOG:
            continue
        body = summarize(path.read_text(encoding="utf-8", errors="ignore"))
        if body:
            parts.extend([f"## {path.stem}", "", body, ""])
    return "\n".join(parts).rstrip() + "\n"


def build_index(root: Path) -> str:
    parts = ["# Memory index", "", "| File | Sections |", "|---|---|"]
    for path in source_files(root):
        titles = [title for title, _ in split_sections(path.read_text(encoding="utf-8", errors="ignore"))]
        listed = ", ".join(titles[:8]) + (" …" if len(titles) > 8 else "")
        parts.append(f"| `{path.name}` | {listed or '_no sections_'} |")
    return "\n".join(parts).rstrip() + "\n"


def generated_stale(root: Path) -> bool:
    generated = [root / name for name in GENERATED]
    if any(not path.exists() for path in generated):
        return True
    newest = max((path.stat().st_mtime_ns for path in source_files(root)), default=0)
    return any(path.stat().st_mtime_ns < newest for path in generated)


def build(root: Path) -> None:
    with locked(root):
        atomic_write(root / "CURRENT.md", build_current(root))
        atomic_write(root / "INDEX.md", build_index(root))


def ensure_built(root: Path) -> None:
    if generated_stale(root):
        build(root)


def append(root: Path, name: str, text: str, heading: str | None = None) -> Path:
    """Append under a heading, reusing the newest one when it already matches."""
    check_secret(text)
    path = resolve(root, name)
    heading = heading or today(root)
    with locked(root):
        old = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else f"# {path.stem}\n"
        sections = split_sections(old)
        if sections and sections[-1][0] == heading:
            new = f"{old.rstrip()}\n{text.rstrip()}\n"
        else:
            new = f"{old.rstrip()}\n\n## {heading}\n\n{text.rstrip()}\n"
        atomic_write(path, new)
    build(root)
    return path


def archive_changelog(root: Path, keep_entries: int = DEFAULT_KEEP_ENTRIES) -> int:
    """Move all but the newest entries into per-month archive files."""
    path = resolve(root, CHANGELOG)
    if not path.exists():
        return 0
    with locked(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        entries: list[tuple[str, str]] = []
        for heading, body in split_sections(text):
            for chunk in re.split(r"(?m)^(?=- )", body):
                chunk = chunk.strip()
                if chunk.startswith("- "):
                    entries.append((heading, chunk))
        if len(entries) <= keep_entries:
            return 0
        old, keep = entries[:-keep_entries], entries[-keep_entries:]
        grouped: dict[str, list[str]] = {}
        for date, entry in old:
            grouped.setdefault(date[:7], []).append(f"## {date}\n\n{entry}")
        for month, blocks in grouped.items():
            archive = root / ARCHIVE_DIR / f"changelog-{month}.md"
            prior = (
                archive.read_text(encoding="utf-8", errors="ignore")
                if archive.exists()
                else f"# Changelog archive {month}\n"
            )
            atomic_write(archive, f"{prior.rstrip()}\n\n" + "\n\n".join(blocks) + "\n")
        rebuilt, last = ["# Changelog"], None
        for date, entry in keep:
            if date != last:
                rebuilt.extend(["", f"## {date}", ""])
                last = date
            rebuilt.append(entry)
        atomic_write(path, "\n".join(rebuilt).rstrip() + "\n")
        return len(old)


def terms_of(query: str) -> list[str]:
    return [word.casefold() for word in re.findall(r"[\w\-一-鿿]+", query) if len(word) > 1]


def search(root: Path, query: str, limit: int = 30) -> list[str]:
    wanted = terms_of(query)
    if not wanted:
        return []
    hits = []
    for path in sorted(root.rglob("*.md")):
        if path.name in GENERATED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            folded = line.casefold()
            if all(term in folded for term in wanted):
                hits.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
                if len(hits) >= limit:
                    return hits
    return hits


def context(root: Path, topic: str, limit: int = 6) -> str:
    """Return the sections most related to a topic.

    Sections are ranked by how many of the topic's words they contain, so a new
    subject area works immediately instead of waiting for someone to add it to a
    hand-maintained topic map.
    """
    ensure_built(root)
    if not topic:
        return (root / "CURRENT.md").read_text(encoding="utf-8", errors="ignore")
    wanted = terms_of(topic)
    scored: list[tuple[int, str, str, str]] = []
    for path in source_files(root):
        if path.name == CHANGELOG:
            continue
        for title, body in split_sections(path.read_text(encoding="utf-8", errors="ignore")):
            blob = f"{title}\n{body}".casefold()
            score = sum(blob.count(term) for term in wanted)
            score += sum(5 for term in wanted if term in title.casefold())
            if score:
                scored.append((score, path.name, title, body))
    if not scored:
        return f"No section mentions {topic!r}. Try `search` or read CURRENT.md."
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    parts = [f"# Context: {topic}", ""]
    for _, filename, title, body in scored[:limit]:
        parts.extend([f"## {title}  \n_from `{filename}`_", "", body, ""])
    return "\n".join(parts).rstrip() + "\n"


def validate(root: Path) -> list[str]:
    problems = []
    files = source_files(root)
    if not files:
        problems.append("no source files found")
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            problems.append(f"secret-like content: {path.name}")
    if generated_stale(root):
        problems.append("generated files are stale; run build")
    current = root / "CURRENT.md"
    if current.exists() and current.stat().st_size > CURRENT_SIZE_LIMIT:
        problems.append(f"CURRENT.md is {current.stat().st_size} bytes; keep the hot path small")
    return problems


def init(root: Path) -> list[str]:
    created = []
    with locked(root):
        for name, body in STARTER_FILES.items():
            path = root / name
            if not path.exists():
                atomic_write(path, body)
                created.append(name)
    build(root)
    return created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("AGENT_MEMORY_ROOT", "./memory")),
        help="memory directory (env: AGENT_MEMORY_ROOT)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create starter files")
    sub.add_parser("build", help="regenerate CURRENT.md and INDEX.md")
    sub.add_parser("list", help="list source files")
    sub.add_parser("validate", help="check structure, secrets, and freshness")
    read = sub.add_parser("read", help="print a memory file")
    read.add_argument("file", nargs="?", default="CURRENT.md")
    ctx = sub.add_parser("context", help="print the sections related to a topic")
    ctx.add_argument("topic", nargs="?", default="")
    ctx.add_argument("--limit", type=int, default=6)
    find = sub.add_parser("search", help="grep the memory with file:line output")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=30)
    add = sub.add_parser("append", help="append text to a file under a heading")
    add.add_argument("file")
    add.add_argument("text")
    add.add_argument("--heading")
    log = sub.add_parser("changelog", help="append a dated changelog entry")
    log.add_argument("text")
    arch = sub.add_parser("archive", help="move old changelog entries into archive/")
    arch.add_argument("--keep", type=int, default=DEFAULT_KEEP_ENTRIES)
    args = parser.parse_args(argv)
    root = args.root

    if args.command == "init":
        created = init(root)
        print(f"created: {', '.join(created)}" if created else "already initialized")
        return 0
    if not root.exists():
        raise MemoryError_(f"memory root does not exist: {root} (run `init` first)")

    if args.command == "build":
        build(root)
        print("regenerated CURRENT.md and INDEX.md")
    elif args.command == "list":
        for path in source_files(root):
            print(path.name)
    elif args.command == "read":
        ensure_built(root)
        print(resolve(root, args.file).read_text(encoding="utf-8", errors="ignore"), end="")
    elif args.command == "context":
        print(context(root, args.topic, args.limit), end="")
    elif args.command == "search":
        hits = search(root, args.query, args.limit)
        print("\n".join(hits) if hits else "no matches")
    elif args.command == "append":
        print(f"appended to {append(root, args.file, args.text, args.heading).name}")
    elif args.command == "changelog":
        entry = args.text if args.text.lstrip().startswith("- ") else f"- {args.text}"
        print(f"appended to {append(root, CHANGELOG, entry).name}")
    elif args.command == "archive":
        moved = archive_changelog(root, args.keep)
        print(f"archived {moved} entr{'y' if moved == 1 else 'ies'}")
    elif args.command == "validate":
        problems = validate(root)
        print("\n".join(problems) if problems else "ok")
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MemoryError_, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
