"""In-process git-sync: pull-before-read, commit only this turn's delta."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sync


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stamp(path: Path, ns: int) -> None:
    """Pin mtime (ns) so signature tests do not depend on fs timestamp resolution."""
    os.utime(path, ns=(ns, ns))


def _init_repo(path: Path, *, name="dev", email="dev@example.com") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd=path)
    _git(["config", "user.name", name], cwd=path)
    _git(["config", "user.email", email], cwd=path)
    (path / "README").write_text("hello\n")
    _git(["add", "README"], cwd=path)
    _git(["commit", "-m", "init"], cwd=path)
    return path


class ExtractPaths(unittest.TestCase):
    def test_read_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("x")
            paths = sync.extract_paths("read_file", {"path": str(target)})
            self.assertEqual(paths, [str(target.resolve())])

    def test_patch_header_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mod.py"
            target.write_text("x")
            patch = f"*** Update File: {target}\n@@\n-x\n+y\n"
            paths = sync.extract_paths("patch", {"patch": patch})
            self.assertEqual(paths, [str(target.resolve())])

    def test_ignores_urls(self):
        paths = sync.extract_paths(
            "vision_analyze", {"image_url": "https://example.com/x.png"}
        )
        self.assertEqual(paths, [])


class SkipRules(unittest.TestCase):
    def test_nix_and_tmp_skipped(self):
        self.assertTrue(sync._skipped("/nix/store/abc"))
        self.assertTrue(sync._skipped("/tmp/foo"))

    def test_hermes_home_skipped_except_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            projects = home / "projects"
            projects.mkdir(parents=True)
            os.environ["HERMES_HOME"] = str(home)
            os.environ["PROJECTS_ROOT"] = str(projects)
            try:
                self.assertTrue(sync._skipped(str(home / "memories" / "MEMORY.md")))
                self.assertFalse(sync._skipped(str(projects / "foo.md")))
            finally:
                os.environ.pop("HERMES_HOME", None)
                os.environ.pop("PROJECTS_ROOT", None)

    def test_plugin_install_tree_is_never_synced(self):
        """A catalog install is a pinned git checkout, not the user's work."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            (home / "plugins" / "git-hook").mkdir(parents=True)
            os.environ["HERMES_HOME"] = str(home)
            try:
                self.assertTrue(
                    sync._skipped(str(home / "plugins" / "git-hook" / "sync.py"))
                )
                self.assertFalse(sync._skipped(str(home / "notes.md")))
            finally:
                os.environ.pop("HERMES_HOME", None)


class PushKnob(unittest.TestCase):
    def test_push_off_keeps_commit_local(self):
        """GIT_HOOK_PUSH=0 must return before any git push runs (push stays on
        by default; the knob is the opt-out)."""
        os.environ["GIT_HOOK_PUSH"] = "0"
        try:
            self.assertEqual(
                sync._push("/nonexistent-repo-path", "test", "abc123"),
                "committed abc123",
            )
        finally:
            os.environ.pop("GIT_HOOK_PUSH", None)


