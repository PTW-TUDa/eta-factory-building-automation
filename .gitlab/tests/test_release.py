import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "release.py"
SPEC = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
URL = "https://git.example/group/project"


def description(breaking=False):
    text = "## Summary\nPump and valve updates.\n\n## Release notes\n\n"
    if breaking:
        text += ("### Breaking Changes\n\n- Rename the pump state field.\n"
                 "  Compatibility: PLC consumers using the old field must update.\n"
                 "  Migration: Replace the old field before downloading.\n\n")
    return text + ("### New Features\n\n- Add a pump mode.\n- Add a valve mode.\n\n"
                   "### Bug Fixes\n\n- Correct the pressure unit.\n\n"
                   "### Internal Changes\n\n- Update library references.\n")


class MetadataTests(unittest.TestCase):
    def test_missing_metadata(self):
        with self.assertRaisesRegex(release.ReleaseError, "Release notes"):
            release.parse_changes("## Summary\nSomething changed")

    def test_all_categories_and_multiple_changes(self):
        changes = release.parse_changes(description(breaking=True))
        self.assertEqual([item["category"] for item in changes],
                         ["breaking", "feature", "feature", "bug", "internal"])

    def test_unknown_category(self):
        with self.assertRaisesRegex(release.ReleaseError, "Unknown release-note category"):
            release.parse_changes("## Release notes\n### Other\n- Change")

    def test_breaking_requires_migration(self):
        invalid = description(breaking=True).replace("Migration: Replace the old field before downloading.",
                                                      "Migration: ")
        with self.assertRaisesRegex(release.ReleaseError, "needs migration"):
            release.parse_changes(invalid)

    def test_template_placeholders_fail(self):
        with self.assertRaisesRegex(release.ReleaseError, "template instructions"):
            release.parse_changes((ROOT / "merge_request_templates/Feature.md").read_text())

    def test_reviewed_edits_are_not_overwritten(self):
        draft = f"{release.START_MARKER}\n## v1.1.2\n\nNo changes.\n{release.END_MARKER}"
        inserted, changed = release.insert_notes("Human introduction.", draft)
        self.assertTrue(changed)
        self.assertTrue(inserted.startswith("Human introduction."))
        self.assertEqual(release.insert_notes(inserted, draft), (inserted, False))
        with self.assertRaisesRegex(release.ReleaseError, "edited"):
            release.insert_notes(inserted.replace("No changes.", "Reviewed text."), draft)


class RepositoryTests(unittest.TestCase):
    def test_versions_are_consistent(self):
        release.validate_versions()


ROOT = SCRIPT.parent


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = "a" * 40
        self.one = "b" * 40
        self.two = "c" * 40
        self.direct = "d" * 40
        self.end = self.two
        self.history = [self.root, self.one, self.two]
        self.messages = {
            self.root: "Initial repository",
            self.one: "Merge branch 'feature-21' into 'development'\n\nSee merge request group/project!21\n\n" + description(),
            self.two: "Merge branch 'feature-22' into 'development'\n\nSee merge request group/project!22\n\n" + description(breaking=True),
        }
        self.tags = {"v1.0.0": self.root, "v1.1.0": self.one}
        self.template_present = True
        self.git_patch = patch.object(release, "git", side_effect=self.fake_git)
        self.git_patch.start()
        self.ok_patch = patch.object(release, "git_ok", side_effect=self.fake_ok)
        self.ok_patch.start()

    def tearDown(self):
        self.ok_patch.stop()
        self.git_patch.stop()

    def fake_ok(self, *args):
        if args[:2] == ("cat-file", "-e"):
            return self.template_present
        return True

    def fake_git(self, *args):
        if args[:2] == ("tag", "--list"):
            return "\n".join(self.tags)
        if args[:1] == ("cat-file",):
            return ""
        if args[:1] == ("merge-base",):
            return args[1]
        if args[:1] == ("rev-parse",):
            for tag, sha in self.tags.items():
                if args[1] == tag + "^{commit}":
                    return sha
            return self.end
        if args[:2] == ("rev-list", "--first-parent"):
            revision = args[-1]
            if ".." not in revision:
                return "\n".join(self.history[:self.history.index(self.end) + 1])
            start, end = revision.split("..")
            return "\n".join(self.history[self.history.index(start) + 1:self.history.index(end) + 1])
        if args[:2] == ("rev-list", "--parents"):
            sha = args[-1]
            return sha if sha == self.root else sha + " " + self.root + ("" if sha == self.direct else " " + self.root)
        if args[:2] == ("show", "-s"):
            return self.messages[args[-1]]
        raise AssertionError(args)

    def test_first_release_without_prior_tag(self):
        self.tags = {}
        self.end = self.root
        self.assertEqual(release.previous_release("master"), (None, release.ZERO))
        self.assertEqual(release.entries(release.ZERO, self.root, URL), [])

    def test_release_version_must_advance(self):
        self.tags = {"v" + release.version(): self.root}
        with self.assertRaisesRegex(release.ReleaseError, "newer than"):
            release.require_new_version("master")

    def test_direct_commits_are_omitted(self):
        self.history = [self.root, self.direct, self.one]
        self.end = self.one
        self.messages[self.direct] = "Direct development change"
        keys = [item["key"] for item in release.entries(self.root, self.one, URL)]
        self.assertEqual(keys, ["!21#1", "!21#2", "!21#3", "!21#4"])

    def test_old_unstructured_merge_is_omitted(self):
        self.messages[self.one] = "Old merge without metadata"
        self.template_present = False
        self.assertEqual(release.entries(self.root, self.one, URL), [])

    def test_new_unstructured_merge_fails(self):
        self.messages[self.one] = "New merge without metadata"
        with self.assertRaisesRegex(release.ReleaseError, "no '## Release notes'"):
            release.entries(self.root, self.one, URL)

    def test_duplicate_merge_request_fails(self):
        self.messages[self.two] = self.messages[self.two].replace("!22", "!21")
        with self.assertRaisesRegex(release.ReleaseError, "Duplicate merge request"):
            release.entries(self.root, self.two, URL)

    def test_two_release_cycles(self):
        self.assertEqual(release.previous_release("master"), ("v1.1.0", self.one))
        first = release.entries(self.root, self.one, URL)
        second = release.entries(self.one, self.two, URL)
        self.assertEqual([item["key"] for item in first], [f"!21#{i}" for i in range(1, 5)])
        self.assertEqual([item["key"] for item in second], [f"!22#{i}" for i in range(1, 6)])
        notes = release.draft(self.one, self.two, URL, "1.1.2")
        self.assertIn("### Breaking Changes", notes)
        self.assertIn("[!22#5]", notes)
        self.assertNotIn("!21#1", notes)
        self.assertIn("## v1.1.2", release.validate_notes(notes, self.one, self.two, URL, "1.1.2"))

    def test_duplicate_approved_entry_fails(self):
        notes = release.draft(self.one, self.two, URL, "1.1.2")
        duplicate = "- Again ([!22#1](" + URL + "/-/merge_requests/22))\n"
        notes = notes.replace("### Breaking Changes\n\n", "### Breaking Changes\n\n" + duplicate)
        with self.assertRaisesRegex(release.ReleaseError, "exactly once"):
            release.validate_notes(notes, self.one, self.two, URL, "1.1.2")

    def test_tag_uses_reviewed_wording(self):
        link = URL + "/-/merge_requests/22"
        reviewed = ("## v1.1.2\n\n### Bug Fixes\n\n"
                    f"- Reviewer improved this explanation. ([!22#1]({link}))\n")
        merge_message = ("Merge branch 'development' into 'master'\n\n"
                         + release.START_MARKER + "\n" + reviewed + release.END_MARKER)
        def tag_git(*args):
            if args[:2] == ("cat-file", "-t"):
                return "tag"
            if args[:1] == ("rev-parse",):
                return self.two
            if args[:2] == ("rev-list", "--parents"):
                return f"{self.two} {self.root} {self.one}"
            if args[:3] == ("show", "-s", "--format=%s"):
                return "Merge branch 'development' into 'master'"
            if args[:3] == ("show", "-s", "--format=%B"):
                return merge_message
            raise AssertionError(args)
        item = {"category": "bug", "change": "Original text", "key": "!22#1", "link": link}
        with patch.object(release, "git", side_effect=tag_git), \
             patch.object(release, "validate_versions", return_value="1.1.2"), \
             patch.object(release, "version", return_value="1.1.2"), \
             patch.object(release, "previous_release", return_value=("v1.1.1", self.root)), \
             patch.object(release, "entries", return_value=[item]):
            self.assertEqual(release.tag_check("v1.1.2", "origin/master", URL), reviewed)


