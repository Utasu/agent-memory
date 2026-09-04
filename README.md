# Agent Memory

A dependency-free shared memory for several agents or processes working on one project.
Source files stay plain Markdown a human can read and edit; `CURRENT.md` and `INDEX.md`
are small generated read paths so an agent loads one short file instead of everything.

## Why it is useful

Two automations working on the same project each rebuild context from scratch, and both
write notes wherever they happen to be running. This gives them one place to look, one
place to write, and makes concurrent writes safe.

## Design choices

- **Every write is locked and atomic.** An exclusive `flock` covers the whole
  read-modify-write, and content lands through a temporary file plus `os.replace`, so
  two agents writing at the same moment cannot lose each other's entry and a reader
  never sees half a file.
- **Secrets are refused, not redacted.** A write containing something shaped like an API
  key, bot token, GitHub token, or private key is rejected before it reaches disk. A
  credential in a file every agent reads is the one mistake you cannot take back.
- **Topics are ranked, not configured.** `context <topic>` scores every section by term
  overlap, so a new subject area works immediately instead of waiting for someone to add
  it to a hand-maintained topic map.
- **The hot path stays small.** `validate` fails when `CURRENT.md` grows past 14 KB or
  when the generated files are older than their sources.
- **Markdown all the way down.** No database, no daemon, no dependencies; `git log` on
  the memory directory is the audit trail.

## Quickstart

```bash
git clone https://github.com/Utasu/agent-memory && cd agent-memory
export AGENT_MEMORY_ROOT=./memory
python3 agent_memory.py init
python3 agent_memory.py changelog "Shipped the nightly exporter; rollback is in decisions.md."
python3 agent_memory.py append projects.md "- Runs at 02:00, writes to /srv/reports." --heading "Nightly exporter"
```

## Demo

Actual output of the commands above:

```console
$ python3 agent_memory.py context exporter
# Context: exporter

## Nightly exporter
_from `projects.md`_

- Runs at 02:00, writes to /srv/reports.

$ python3 agent_memory.py search "nightly exporter"
changelog.md:5: - Shipped the nightly exporter; rollback is in decisions.md.
projects.md:7: ## Nightly exporter
projects.md:9: - Runs at 02:00, writes to /srv/reports.

$ python3 agent_memory.py validate
ok

$ python3 agent_memory.py changelog "deploy token: ghp_0123456789abcdefghijklmnopqrstuvwxyz"
error: refusing to write text that looks like a secret, token, or private key   # exit 2
```

## Commands

| Command | What it does |
|---|---|
| `init` | create starter files and generate the read paths |
| `build` | regenerate `CURRENT.md` and `INDEX.md` |
| `read [file]` | print a memory file (defaults to `CURRENT.md`) |
| `context [topic]` | print the sections most related to a topic |
| `search <query>` | grep the memory, `file:line` output |
| `append <file> <text> [--heading H]` | append under a heading, reusing today's |
| `changelog <text>` | append a dated entry |
| `archive [--keep N]` | move older entries into `archive/changelog-YYYY-MM.md` |
| `validate` | check structure, secrets, and freshness; exit `1` on problems |

`--root` or `AGENT_MEMORY_ROOT` selects the memory directory. Set
`AGENT_MEMORY_UTC_OFFSET` when agents on different hosts must agree on the date.

## Test

```bash
python3 -m unittest discover -s tests -v
```

The suite covers the locking guarantee by running six processes that append
simultaneously and asserting every entry survives.

## Operating rules that make it work

1. Read `CURRENT.md` first; open a source file only when you need the detail.
2. After meaningful work, update the source file **and** add one changelog line.
3. Record decisions and current state, not transcripts.
4. Never record secrets — the tool enforces this, but do not rely on the pattern list.