class GitSync(unittest.TestCase):
    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        os.environ["PROJECTS_ROOT"] = str(self.root)
        os.environ.pop("GIT_HOOK_COMMIT", None)
        os.environ.pop("GIT_HOOK_PUSH", None)
        os.environ.pop("GIT_HOOK_COMMIT_MSG", None)

    def tearDown(self):
        sync.reset_state()
        os.environ.pop("PROJECTS_ROOT", None)
        os.environ.pop("HERMES_HOME", None)
        self.td.cleanup()

    def _clone_pair(self):
        origin = _init_repo(self.root / "origin")
        # make origin a bare remote by cloning
        bare = self.root / "origin.git"
        _git(["clone", "--bare", str(origin), str(bare)], cwd=self.root)
        work = self.root / "work"
        _git(["clone", str(bare), str(work)], cwd=self.root)
        _git(["config", "user.name", "dev"], cwd=work)
        _git(["config", "user.email", "dev@example.com"], cwd=work)
        return bare, work

    def test_pull_ff_only_when_clean(self):
        bare, work = self._clone_pair()
        other = self.root / "other"
        _git(["clone", str(bare), str(other)], cwd=self.root)
        _git(["config", "user.name", "dev"], cwd=other)
        _git(["config", "user.email", "dev@example.com"], cwd=other)
        (other / "new.txt").write_text("from other\n")
        _git(["add", "new.txt"], cwd=other)
        _git(["commit", "-m", "add new"], cwd=other)
        _git(["push", "origin", "HEAD"], cwd=other)

        status = sync.pull_if_clean(str(work))
        self.assertEqual(status, "pulled")
        self.assertTrue((work / "new.txt").exists())

    def test_skip_pull_when_dirty(self):
        _, work = self._clone_pair()
        (work / "README").write_text("dirty\n")
        status = sync.pull_if_clean(str(work))
        self.assertEqual(status, "dirty")

    def test_commit_only_this_turns_delta(self):
        _, work = self._clone_pair()
        os.environ["GIT_HOOK_PUSH"] = "0"
        (work / "wip.txt").write_text("unrelated wip\n")  # pre-existing dirty
        target = work / "touched.txt"

        sync.on_pre_tool_call("write_file", {"path": str(work / "README")})
        target.write_text("ours\n")
        sync.on_post_tool_call("write_file", {"path": str(target)}, status="ok")

        result = sync.commit_and_push(
            str(work), set(sync._dirty.get(str(work), set())), "test"
        )
        self.assertIn("committed", result)
        log = _git(["log", "-1", "--name-only", "--pretty=format:"], cwd=work).stdout
        self.assertIn("touched.txt", log)
        self.assertNotIn("wip.txt", log)
        status = _git(["status", "--porcelain"], cwd=work).stdout
        self.assertIn("wip.txt", status)

    def test_edit_to_already_dirty_file_is_committed(self):
        """A file already dirty at turn start still counts as this turn's delta.

        Path-only snapshots missed it: the path sits in the dirty set before and
        after the edit, so `delta` was empty, the edit never reached `_dirty`,
        and the file stayed uncommittable for as long as it remained dirty.
        """
        _, work = self._clone_pair()
        os.environ["GIT_HOOK_PUSH"] = "0"
        (work / "wip.txt").write_text("someone else's pre-existing dirt\n")
        target = work / "README"
        target.write_text("before-before\n")
        _stamp(target, 1_000_000_000_000_000_000)

        sync.on_pre_tool_call("patch", {"path": str(target)})
        # Same length on purpose: only the mtime term can flag this rewrite.
        target.write_text("during-during\n")
        _stamp(target, 1_700_000_000_000_000_000)
        sync.on_post_tool_call("patch", {"path": str(target)}, status="ok")

        self.assertEqual(sync._dirty.get(str(work)), {"README"})
        result = sync.commit_and_push(
            str(work), set(sync._dirty.get(str(work), set())), "test"
        )
        self.assertIn("committed", result)
        log = _git(["log", "-1", "--name-only", "--pretty=format:"], cwd=work).stdout
        self.assertIn("README", log)
        self.assertNotIn("wip.txt", log)
        self.assertIn("wip.txt", _git(["status", "--porcelain"], cwd=work).stdout)

    def test_snapshot_signature_moves_when_already_dirty_file_is_rewritten(self):
        _, work = self._clone_pair()
        target = work / "README"
        target.write_text("before-before\n")
        _stamp(target, 1_000_000_000_000_000_000)
        first = sync._porcelain_snapshot(str(work))

        target.write_text("during-during\n")
        _stamp(target, 1_700_000_000_000_000_000)
        second = sync._porcelain_snapshot(str(work))

        self.assertEqual(set(first), set(second))  # the path set never moved
        self.assertNotEqual(first["README"], second["README"])
        self.assertEqual(sync._porcelain_paths(str(work)), frozenset({"README"}))

    def test_read_then_flush_does_not_commit_unrelated(self):
        _, work = self._clone_pair()
        os.environ["GIT_HOOK_PUSH"] = "0"
        (work / "wip.txt").write_text("wip\n")
        readme = work / "README"
        sync.on_pre_tool_call("read_file", {"path": str(readme)})
        sync.on_post_tool_call("read_file", {"path": str(readme)}, status="ok")
        sync._flush("test")
        log = _git(["log", "-1", "--pretty=%s"], cwd=work).stdout.strip()
        self.assertEqual(log, "init")

    def test_disabled(self):
        os.environ["GIT_HOOK_COMMIT"] = "0"
        self.assertTrue(sync.disabled())
        sync.on_pre_tool_call("read_file", {"path": "/tmp"})
        self.assertEqual(sync._pulled, set())

    def test_register_hooks(self):
        ctx = MagicMock()
        sync.register(ctx)
        names = [c.args[0] for c in ctx.register_hook.call_args_list]
        self.assertEqual(
            names,
            ["pre_tool_call", "post_tool_call", "post_llm_call", "on_session_end"],
        )

    def test_busy_preserves_dirty_and_surfaces(self):
        _, work = self._clone_pair()
        os.environ["GIT_HOOK_PUSH"] = "0"
        target = work / "touched.txt"
        sync.on_pre_tool_call("write_file", {"path": str(target)})
        target.write_text("ours\n")
        sync.on_post_tool_call("write_file", {"path": str(target)}, status="ok")
        (work / ".git" / "MERGE_HEAD").write_text("deadbeef\n")
        result = sync.on_post_llm_call()
        self.assertIsNotNone(result)
        self.assertIn("busy", result["context"])
        self.assertTrue(sync._dirty.get(str(work)))

    def test_commit_skip_is_warning_status(self):
        _, work = self._clone_pair()
        os.environ["GIT_HOOK_PUSH"] = "0"
        hook = work / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        if not os.access(hook, os.X_OK):
            # On a noexec temp dir git ignores hooks ("advice.ignoredHook") and
            # the commit succeeds, so this branch is unreachable here. Point
            # TMPDIR at an exec-capable path to exercise it.
            self.skipTest("temp dir is noexec; git ignores hooks here")
        (work / "touched.txt").write_text("ours\n")
        status = sync.commit_and_push(str(work), {"touched.txt"}, "test")
        self.assertEqual(status, "commit-skipped")

    def test_push_fail_surfaces_and_retries(self):
        _, work = self._clone_pair()
        target = work / "touched.txt"
        sync.on_pre_tool_call("write_file", {"path": str(target)})
        target.write_text("ours\n")
        sync.on_post_tool_call("write_file", {"path": str(target)}, status="ok")
        _git(["remote", "set-url", "origin", str(self.root / "missing.git")], cwd=work)
        result = sync.on_post_llm_call()
        self.assertIsNotNone(result)
        self.assertIn("push failed", result["context"])
        self.assertIn(str(work), sync._unpushed)
        again = sync.on_post_llm_call()
        self.assertIsNotNone(again)
        self.assertIn("push failed", again["context"])
        self.assertIn(str(work), sync._unpushed)