class PublicSnapshotTests(unittest.TestCase):
    """Run the snapshot logic against a throwaway repository, never the real one."""

    IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
                "GIT_AUTHOR_DATE": "1700000000 +0100", "GIT_COMMITTER_DATE": "1700000000 +0100"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.root_patch = patch.object(release, "ROOT", self.repo)
        self.root_patch.start()
        self.run_git("init", "--quiet")
        self.write("README.rst", "public")
        self.write("ETA_Fabrik_C6030/CP_5C7432/POUs/Beleuchtung Außen/FB.TcPOU", "code")
        self.write("ETA_Fabrik_C6030/CP_5C7432/GVLs/GlobalVariables.TcGVL", "secret")
        self.write("ETA_Fabrik_C6030/CP_5C7432/GVLs/Lighting (old)/Con_Bel.TcGVL", "secret")
        self.commit("Internal release")
        self.run_git("tag", "-a", "v9.9.9", "-m", "Release v9.9.9")
        self.base = release.git("commit-tree", release.git("mktree", env=self.IDENTITY),
                                "-m", "Old public state", env=self.IDENTITY)

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def run_git(self, *args):
        return release.git(*args, env=self.IDENTITY)

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.run_git("add", "-A")
        self.run_git("commit", "--quiet", "-m", message)

    def test_snapshot_drops_gvls_and_internal_history(self):
        commit = release.public_commit("v9.9.9", self.base)
        files = release.git("ls-tree", "-r", "-z", "--name-only", commit).split("\0")
        self.assertIn("README.rst", files)
        self.assertIn("ETA_Fabrik_C6030/CP_5C7432/POUs/Beleuchtung Außen/FB.TcPOU", files)
        self.assertEqual(release.private_files(commit), [])
        self.assertEqual(release.git("rev-list", "--parents", "-n", "1", commit).split()[1:],
                         [self.base])

    def test_snapshot_is_reproducible(self):
        self.assertEqual(release.public_commit("v9.9.9", self.base),
                         release.public_commit("v9.9.9", self.base))

    def test_first_public_commit_has_no_parent(self):
        commit = release.public_commit("v9.9.9", release.ZERO)
        self.assertEqual(release.git("rev-list", "--parents", "-n", "1", commit), commit)

    def test_gvl_outside_excluded_folder_fails(self):
        self.write("ETA_Fabrik_C6030/CP_5C7432/Other/New.TcGVL", "secret")
        self.commit("Add GVL elsewhere")
        with self.assertRaisesRegex(release.ReleaseError, "New.TcGVL"):
            release.public_tree("HEAD")


if __name__ == "__main__":
    unittest.main()
