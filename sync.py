"""Hook-driven git sync for any worktree Hermes touches.

pre_tool_call  — ff-only pull on first read of a clean repo (before the
                 tool result reaches the model).
post_tool_call — record porcelain delta (only files this turn changed).
post_llm_call / on_session_end — commit those paths and push.

Fail-open everywhere. Never registers a tool. Never shells a sidecar.
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Set, Tuple

log = logging.getLogger("plugins.git_hook")

# One dirty path's content signature: (XY status, st_size, st_mtime_ns).
PathSig = Tuple[str, int, int]

_WRITE_TOOLS = frozenset({"write_file", "patch", "skill_manage"})
_PATH_KEYS = ("path", "file_path", "workdir")
_SKIP_PREFIXES = (
    "/nix/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/run/",
    "/tmp/",
    "/var/tmp/",
)
_SENSITIVE_DIRS = frozenset(
    {
        "credentials",
        "secrets",
        "mcp-tokens",
        "sessions",
        "memories",
        "state",
        "hmc_state",
        "logs",
        "cache",
        "cost-snapshots",
    }
)
_SENSITIVE_FILE_PREFIXES = ("auth.json", "config.yaml", ".env")
# Secret-looking file names that are never auto-staged in ANY repo.
_SECRET_NAME_PREFIXES = (
    ".env",
    "auth.json",
    "credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)
_SECRET_NAME_SUFFIXES = (".pem", ".key")
_PATCH_FILE_RE = re.compile(
    r"^\*\*\* (?:(?:Update|Add|Delete) File|Rename File(?: From)?): (.+)$"
)
_FALSE = frozenset({"0", "false", "no", "off", ""})

# A push the remote refuses for POLICY reasons — branch protection, a ruleset,
# a server-side pre-receive check — can never succeed on a retry: the branch is
# closed to direct pushes. Without this the hook re-commits and re-pushes on
# every pass, the branch stays ahead of its upstream for good (which also breaks
# the ff-only pull), and the same remote error is logged on every flush. Such a
# rejection is terminal for that branch. Transient failures (network, auth,
# timeout) keep the existing "committed_local_only + retry" contract.
_STRUCTURAL_PUSH_RE = re.compile(
    r"\bGH0\d{2}\b"
    r"|protected branch"
    r"|refusing to update checked out branch"
    r"|hook declined to update"
    r"|must be made through a pull request"
    r"|required status check"
    r"|pre-receive hook declined",
    re.IGNORECASE,
)

# write_file/patch land content via a same-directory atomic rename. Their temp
# names (`.hermes-tmp.XXXXXX`, plus the webui `.name.hermes-tmp-<pid>` form) are
# visible to post_tool_call but gone by flush time, and one dead pathspec makes
# `git add` exit non-zero — which drops the whole batch and re-queues the dead
# path forever, so every real change of the turn goes uncommitted.
_TRANSIENT_MARKER = ".hermes-tmp"

_lock = threading.Lock()
# Every state map below is keyed by (session_id, root). The gateway runs
# several sessions in one process, and a shared batch would let one session's
# flush publish the other session's half-finished edit under its own commit.
SessionKey = Tuple[str, str]
_pulled: Set[SessionKey] = set()
# Per-(session, root) snapshot of the dirty set: rel path -> content signature
# (status, size, mtime_ns). Value-carrying, not path-only — see
# _porcelain_snapshot.
_before: Dict[SessionKey, "Dict[str, PathSig]"] = {}
_dirty: Dict[SessionKey, Set[str]] = {}
_unpushed: Set[SessionKey] = set()
_root_cache: Dict[str, Optional[str]] = {}
# (root, branch) whose upstream refused a direct push on policy grounds. Learned
# from the rejection itself — never guessed, never persisted: a process restart
# re-tests the branch once. A persisted memo would outlive the ruleset it came
# from and silently keep refusing a branch that had become pushable again.
_protected: Set[Tuple[str, str]] = set()
# (root, branch) already reported to the operator by this process: the warning
# is per branch, not per flush.
_policy_noted: Set[Tuple[str, str]] = set()

# Flush outcomes that must put paths back on _dirty (commit never landed).
_RETRY_DIRTY = frozenset(
    {"busy", "locked", "timeout", "add-failed", "commit-skipped"}
)

# Never prompt. A credential or ssh passphrase question inside a hook would
# block until the timeout, and pre_tool_call fails the user's tool call closed.
_NO_PROMPT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}

# Total budget for one pre_tool_call callback, across every root it touches.
# The host allows 30 s before it blocks the tool call, so stay well under it.
_HOOK_BUDGET_S = 20.0


def _truthy(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSE


def disabled() -> bool:
    return not _truthy("GIT_HOOK_COMMIT", True)


def _home() -> Path:
    return Path(
        os.environ.get("HERMES_HOME")
        or (Path(os.environ.get("HOME") or Path.home()) / ".hermes")
    ).expanduser().resolve()


def _projects_root() -> Path:
    raw = os.environ.get("PROJECTS_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_home() / "projects").resolve()


def _allowed_roots() -> list[Path]:
    """Directories whose worktrees git-hook may touch (opt-in per repo).

    `GIT_HOOK_ROOTS` is a colon-separated allowlist; `*` restores "every
    worktree". Unset, only repos under `PROJECTS_ROOT` are synced.
    """
    raw = os.environ.get("GIT_HOOK_ROOTS", "").strip()
    if raw == "*":
        return [Path("/")]
    if not raw:
        return [_projects_root()]
    return [Path(p).expanduser().resolve() for p in raw.split(":") if p.strip()]


def _allowed(path: str) -> bool:
    p = Path(path).resolve()
    return any(p == r or r in p.parents for r in _allowed_roots())


def _timeout(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _git_bin() -> str:
    found = shutil.which("git")
    if found:
        return found
    for candidate in (
        "/run/current-system/sw/bin/git",
        "/usr/bin/git",
        "/opt/homebrew/bin/git",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return "git"


def _git(
    args: list[str],
    cwd: str,
    timeout: float,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    extra = os.environ.get("GIT_HOOK_COMMIT_PATH", "").strip()
    bits = [p for p in extra.split(":") if p]
    bits += [
        "/run/current-system/sw/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    cur = env.get("PATH", "")
    env["PATH"] = ":".join(bits + ([cur] if cur else []))
    if extra_env:
        env.update(extra_env)
    # core.fsmonitor in a repo's own config is a command git runs on status /
    # add / commit. The repo is picked by the model, so never run it.
    return subprocess.run(
        [_git_bin(), "-c", "core.fsmonitor=", *args],
        cwd=cwd,
        timeout=timeout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=False,
    )


def _existing_path(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith(("http://", "https://", "data:")):
        return None
    try:
        path = Path(text).expanduser()
        if path.exists():
            return str(path.resolve())
    except OSError:
        return None
    # Parent may exist even if the file is about to be created.
    try:
        parent = Path(text).expanduser().parent
        if parent.exists():
            return str(parent.resolve())
    except OSError:
        return None
    return None


def _cwd() -> Optional[str]:
    """The process working directory, or None when that directory is gone.

    `os.getcwd()` raises FileNotFoundError (ENOENT) once the directory the
    process was started in has been removed — the normal end state of a kanban
    worker whose scratch workspace drains under it. A dead cwd means there is
    nothing to pull and nothing to snapshot, so the hook's answer is "no path",
    never an error.
    """
    try:
        return os.getcwd()
    except OSError:
        return None


def extract_paths(tool_name: str, args: Optional[Dict[str, Any]]) -> list[str]:
    """Filesystem paths a tool is about to touch. Conservative."""
    args = args if isinstance(args, dict) else {}
    found: list[str] = []
    seen: Set[str] = set()

    def _add(raw: Any) -> None:
        path = _existing_path(raw)
        if path and path not in seen:
            seen.add(path)
            found.append(path)

    for key in _PATH_KEYS:
        _add(args.get(key))
    _add(args.get("image_url"))

    patch_text = args.get("patch")
    if isinstance(patch_text, str):
        for line in patch_text.splitlines():
            match = _PATCH_FILE_RE.match(line)
            if match:
                _add(match.group(1).strip())

    if tool_name in {"terminal", "execute_code"} and not found:
        # No path argument: fall back to the process cwd. A deleted cwd yields
        # None here (_cwd), which _add ignores — never an exception.
        _add(_cwd())
    return found


def _sensitive(path: Path, home: Path) -> bool:
    """Never auto-sync the agent's own secrets/state under its home dir."""
    try:
        rel = path.relative_to(home)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    # The plugin install tree is not the user's work. A catalog install is a
    # `git clone --depth 1` checked out at a pinned SHA, so treating it as a
    # worktree would let this hook pull or commit an installed plugin away
    # from the commit the catalog reviewed. Read-only from here.
    if parts[0] == "plugins":
        return True
    # A sensitive directory anywhere on the path (e.g. home/secrets/api.key).
    if any(p in _SENSITIVE_DIRS for p in parts):
        return True
    # The agent's own secret files live directly under home (or a profile).
    if len(parts) == 1 or (len(parts) == 3 and parts[0] == "profiles"):
        name = parts[-1]
        if name.startswith(_SENSITIVE_FILE_PREFIXES) or ".db" in name:
            return True
    return False