class TransientPaths(unittest.TestCase):
    """`.hermes-tmp.*` temps must never reach `git add` — one stale pathspec
    fails the whole add batch and the real changes of the turn never commit."""

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory()
        self.root = _init_repo(Path(self.td.name) / "repo")
        os.environ["PROJECTS_ROOT"] = str(self.td.name)
        os.environ["GIT_HOOK_PUSH"] = "0"

    def tearDown(self):
        sync.reset_state()
        os.environ.pop("PROJECTS_ROOT", None)
        os.environ.pop("GIT_HOOK_PUSH", None)
        self.td.cleanup()

    def test_porcelain_hides_atomic_write_temps(self):
        (self.root / ".hermes-tmp.ABC123").write_text("scratch\n")
        (self.root / "real.txt").write_text("real\n")
        paths = sync._porcelain_paths(str(self.root))
        self.assertNotIn(".hermes-tmp.ABC123", paths)
        self.assertIn("real.txt", paths)

    def test_dead_temp_path_does_not_block_the_batch(self):
        (self.root / "README").write_text("hello\nagain\n")
        status = sync.commit_and_push(
            str(self.root), {"README", ".hermes-tmp.ABC123"}, "test"
        )
        self.assertIn("committed", status)
        log = _git(
            ["log", "-1", "--name-only", "--pretty=format:"], cwd=self.root
        ).stdout
        self.assertIn("README", log)
        self.assertNotIn(".hermes-tmp", log)

    def test_temp_seen_by_hooks_never_enters_dirty(self):
        target = self.root / "README"
        sync.on_pre_tool_call("write_file", {"path": str(target)})
        target.write_text("hello\nedited\n")
        temp = self.root / ".hermes-tmp.lhjcKo"
        temp.write_text("scratch\n")
        sync.on_post_tool_call("write_file", {"path": str(target)}, status="ok")
        temp.unlink()  # renamed over the target before the flush
        self.assertEqual(sync._dirty.get(str(self.root)), {"README"})

    def test_deleted_tracked_path_is_still_staged(self):
        (self.root / "README").unlink()
        status = sync.commit_and_push(str(self.root), {"README"}, "test")
        self.assertIn("committed", status)
        show = _git(["show", "--name-status", "--pretty=format:"], cwd=self.root).stdout
        self.assertIn("D\tREADME", show)

    def test_file_created_and_deleted_in_one_turn_is_dropped(self):
        (self.root / "real.txt").write_text("real\n")
        status = sync.commit_and_push(
            str(self.root), {"real.txt", "transient-scratch.txt"}, "test"
        )
        self.assertIn("committed", status)
        log = _git(
            ["log", "-1", "--name-only", "--pretty=format:"], cwd=self.root
        ).stdout
        self.assertIn("real.txt", log)
        self.assertNotIn("transient-scratch.txt", log)


