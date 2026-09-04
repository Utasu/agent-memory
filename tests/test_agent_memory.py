import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import agent_memory


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "memory"
        agent_memory.init(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_init_creates_sources_and_generated_files(self):
        self.assertTrue((self.root / "projects.md").exists())
        self.assertTrue((self.root / "CURRENT.md").exists())
        self.assertTrue((self.root / "INDEX.md").exists())
        self.assertIn("projects", (self.root / "INDEX.md").read_text(encoding="utf-8"))

    def test_secrets_are_refused_before_they_reach_disk(self):
        before = (self.root / "projects.md").read_text(encoding="utf-8")
        for leak in (
            "api_key = AKIA1234567890abcdef",
            "1234567890:AAHqwertyuiopasdfghjklzxcvbnm12345678",
            "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
            "-----BEGIN OPENSSH PRIVATE KEY-----",
        ):
            with self.assertRaises(agent_memory.MemoryError_):
                agent_memory.append(self.root, "projects.md", leak)
        self.assertEqual((self.root / "projects.md").read_text(encoding="utf-8"), before)

    def test_same_day_entries_share_one_heading(self):
        agent_memory.append(self.root, "changelog.md", "- first")
        agent_memory.append(self.root, "changelog.md", "- second")
        text = (self.root / "changelog.md").read_text(encoding="utf-8")
        headings = [title for title, _ in agent_memory.split_sections(text)]
        self.assertEqual(len(headings), 1)
        self.assertIn("- first", text)
        self.assertIn("- second", text)

    def test_writes_outside_the_root_are_refused(self):
        for name in ("../escape.md", "/etc/passwd.md", "sub/dir.md"):
            with self.assertRaises(agent_memory.MemoryError_):
                agent_memory.resolve(self.root, name)

    def test_context_ranks_sections_without_a_topic_map(self):
        agent_memory.append(
            self.root,
            "projects.md",
            "- The billing exporter writes invoices nightly.",
            heading="Billing exporter",
        )
        agent_memory.append(
            self.root, "projects.md", "- Unrelated notes about fonts.", heading="Typography"
        )
        result = agent_memory.context(self.root, "billing exporter")
        headings = [line for line in result.splitlines() if line.startswith("## ")]
        self.assertTrue(headings, "context returned no sections")
        self.assertIn("Billing exporter", headings[0])

    def test_context_reports_a_miss_instead_of_guessing(self):
        self.assertIn("No section mentions", agent_memory.context(self.root, "zzzz-nonexistent"))

    def test_search_returns_file_and_line(self):
        agent_memory.append(self.root, "decisions.md", "- Chose SQLite over Postgres.")
        hits = agent_memory.search(self.root, "sqlite postgres")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].startswith("decisions.md:"))

    def test_generated_files_are_rebuilt_when_a_source_changes(self):
        agent_memory.build(self.root)
        self.assertFalse(agent_memory.generated_stale(self.root))
        # Age the generated files rather than dating a source into the future, so a
        # rebuild can actually win the comparison.
        for name in agent_memory.GENERATED:
            path = self.root / name
            stamp = path.stat().st_mtime - 60
            os.utime(path, (stamp, stamp))
        self.assertTrue(agent_memory.generated_stale(self.root))
        self.assertIn("generated files are stale; run build", agent_memory.validate(self.root))
        agent_memory.ensure_built(self.root)
        self.assertFalse(agent_memory.generated_stale(self.root))

    def test_archive_keeps_the_newest_entries_and_files_the_rest_by_month(self):
        for index in range(5):
            agent_memory.append(
                self.root, "changelog.md", f"- entry {index}", heading=f"2026-0{index + 1}-01"
            )
        moved = agent_memory.archive_changelog(self.root, keep_entries=2)
        self.assertEqual(moved, 3)
        remaining = (self.root / "changelog.md").read_text(encoding="utf-8")
        self.assertIn("- entry 4", remaining)
        self.assertNotIn("- entry 0", remaining)
        archived = (self.root / "archive" / "changelog-2026-01.md").read_text(encoding="utf-8")
        self.assertIn("- entry 0", archived)

    def test_concurrent_appends_do_not_lose_entries(self):
        # Two processes writing at the same moment is the case the lock exists for.
        script = (
            "import sys; sys.path.insert(0, %r);"
            "import agent_memory as m;"
            "m.append(__import__('pathlib').Path(%r), 'changelog.md', '- from ' + sys.argv[1])"
        ) % (str(Path(agent_memory.__file__).parent), str(self.root))
        workers = [
            subprocess.Popen([sys.executable, "-c", script, str(index)]) for index in range(6)
        ]
        for worker in workers:
            worker.wait(timeout=60)
        text = (self.root / "changelog.md").read_text(encoding="utf-8")
        for index in range(6):
            self.assertIn(f"- from {index}", text)

    def test_validate_is_clean_on_a_fresh_memory(self):
        self.assertEqual(agent_memory.validate(self.root), [])


if __name__ == "__main__":
    unittest.main()