def _secret_file(root: str, rel: str) -> bool:
    """Per-file secret filter, applied to every path before `git add`."""
    name = rel.rsplit("/", 1)[-1].lower()
    if name.startswith(_SECRET_NAME_PREFIXES) or name.endswith(_SECRET_NAME_SUFFIXES):
        return True
    p = Path(root, rel)
    projects = _projects_root()
    if p == projects or projects in p.parents:
        return False
    home = _home()
    return (p == home or home in p.parents) and _sensitive(p, home)


def _skipped(path: str) -> bool:
    try:
        p = Path(path).resolve()
        projects = _projects_root()
        if p == projects or projects in p.parents:
            return False
        home = _home()
        if p == home or home in p.parents:
            return _sensitive(p, home)
    except OSError:
        return True
    resolved = str(p).rstrip("/") + "/"
    if any(resolved.startswith(pref) or str(p) == pref.rstrip("/") for pref in _SKIP_PREFIXES):
        return True
    return False


def git_root(path: str) -> Optional[str]:
    """Innermost worktree containing path, or None if skipped / not git."""
    try:
        p = Path(path).resolve()
        start = p if p.is_dir() else p.parent
        start = start.resolve()
    except OSError:
        return None
    key = str(start)
    if key in _root_cache:
        return _root_cache[key]
    if _skipped(key) or not _allowed(key):
        _root_cache[key] = None
        return None
    try:
        proc = _git(
            ["rev-parse", "--show-toplevel"],
            cwd=key,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        _root_cache[key] = None
        return None
    if proc.returncode != 0:
        _root_cache[key] = None
        return None
    root = (proc.stdout or "").strip()
    if not root:
        _root_cache[key] = None
        return None
    if _skipped(root) or not _allowed(root):
        _root_cache[key] = None
        return None
    _root_cache[key] = root
    return root


def _transient(rel: str) -> bool:
    """True for Hermes atomic-write temp names — dead by flush time."""
    return _TRANSIENT_MARKER in rel.rsplit("/", 1)[-1]


def _path_sig(root: str, rel: str, status: str) -> PathSig:
    """Cheap content signature for one dirty path.

    A path-only dirty set cannot see an edit to a file that is ALREADY dirty:
    the path is present before and after, so it produces no delta, never enters
    `_dirty`, and stays uncommittable for as long as it remains dirty. Folding
    the XY status plus size/mtime in makes a rewrite of an already-dirty path a
    delta again, without widening the per-turn contract to a whole-repo sweep.
    """
    try:
        st = os.stat(os.path.join(root, rel))
    except OSError:
        # Deleted, or renamed away: the status alone identifies it.
        return (status, -1, -1)
    return (status, st.st_size, st.st_mtime_ns)


def _porcelain_snapshot(root: str) -> Dict[str, PathSig]:
    """Dirty paths of `root` mapped to their content signature."""
    try:
        # -uall: list untracked files one by one. A collapsed `?? dir/` entry
        # would make `git add -- dir/` stage everything in it.
        proc = _git(["status", "--porcelain", "-z", "-uall"], cwd=root, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if proc.returncode != 0 or not proc.stdout:
        return {}
    parts = proc.stdout.split("\0")
    sigs: Dict[str, PathSig] = {}
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if not entry or len(entry) <= 3:
            continue
        # "XY PATH" — status is 2 chars, then space, then path.
        status = entry[:2]
        rel = entry[3:]
        if not rel:
            continue
        # Atomic-write temps are never part of the turn's real delta.
        if not _transient(rel):
            sigs[rel] = _path_sig(root, rel, status)
        # rename/copy: next -z field is the original path
        if entry[0] in {"R", "C"} or (len(entry) > 1 and entry[1] in {"R", "C"}):
            if i < len(parts) and parts[i]:
                source = parts[i]
                if not _transient(source):
                    sigs[source] = _path_sig(root, source, status)
                i += 1
    return sigs


def _porcelain_paths(root: str) -> frozenset[str]:
    """The dirty path set — callers that only ask "is this repo dirty?"."""
    return frozenset(_porcelain_snapshot(root))


def _busy(root: str) -> bool:
    git_dir = Path(root) / ".git"
    if git_dir.is_file():
        return False
    # A rebase is in progress only while git's own state directory is there:
    # `rebase-merge/` (merge backend) or `rebase-apply/` (am backend) — the same
    # pair `git status` reads. A bare `REBASE_HEAD` is not that: git 2.55 leaves
    # it behind after `rebase --continue` finishes, the tree is clean, and both
    # `rebase --abort` and `rebase --quit` answer "no rebase in progress", so
    # nothing ever removes it. Reading it as "busy" latched the worktree busy
    # forever and every later sync for it was skipped.
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return True
    for name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    ):
        if (git_dir / name).exists():
            return True
    return False


def _has_upstream(root: str) -> bool:
    try:
        proc = _git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=root,
            timeout=3,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _worktree_git_dir(root: str) -> Path:
    """The per-worktree git dir, which is never inside the worktree itself.

    In a linked worktree `.git` is a *file* holding ``gitdir: <path>``, and the
    path it points at lives under the main repo's ``.git/worktrees/<name>``.
    The lock belongs there: a lock written into the worktree shows up as an
    untracked change on the next `git status`.
    """
    dot = Path(root) / ".git"
    if not dot.is_file():
        return dot
    try:
        text = dot.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return dot
    if not text.startswith("gitdir:"):
        return dot
    target = Path(text.split(":", 1)[1].strip())
    if not target.is_absolute():
        target = (dot.parent / target).resolve()
    return target


def _branch(root: str) -> str:
    """Checked-out branch name; "HEAD" when detached, "" if unresolvable."""
    try:
        proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _head_sha(root: str) -> str:
    """Full HEAD sha, "" on an unborn branch."""
    try:
        proc = _git(["rev-parse", "HEAD"], cwd=root, timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _policy_rejection(stderr: str) -> bool:
    """True when the remote refused the push because of branch policy."""
    return bool(_STRUCTURAL_PUSH_RE.search(stderr or ""))


def _status_branch(status: str) -> str:
    """Branch field of a `protected <branch>` / `protected-stranded <branch>`."""
    parts = status.split(" ", 1)
    return parts[1] if len(parts) > 1 and parts[1] else "HEAD"


def _protected_branch(root: str, branch: str) -> bool:
    with _lock:
        return bool(branch) and (root, branch) in _protected


def _mark_protected(root: str, branch: str) -> None:
    if not branch:
        return
    with _lock:
        _protected.add((root, branch))


def _note_policy_once(root: str, branch: str) -> bool:
    """True the first time this process reports `branch` as push-closed."""
    with _lock:
        key = (root, branch)
        if key in _policy_noted:
            return False
        _policy_noted.add(key)
        return True


def _undo_own_commit(root: str, prev_sha: str, sha: str) -> bool:
    """Drop the commit this hook just made, keeping its content staged.

    Only called after the branch's push was refused by policy, i.e. the commit
    exists nowhere but this clone, and only when HEAD is still provably the
    commit this pass created — a commit that appeared meanwhile (another
    session, a human) is never touched. `--soft` leaves the paths staged, so the
    edit stays in the working tree instead of being silently reverted.
    """
    if not prev_sha or not sha:
        return False
    if _head_sha(root) != sha:
        log.info("git-hook: HEAD moved since %s; leaving history alone", sha[:9])
        return False
    try:
        back = _git(["reset", "--soft", prev_sha], cwd=root, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return False
    if back.returncode != 0:
        log.warning(
            "git-hook: could not undo own commit %s (%s)",
            sha[:9],
            (back.stderr or "").strip()[:200],
        )
        return False
    log.info("git-hook: undid own unpushable commit %s; paths stay staged", sha[:9])
    return True


def _paths_hint(paths: Iterable[str]) -> Optional[str]:
    """Short file list for an operator-facing note."""
    names = [Path(p).name for p in sorted(paths) if p]
    if not names:
        return None
    if len(names) <= 3:
        return ", ".join(names)
    return ", ".join(names[:3]) + f" (+{len(names) - 3} more)"


def _with_repo_lock(root: str):
    lock_path = _worktree_git_dir(root) / "git-hook.lock"

    class _Guard:
        def __enter__(self):
            self.fh = open(lock_path, "a+")
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.fh.close()
                raise
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()

    return _Guard()


def pull_if_clean(
    root: str, session_id: str = "", deadline: Optional[float] = None
) -> str:
    """ff-only pull. Returns a short status token. Never raises.

    *deadline* is a ``time.monotonic()`` instant: it caps the subprocess so one
    callback cannot run past the host's hook budget and fail the user's tool
    call closed.
    """
    key = (session_id, root)
    if key in _pulled:
        return "already"
    if _busy(root):
        return "busy"
    if _porcelain_paths(root):
        return "dirty"
    if not _has_upstream(root):
        _pulled.add(key)
        return "no-upstream"
    timeout = _timeout("GIT_HOOK_PULL_TIMEOUT_S", 12)
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "budget"
        timeout = min(timeout, remaining)
    try:
        with _with_repo_lock(root):
            if _porcelain_paths(root):
                return "dirty"
            proc = _git(
                ["pull", "--ff-only", "--quiet"],
                cwd=root,
                timeout=timeout,
                extra_env=_NO_PROMPT_ENV,
            )
    except OSError:
        return "locked"
    except subprocess.TimeoutExpired:
        log.warning("git-hook: pull timeout %s", root)
        return "timeout"
    _pulled.add(key)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        log.info("git-hook: pull skipped %s (%s)", root, err)
        return "failed"
    return "pulled"


def _commit_message(paths: Iterable[str]) -> str:
    override = os.environ.get("GIT_HOOK_COMMIT_MSG", "").strip()
    if override:
        return override
    names = [Path(p).name for p in paths]
    if len(names) == 1:
        return f"update {names[0]}"
    return f"update {len(names)} files"


def _push(root: str, source: str, sha: str) -> str:
    """Push HEAD. Fail-open; caller owns dirty/unpushed bookkeeping.

    A policy rejection returns `protected <branch>` and is remembered, so the
    branch is not pushed (or committed to) again in this process.
    """
    if not _truthy("GIT_HOOK_PUSH", True):
        log.info("git-hook [%s]: committed %s (push off)", source, sha)
        return f"committed {sha}"
    branch = _branch(root)
    if _protected_branch(root, branch):
        return f"protected {branch or 'HEAD'}"
    timeout = _timeout("GIT_HOOK_PUSH_TIMEOUT_S", 20)
    try:
        if not _has_upstream(root):
            push = _git(
                ["push", "origin", "HEAD"],
                cwd=root,
                timeout=timeout,
                extra_env=_NO_PROMPT_ENV,
            )
        else:
            push = _git(["push"], cwd=root, timeout=timeout, extra_env=_NO_PROMPT_ENV)
    except subprocess.TimeoutExpired:
        log.warning("git-hook [%s]: committed %s, push timeout", source, sha)
        return f"committed_local_only {sha}"
    except OSError as exc:
        log.warning("git-hook [%s]: committed %s, push error %s", source, sha, exc)
        return f"committed_local_only {sha}"
    if push.returncode != 0:
        err = (push.stderr or "").strip()[:200]
        if _policy_rejection(push.stderr or ""):
            _mark_protected(root, branch)
            log.warning(
                "git-hook [%s]: %s rejects direct pushes (policy: %s); %s stays local",
                source,
                branch or "HEAD",
                err[:120],
                sha,
            )
            return f"protected {branch or 'HEAD'}"
        log.warning("git-hook [%s]: committed %s, push failed %s", source, sha, err)
        return f"committed_local_only {sha}"
    log.info("git-hook [%s]: pushed %s", source, sha)
    return f"pushed {sha}"


def _stageable(root: str, rels: Iterable[str]) -> list[str]:
    """The subset of `rels` that `git add` accepts.

    A stale path — a temp name already renamed away, or a file created and
    deleted inside the same turn — makes `git add -- <path>` exit non-zero
    ("pathspec did not match any files") and git then stages NOTHING, so the
    whole turn's work is lost and the dead path is retried forever. Drop those.
    A missing-but-tracked path is kept: that is a deletion, and `git add`
    stages it.
    """
    present: list[str] = []
    gone: list[str] = []
    for rel in rels:
        if not rel or _transient(rel):
            continue
        if os.path.lexists(os.path.join(root, rel)):
            present.append(rel)
        else:
            gone.append(rel)
    if gone:
        try:
            proc = _git(["ls-files", "-z", "--", *gone], cwd=root, timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            log.debug("git-hook: ls-files failed for %s", gone)
        else:
            if proc.returncode == 0 and proc.stdout:
                present.extend(
                    p for p in proc.stdout.split("\0") if p and not _transient(p)
                )
    kept = sorted(set(present))
    dropped = len(set(rels)) - len(kept)
    if dropped:
        log.debug("git-hook: %s skipped %d unstaged-able path(s)", root, dropped)
    return kept


def commit_and_push(root: str, paths: Set[str], source: str) -> str:
    """Stage only `paths`, commit, optionally push. Fail-open."""
    if not paths:
        return "clean"
    if _busy(root):
        log.warning("git-hook: busy %s (merge/rebase in progress); will retry", root)
        return "busy"
    rels = sorted({p for p in paths if p and not p.startswith("/")})
    secret = [r for r in rels if _secret_file(root, r)]
    if secret:
        log.info("git-hook: %s not staging %d secret-looking path(s)", root, len(secret))
        rels = [r for r in rels if r not in secret]
    # Never hand git a path that no longer resolves: one dead pathspec fails the
    # entire add batch (git exits non-zero and stages nothing).
    rels = _stageable(root, rels)
    if not rels:
        return "clean"
    # A branch whose direct push was already refused by policy is closed: a new
    # commit here would only be stranded — and re-logged — again. Keep the paths
    # dirty instead, so the edit stays visible to a human.
    branch = _branch(root)
    if _protected_branch(root, branch):
        log.info(
            "git-hook: %s rejects direct pushes; %d path(s) left uncommitted",
            branch,
            len(rels),
        )
        return f"protected {branch or 'HEAD'}"
    try:
        with _with_repo_lock(root):
            prev_sha = _head_sha(root)
            add = _git(["add", "--", *rels], cwd=root, timeout=10)
            if add.returncode != 0:
                log.warning(
                    "git-hook: add failed %s %s",
                    root,
                    (add.stderr or "").strip()[:200],
                )
                return "add-failed"
            if not _porcelain_paths(root):
                return "clean"
            msg = _commit_message(rels)
            # useConfigOnly: never invent hermes@local / agent@hostname
            commit = _git(
                [
                    "-c",
                    "user.useConfigOnly=true",
                    "commit",
                    "-m",
                    msg,
                    "--",
                    *rels,
                ],
                cwd=root,
                timeout=15,
                extra_env=dict(_NO_PROMPT_ENV),
            )
            if commit.returncode != 0:
                err = (commit.stderr or commit.stdout or "").strip()[:300]
                log.warning("git-hook: commit skipped %s (%s)", root, err)
                return "commit-skipped"
            sha = (_git(["rev-parse", "--short", "HEAD"], cwd=root, timeout=3).stdout or "").strip()
            status = _push(root, source, sha)
            if status.startswith("protected"):
                # Nobody has this commit but this clone: drop it again, content
                # staged, instead of leaving the branch ahead of its upstream.
                if not _undo_own_commit(root, prev_sha, _head_sha(root)):
                    return f"protected-stranded {_branch(root) or 'HEAD'}"
            return status
    except OSError:
        return "locked"
    except subprocess.TimeoutExpired:
        log.warning("git-hook: git timeout %s", root)
        return "timeout"


def _roots_for(tool_name: str, args: Optional[Dict[str, Any]]) -> list[str]:
    roots: list[str] = []
    seen: Set[str] = set()
    try:
        paths = extract_paths(tool_name, args)
    except OSError:
        # Path extraction is best-effort: an unreadable/dead path must leave
        # this turn unsynced, not fail the tool call that triggered it.
        log.debug("git-hook: path extraction failed for %s", tool_name, exc_info=True)
        return roots
    for path in paths:
        root = git_root(path)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    return roots


def _hook_guard(name: str, body: Callable[[], None]) -> None:
    """Run one tool-call callback body; never let a failure reach the manager.

    An exception escaping a tool-call callback is not a no-op: the plugin
    manager records a callback failure and REFUSES the tool call, so an
    optional sync hook costs real work when it trips (a deleted cwd lost three
    `terminal` calls and one `execute_code` call on 2026-09-26). Fail open;
    the traceback goes to the log.
    """
    try:
        body()
    except Exception:
        log.warning("git-hook: %s failed; tool call proceeds", name, exc_info=True)


def on_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    session_id: str = "",
    **_kwargs: Any,
) -> None:
    if disabled():
        return
    _hook_guard(
        "pre_tool_call",
        lambda: _snapshot_and_pull(tool_name, args, session_id),
    )


def _snapshot_and_pull(
    tool_name: str, args: Optional[Dict[str, Any]], session_id: str = ""
) -> None:
    # Writes still record a before-snapshot; pull only on non-write.
    do_pull = tool_name not in _WRITE_TOOLS
    roots = _roots_for(tool_name, args)
    # One budget for the whole callback: the host fails pre_tool_call closed
    # when it overruns, which would block the user's tool call.
    deadline = time.monotonic() + _timeout("GIT_HOOK_HOOK_BUDGET_S", _HOOK_BUDGET_S)
    for root in roots:
        key = (session_id, root)
        # Snapshot + optional pull run OUTSIDE the module lock. The lock only
        # guards dict state; holding it across git subprocesses (porcelain up
        # to 5s, pull up to GIT_HOOK_PULL_TIMEOUT_S) made one session's slow
        # git op block every other session's git-hook callbacks for seconds —
        # the plugin manager then saw the callback still running at the next
        # tool completion and skipped it ("previous timeout or still running").
        with _lock:
            missing = key not in _before
        if missing:
            snap = _porcelain_snapshot(root)
            with _lock:
                if key not in _before:
                    _before[key] = snap
        if do_pull:
            status = pull_if_clean(root, session_id, deadline)
            if status == "pulled":
                snap = _porcelain_snapshot(root)
                with _lock:
                    _before[key] = snap


def on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    status: str = "",
    session_id: str = "",
    **_kwargs: Any,
) -> None:
    if disabled():
        return
    if status in {"blocked"}:
        return
    _hook_guard(
        "post_tool_call",
        lambda: _record_delta(tool_name, args, session_id),
    )


def _record_delta(
    tool_name: str, args: Optional[Dict[str, Any]], session_id: str = ""
) -> None:
    roots = _roots_for(tool_name, args)
    if not roots:
        return
    # Porcelain (git status subprocess, up to 5s per root) runs OUTSIDE the
    # module lock so a busy repo or another session's git op cannot hold this
    # callback hostage — see on_pre_tool_call. Delta bookkeeping is a short
    # locked section; the set ops are atomic under the GIL.
    snapshots: dict[str, Dict[str, PathSig]] = {}
    for root in roots:
        snapshots[root] = _porcelain_snapshot(root)
    with _lock:
        for root in roots:
            key = (session_id, root)
            before = _before.get(key) or {}
            after = snapshots[root]
            # A path is this turn's delta when it is new OR when its content
            # signature moved. The second half is what makes an edit to an
            # already-dirty file committable; pre-existing dirt nobody touched
            # keeps an identical signature and is still left alone.
            delta = {rel for rel, sig in after.items() if before.get(rel) != sig}
            if delta:
                _dirty.setdefault(key, set()).update(delta)
            _before[key] = after


def _note(root: str, status: str, hint: Optional[str] = None) -> Optional[str]:
    short = Path(root).name
    if status in {"busy", "locked", "timeout"}:
        return f"{short}: {status} — git-hook will retry"
    if status == "add-failed":
        return f"{short}: git add failed (see agent.log)"
    if status == "commit-skipped":
        return f"{short}: commit skipped (identity/hook). Files stay uncommitted."
    if status.startswith("committed_local_only"):
        return f"{short}: committed locally but push failed. git-hook will retry push."
    if status.startswith("protected-stranded"):
        return (
            f"{short}: origin rejects direct pushes to {_status_branch(status)} "
            "(a PR is required) — a local commit stays unpushed. Move it to a "
            "branch and open a PR."
        )
    if status.startswith("protected"):
        files = hint or "the turn's file(s)"
        return (
            f"{short}: origin rejects direct pushes to {_status_branch(status)} "
            f"(a PR is required) — git-hook left {files} uncommitted and pushed "
            "nothing. Put them on a branch and open a PR."
        )
    return None


def _may_note(root: str, status: str) -> bool:
    """Policy notes are per branch per process; every other note repeats."""
    if not status.startswith("protected"):
        return True
    return _note_policy_once(root, _status_branch(status))


def _flush(source: str, session_id: str = "") -> Optional[str]:
    """Commit and push this session's queued paths only.

    Another session's batch in the same repo is left alone: it belongs to a
    turn that has not finished, and publishing it here would attribute its
    half-finished edit to this commit.
    """
    if disabled():
        return None
    with _lock:
        mine = [k for k in _dirty if k[0] == session_id]
        pending = {k: set(_dirty[k]) for k in mine if _dirty[k]}
        unpushed = {k for k in _unpushed if k[0] == session_id}
        for k in mine:
            _dirty.pop(k, None)
    notes: list[str] = []
    for key, paths in pending.items():
        root = key[1]
        try:
            status = commit_and_push(root, paths, source)
        except Exception:
            log.exception("git-hook: flush failed %s", root)
            status = "timeout"
        note = _note(root, status, _paths_hint(paths))
        if note and not _may_note(root, status):
            note = None
        if note:
            notes.append(note)
        with _lock:
            if status in _RETRY_DIRTY:
                _dirty.setdefault(key, set()).update(paths)
            if status.startswith("committed_local_only") or status.startswith(
                "protected-stranded"
            ):
                _unpushed.add(key)
            elif status.startswith("pushed") or status.startswith("committed "):
                _unpushed.discard(key)
    for key in unpushed:
        if key in pending:
            continue
        root = key[1]
        try:
            sha = (
                _git(["rev-parse", "--short", "HEAD"], cwd=root, timeout=3).stdout or ""
            ).strip()
            status = _push(root, source, sha or "HEAD")
        except Exception:
            log.exception("git-hook: push retry failed %s", root)
            continue
        if status.startswith("protected"):
            # Short-circuited on the policy memo; a commit is stranded here.
            status = f"protected-stranded {_status_branch(status)}"
        note = _note(root, status)
        if note and not _may_note(root, status):
            note = None
        if note:
            notes.append(note)
        with _lock:
            if status.startswith("pushed"):
                _unpushed.discard(key)
    if not notes:
        return None
    return "git-hook:\n" + "\n".join(f"- {n}" for n in notes)


def on_post_llm_call(
    session_id: str = "", **kwargs: Any
) -> Optional[Dict[str, str]]:
    if kwargs.get("error"):
        return None
    body = _flush("post_llm_call", session_id)
    if body:
        return {"context": body}
    return None


def on_session_end(session_id: str = "", **kwargs: Any) -> Optional[Dict[str, str]]:
    reason = str(kwargs.get("turn_exit_reason") or "")
    if reason == "error" and kwargs.get("error"):
        return None
    body = _flush(f"on_session_end:{reason or 'unknown'}", session_id)
    if body:
        return {"context": body}
    return None


def reset_state() -> None:
    """Tests only."""
    with _lock:
        _pulled.clear()
        _before.clear()
        _dirty.clear()
        _unpushed.clear()
        _root_cache.clear()
        _protected.clear()
        _policy_noted.clear()


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_session_end", on_session_end)
    log.info("git-hook: registered (in-process, any worktree)")