INCIDENT_STDERR = (
    # Captured verbatim from a real GitHub refusal of a direct push to
    # `main` of aean0x/rk3588-nixos-nas (ruleset `openclaw-pr`, 2026-09-21).
    "remote: error: GH013: Repository rule violations found for refs/heads/main.\n"
    "remote: Review all repository rules at "
    "https://github.com/aean0x/rk3588-nixos-nas/rules?ref=refs%2Fheads%2Fmain\n"
    "remote: \n"
    "remote: - Changes must be made through a pull request.\n"
    "remote: \n"
    "To https://github.com/aean0x/rk3588-nixos-nas.git\n"
    " ! [remote rejected] HEAD -> main (push declined due to repository rule violations)\n"
    "error: failed to push some refs to 'https://github.com/aean0x/rk3588-nixos-nas.git'"
)


class PushRejectionClass(unittest.TestCase):
    """Only a policy refusal is terminal; a network/auth failure must still
    keep the existing commit-and-retry contract."""

    def test_real_gh013_text_is_policy(self):
        """Verbatim stderr from the 2026-09-21 strand incident."""
        self.assertTrue(sync._policy_rejection(INCIDENT_STDERR))

    def test_other_remote_policy_rejections_are_policy(self):
        self.assertTrue(
            sync._policy_rejection(
                "remote: error: GH006: Protected branch update failed for refs/heads/main."
            )
        )
        self.assertTrue(
            sync._policy_rejection(
                "remote: error: refusing to update checked out branch: refs/heads/main"
            )
        )
        self.assertTrue(
            sync._policy_rejection(
                "remote: error: Required status check \"lint\" is expected.\n"
                "remote: error: hook declined to update refs/heads/main"
            )
        )

    def test_transient_failures_are_not_policy(self):
        for text in (
            "fatal: unable to access 'https://github.com/x/y.git/':"
            " Could not resolve host: github.com",
            "remote: Invalid username or password.\n"
            "fatal: Authentication failed for 'https://github.com/x/y.git/'",
            "fatal: the remote end hung up unexpectedly",
            "fatal: '/nonexistent/missing.git' does not appear to be a git repository",
            "fatal: unable to write new index file",
            "",
        ):
            self.assertFalse(sync._policy_rejection(text), text)


