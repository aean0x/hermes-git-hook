"""In-process git-sync: pull-before-read, commit only this turn's delta."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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


def _key(root) -> tuple:
    """State key for a hook call that passes no session_id (the tests' default).

    Every state map is keyed by (session_id, root) so two sessions in one repo
    cannot share a batch.
    """
    return ("", str(root))


def _init_repo(path: Path, *, name="dev", email="dev@example.com") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd=path)
    _git(["config", "user.name", name], cwd=path)
    _git(["config", "user.email", email], cwd=path)
    # No background maintenance: `git gc --auto` and maintenance run after these
    # commands and keep writing into .git while TemporaryDirectory.cleanup() is
    # already walking it, which fails the teardown with "Directory not empty".
    _git(["config", "gc.auto", "0"], cwd=path)
    _git(["config", "maintenance.auto", "false"], cwd=path)
    (path / "README").write_text("hello\n")
    _git(["add", "README"], cwd=path)
    _git(["commit", "-m", "init"], cwd=path)
    return path


class ExtractPaths(unittest.TestCase):
    def test_read_file_path(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            target = Path(tmp) / "a.txt"
            target.write_text("x")
            paths = sync.extract_paths("read_file", {"path": str(target)})
            self.assertEqual(paths, [str(target.resolve())])

    def test_patch_header_paths(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
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
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        # A clone takes the global config, so disable background maintenance here
        # too: it writes into .git after the test's last git command.
        _git(["config", "gc.auto", "0"], cwd=work)
        _git(["config", "maintenance.auto", "false"], cwd=work)
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
            str(work), set(sync._dirty.get(_key(work), set())), "test"
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

        self.assertEqual(sync._dirty.get(_key(work)), {"README"})
        result = sync.commit_and_push(
            str(work), set(sync._dirty.get(_key(work), set())), "test"
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
        self.assertTrue(sync._dirty.get(_key(work)))

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
        self.assertIn(_key(work), sync._unpushed)
        again = sync.on_post_llm_call()
        self.assertIsNotNone(again)
        self.assertIn("push failed", again["context"])
        self.assertIn(_key(work), sync._unpushed)


class TransientPaths(unittest.TestCase):
    """`.hermes-tmp.*` temps must never reach `git add` — one stale pathspec
    fails the whole add batch and the real changes of the turn never commit."""

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        self.assertEqual(sync._dirty.get(_key(self.root)), {"README"})

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


class ScopeAndSecrets(unittest.TestCase):
    """Only opted-in repos are synced, and secrets are filtered per staged file."""

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.td.name)
        self.saved = {k: os.environ.get(k) for k in
                      ("HERMES_HOME", "PROJECTS_ROOT", "GIT_HOOK_ROOTS", "GIT_HOOK_PUSH")}
        for k in self.saved:
            os.environ.pop(k, None)
        os.environ["GIT_HOOK_PUSH"] = "0"

    def tearDown(self):
        sync.reset_state()
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.td.cleanup()

    def test_hermes_home_repo_flush_never_stages_secrets_or_unrelated_untracked(self):
        home = _init_repo(self.base / "hermes")
        os.environ["HERMES_HOME"] = str(home)
        os.environ["GIT_HOOK_ROOTS"] = str(home)  # explicit opt-in
        (home / "notes").mkdir()
        (home / "notes" / "old.txt").write_text("unrelated untracked\n")
        soul = home / "SOUL.md"

        sync.on_pre_tool_call("write_file", {"path": str(soul)})
        soul.write_text("me\n")
        (home / "notes" / "new.md").write_text("ours\n")
        (home / ".env").write_text("K=v\n")
        (home / "auth.json").write_text("{}\n")
        (home / "profiles" / "work").mkdir(parents=True)
        (home / "profiles" / "work" / ".env").write_text("K=v\n")
        (home / "id_ed25519").write_text("key\n")
        sync.on_post_tool_call("write_file", {"path": str(soul)}, status="ok")
        sync._flush("test")

        committed = set(
            _git(["show", "--name-only", "--pretty=format:"], cwd=home).stdout.split()
        )
        self.assertEqual(committed, {"SOUL.md", "notes/new.md"})

    def test_repo_outside_opt_in_roots_is_ignored_and_fsmonitor_never_runs(self):
        # Both repos sit under PROJECTS_ROOT (exempt from the skip rules), so
        # only the GIT_HOOK_ROOTS allowlist can keep the second one out.
        os.environ["PROJECTS_ROOT"] = str(self.base / "projects")
        os.environ["GIT_HOOK_ROOTS"] = str(self.base / "projects" / "repo")
        outside = _init_repo(self.base / "projects" / "not-opted-in")
        self.assertIsNone(sync.git_root(str(outside / "README")))

        inside = _init_repo(self.base / "projects" / "repo")
        marker = self.base / "fsmonitor-ran"
        hook = self.base / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        hook.chmod(0o755)
        _git(["config", "core.fsmonitor", str(hook)], cwd=inside)
        (inside / "README").write_text("dirty\n")
        self.assertEqual(sync.git_root(str(inside / "README")), str(inside.resolve()))
        sync.on_pre_tool_call("read_file", {"path": str(inside / "README")})
        self.assertIn("README", sync._before[_key(inside.resolve())])
        self.assertFalse(marker.exists(), "repo-config fsmonitor command was executed")


class SessionScopedState(unittest.TestCase):
    """The gateway runs several sessions in one process: never share a batch."""

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        os.environ["PROJECTS_ROOT"] = str(self.root)
        os.environ["GIT_HOOK_PUSH"] = "0"
        origin = _init_repo(self.root / "origin")
        self.work = self.root / "work"
        _git(["clone", str(origin), str(self.work)], cwd=self.root)
        _git(["config", "user.name", "dev"], cwd=self.work)
        _git(["config", "user.email", "dev@example.com"], cwd=self.work)

    def tearDown(self):
        sync.reset_state()
        os.environ.pop("PROJECTS_ROOT", None)
        os.environ.pop("GIT_HOOK_PUSH", None)
        self.td.cleanup()

    def _turn(self, session: str, name: str) -> None:
        target = self.work / name
        sync.on_pre_tool_call("write_file", {"path": str(target)}, session_id=session)
        target.write_text(f"from {session}\n")
        sync.on_post_tool_call(
            "write_file", {"path": str(target)}, status="ok", session_id=session
        )

    def _committed(self) -> str:
        return _git(
            ["show", "--name-only", "--pretty=format:"], cwd=self.work
        ).stdout

    def test_one_session_flush_leaves_the_other_batch_alone(self):
        self._turn("sess-a", "a.txt")
        self._turn("sess-b", "b.txt")

        sync._flush("post_llm_call", "sess-a")
        committed = self._committed()
        self.assertIn("a.txt", committed)
        self.assertNotIn("b.txt", committed)
        self.assertIn("b.txt", sync._dirty[("sess-b", str(self.work))])

        sync._flush("post_llm_call", "sess-b")
        self.assertIn("b.txt", self._committed())

    def test_pull_is_once_per_session_not_once_per_process(self):
        _git(["remote", "set-url", "origin", str(self.root / "origin")], cwd=self.work)
        self.assertEqual(sync.pull_if_clean(str(self.work), "sess-a"), "pulled")
        self.assertEqual(sync.pull_if_clean(str(self.work), "sess-a"), "already")
        self.assertEqual(sync.pull_if_clean(str(self.work), "sess-b"), "pulled")


class PullBudget(unittest.TestCase):
    """A pre_tool_call callback must not run past the host's hook budget."""

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name)
        origin = _init_repo(self.root / "origin")
        bare = self.root / "origin.git"
        _git(["clone", "--bare", str(origin), str(bare)], cwd=self.root)
        self.work = self.root / "work"
        _git(["clone", str(bare), str(self.work)], cwd=self.root)

    def tearDown(self):
        sync.reset_state()
        self.td.cleanup()

    def test_expired_budget_skips_the_pull_and_stays_retryable(self):
        status = sync.pull_if_clean(
            str(self.work), "sess", deadline=time.monotonic() - 1
        )
        self.assertEqual(status, "budget")
        self.assertEqual(sync._pulled, set(), "a skipped pull must not count as done")

    def test_budget_shortens_rather_than_lengthens_the_pull(self):
        status = sync.pull_if_clean(
            str(self.work), "sess", deadline=time.monotonic() + 60
        )
        self.assertEqual(status, "pulled")


class WorktreeLock(unittest.TestCase):
    """The lock must not land in the worktree, where it reads as untracked dirt."""

    def setUp(self):
        sync.reset_state()
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = _init_repo(Path(self.td.name) / "repo")
        self.wt = Path(self.td.name) / "wt"
        _git(["worktree", "add", "-b", "side", str(self.wt)], cwd=self.repo)

    def tearDown(self):
        sync.reset_state()
        self.td.cleanup()

    def test_plain_repo_keeps_the_lock_in_dot_git(self):
        self.assertEqual(sync._worktree_git_dir(str(self.repo)), self.repo / ".git")

    def test_linked_worktree_lock_lives_in_its_per_worktree_git_dir(self):
        git_dir = sync._worktree_git_dir(str(self.wt))
        self.assertIn("worktrees", str(git_dir))
        with sync._with_repo_lock(str(self.wt)):
            self.assertTrue((git_dir / "git-hook.lock").exists())
            self.assertFalse((self.wt / ".git-auto-sync.lock").exists())
        status = _git(["status", "--porcelain"], cwd=self.wt).stdout
        self.assertNotIn("lock", status)


if __name__ == "__main__":
    if not shutil.which("git"):
        raise SystemExit("git required")
    unittest.main()