class ProtectedBranch(unittest.TestCase):
    """PR-protected branches must never accumulate hook commits.

    The incident this encodes: the hook committed onto a PR-protected `main`,
    the push was refused (GH013), and every later pass re-committed and
    re-pushed — stranding commits on `main` (breaking the ff-only pull) and
    re-logging the same remote error 30 times in one day.
    """

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        os.environ["PROJECTS_ROOT"] = str(self.root)
        os.environ.pop("GIT_HOOK_PUSH", None)
        os.environ.pop("GIT_HOOK_COMMIT", None)
        self.bare, self.work = self._clone_pair()
        self.counter = self.root / "push-attempts"

    def tearDown(self):
        sync.reset_state()
        os.environ.pop("PROJECTS_ROOT", None)
        os.environ.pop("GIT_HOOK_PUSH", None)
        self.td.cleanup()

    def _clone_pair(self):
        origin = _init_repo(self.root / "origin")
        bare = self.root / "origin.git"
        _git(["clone", "--bare", str(origin), str(bare)], cwd=self.root)
        work = self.root / "work"
        _git(["clone", str(bare), str(work)], cwd=self.root)
        _git(["config", "user.name", "dev"], cwd=work)
        _git(["config", "user.email", "dev@example.com"], cwd=work)
        return bare, work

    def _deny_pushes(self, remote_line):
        """Remote-side refusal, the way GitHub states a ruleset rejection."""
        hook = self.bare / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            f"echo attempt >> {self.counter}\n"
            f"echo 'remote: error: {remote_line}' >&2\n"
            "exit 1\n"
        )
        hook.chmod(0o755)
        if not os.access(hook, os.X_OK):
            self.skipTest("temp dir is noexec; the remote hook cannot run")

    def _allow_pushes(self):
        hook = self.bare / "hooks" / "pre-receive"
        if hook.exists():
            hook.unlink()

    def _attempts(self):
        if not self.counter.exists():
            return 0
        return len(self.counter.read_text().split())

    def _head(self):
        return _git(["rev-parse", "HEAD"], cwd=self.work).stdout.strip()

    def _commits(self):
        return _git(["rev-list", "--count", "HEAD"], cwd=self.work).stdout.strip()

    def test_policy_rejection_leaves_no_stranded_commit(self):
        self._deny_pushes("GH013: Repository rule violations found for refs/heads/main.")
        before = self._head()
        (self.work / "notes.txt").write_text("turn work\n")

        status = sync.commit_and_push(str(self.work), {"notes.txt"}, "test")

        self.assertTrue(status.startswith("protected "), status)
        self.assertEqual(self._head(), before)  # the hook's own commit is gone
        self.assertEqual(self._commits(), "1")
        # ... and the edit is still there, staged, for a human to PR.
        self.assertIn("notes.txt", _git(["status", "--porcelain"], cwd=self.work).stdout)
        self.assertEqual(self._attempts(), 1)
        self.assertEqual(
            _git(["rev-parse", "main"], cwd=self.bare).stdout.strip(),
            _git(["rev-parse", "main"], cwd=self.work).stdout.strip(),
        )

    def test_later_turns_do_not_push_the_closed_branch_again(self):
        self._deny_pushes("GH013: Repository rule violations found for refs/heads/main.")
        (self.work / "a.txt").write_text("one\n")
        sync.commit_and_push(str(self.work), {"a.txt"}, "test")
        before = self._head()
        (self.work / "b.txt").write_text("two\n")

        status = sync.commit_and_push(str(self.work), {"b.txt"}, "test")

        self.assertTrue(status.startswith("protected "), status)
        self.assertEqual(self._head(), before)
        self.assertEqual(self._attempts(), 1)  # no second push attempt
        self.assertIn("b.txt", _git(["status", "--porcelain"], cwd=self.work).stdout)

    def test_a_feature_branch_is_not_affected(self):
        self._deny_pushes("GH013: Repository rule violations found for refs/heads/main.")
        (self.work / "a.txt").write_text("one\n")
        sync.commit_and_push(str(self.work), {"a.txt"}, "test")
        self.assertIn((str(self.work), "main"), sync._protected)

        _git(["switch", "-c", "feat/x"], cwd=self.work)
        _git(["commit", "-m", "carry the work over"], cwd=self.work)
        self._allow_pushes()
        (self.work / "b.txt").write_text("two\n")

        status = sync.commit_and_push(str(self.work), {"b.txt"}, "test")

        self.assertIn("pushed", status)
        pushed = _git(
            ["show", "--pretty=format:", "--name-only", "feat/x"], cwd=self.bare
        ).stdout
        self.assertIn("b.txt", pushed)

    def test_transient_push_failure_still_keeps_the_commit(self):
        _git(
            ["remote", "set-url", "origin", str(self.root / "missing.git")],
            cwd=self.work,
        )
        (self.work / "note.txt").write_text("ours\n")

        status = sync.commit_and_push(str(self.work), {"note.txt"}, "test")

        self.assertTrue(status.startswith("committed_local_only"), status)
        self.assertEqual(self._commits(), "2")  # the commit is kept, for a retry
        self.assertNotIn((str(self.work), "main"), sync._protected)

    def test_policy_note_is_reported_once_per_branch(self):
        self._deny_pushes("GH013: Repository rule violations found for refs/heads/main.")
        target = self.work / "touched.txt"
        sync.on_pre_tool_call("write_file", {"path": str(target)})
        target.write_text("ours\n")
        sync.on_post_tool_call("write_file", {"path": str(target)}, status="ok")

        first = sync.on_post_llm_call()

        self.assertIsNotNone(first)
        self.assertIn("rejects direct pushes to main", first["context"])
        self.assertIn("touched.txt", first["context"])

        more = self.work / "more.txt"
        sync.on_pre_tool_call("write_file", {"path": str(more)})
        more.write_text("more\n")
        sync.on_post_tool_call("write_file", {"path": str(more)}, status="ok")

        self.assertIsNone(sync.on_post_llm_call())  # one warning, not one per turn
        self.assertEqual(self._attempts(), 1)

    def test_stranded_commits_stop_being_retried(self):
        """The GH013 storm: a commit already stranded on the branch must not be
        re-pushed (and re-logged) on every flush."""
        self._deny_pushes("GH013: Repository rule violations found for refs/heads/main.")
        (self.work / "stranded.txt").write_text("stranded\n")
        _git(["add", "stranded.txt"], cwd=self.work)
        _git(["commit", "-m", "stranded"], cwd=self.work)
        sync._unpushed.add(str(self.work))

        first = sync.on_post_llm_call()

        self.assertIsNotNone(first)
        self.assertIn("a local commit stays unpushed", first["context"])
        self.assertEqual(self._attempts(), 1)

        self.assertIsNone(sync.on_post_llm_call())
        self.assertEqual(self._attempts(), 1)

    def test_undo_refuses_to_touch_a_commit_it_did_not_make(self):
        (self.work / "manual.txt").write_text("by hand\n")
        _git(["add", "manual.txt"], cwd=self.work)
        _git(["commit", "-m", "by hand"], cwd=self.work)
        head = self._head()
        parent = _git(["rev-parse", "HEAD~1"], cwd=self.work).stdout.strip()

        # HEAD is not the commit the hook claims it just made: leave it alone.
        self.assertFalse(sync._undo_own_commit(str(self.work), parent, "0" * 40))

        self.assertEqual(self._head(), head)
        self.assertIn(
            "manual.txt",
            _git(["show", "--pretty=format:", "--name-only", "HEAD"], cwd=self.work).stdout,
        )

    def test_undo_keeps_the_content_staged(self):
        before = self._head()
        (self.work / "x.txt").write_text("x\n")
        _git(["add", "x.txt"], cwd=self.work)
        _git(["commit", "-m", "hook commit"], cwd=self.work)
        head = self._head()

        self.assertTrue(sync._undo_own_commit(str(self.work), before, head))

        self.assertEqual(self._head(), before)
        self.assertIn("A  x.txt", _git(["status", "--porcelain"], cwd=self.work).stdout)


class DeadCwd(unittest.TestCase):
    """A cwd deleted under the process must be a no-op, never a refused call.

    os.getcwd() raises FileNotFoundError once the directory the process sits in
    is gone — the normal end state of a kanban worker whose scratch workspace
    drains under it. An exception escaping a tool-call callback is not a no-op:
    the plugin manager refuses the tool call, which is how a dead cwd cost three
    `terminal` calls and one `execute_code` call (2026-09-26).
    """

    def setUp(self):
        sync.reset_state()
        self.original = os.getcwd()
        self.td = tempfile.TemporaryDirectory()
        self.gone = Path(self.td.name) / "gone"
        self.gone.mkdir()
        os.chdir(self.gone)
        os.rmdir(self.gone)

    def tearDown(self):
        os.chdir(self.original)
        sync.reset_state()
        self.td.cleanup()

    def test_cwd_helper_returns_none_when_cwd_is_gone(self):
        self.assertIsNone(sync._cwd())
        self.assertEqual(sync.extract_paths("terminal", {"command": "true"}), [])

    def test_cwd_helper_returns_live_cwd(self):
        live = Path(self.td.name) / "live"
        live.mkdir()
        os.chdir(live)
        self.assertEqual(sync._cwd(), str(live.resolve()))

    def test_tool_call_hooks_survive_a_dead_cwd(self):
        sync.on_pre_tool_call("terminal", {"command": "true"})
        sync.on_pre_tool_call("execute_code", {"code": "print(1)"})
        sync.on_post_tool_call("terminal", {"command": "true"}, status="ok")
        self.assertEqual(sync._roots_for("terminal", {"command": "true"}), [])
        self.assertEqual(sync._dirty, {})

    def test_hook_guard_swallows_a_raising_body(self):
        """Fail open: a callback that raises must not reach the plugin manager."""
        with patch.object(sync, "_snapshot_and_pull", side_effect=OSError("dead fs")):
            sync.on_pre_tool_call("terminal", {"command": "true"})
        with patch.object(sync, "_record_delta", side_effect=OSError("dead fs")):
            sync.on_post_tool_call("terminal", {"command": "true"}, status="ok")


if __name__ == "__main__":
    if not shutil.which("git"):
        raise SystemExit("git required")
    unittest.main()
