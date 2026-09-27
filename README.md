# git-hook — auto fetch/pull/commit/push for every worktree Hermes touches

A Hermes Agent plugin (hooks only, no tools) that keeps the git worktrees the
agent works in honest:

- **Before a read**: at most one `git pull --ff-only` per worktree per session
  (a pull fetches as part of the pull), and only when the worktree is clean and
  has an upstream. Nothing runs on a dirty worktree, so the agent reads current
  code instead of a stale checkout.
- **After the turn**: commit **only the files this turn changed** and push.
  It does not sweep up unrelated dirty files, and it does not commit the
  agent's own secrets, state, caches or logs.
- **On a branch whose direct push the remote refuses** (a PR-protected `main`):
  drop its own unpushed commit, leave the edit staged and dirty, stop
  committing on that branch, and say so once — instead of stranding commits
  that can never be pushed.

Failure is not silent, and it is not published a second way: a busy index, a
hook rejection, a failed or timed-out push is written to the log, the paths stay
queued for a retry, and the plugin also returns a note from its turn-end and
session-end hooks. Hermes currently discards hook return values, so the log is
where those notes actually surface.

State is per session: two sessions in one repository never share a batch, so one
session's flush cannot publish the other session's half-finished edit.

## Install

```bash
hermes plugins install <owner>/hermes-git-hook
hermes plugins enable git-hook
```

Manual install: copy this directory into `~/.hermes/plugins/git-hook` and add
`git-hook` to `plugins.enabled` in `config.yaml`.

## Configuration

Every knob is an environment variable, read per call — no config.yaml schema.

| Variable | Default | Meaning |
| --- | --- | --- |
| `GIT_HOOK_ROOTS` | `$PROJECTS_ROOT` | Colon-separated allowlist of directories whose worktrees are synced (opt-in per repo). A repo outside every entry is never pulled, committed or pushed. `*` means every worktree. |
| `GIT_HOOK_COMMIT` | `1` (on) | `0` disables all commits and pushes. Reads still pull. |
| `GIT_HOOK_PUSH` | `1` (on) | **Push is on** for every allowlisted repo. `0` keeps commits local. |
| `GIT_HOOK_COMMIT_MSG` | `update <name>` | Overrides the commit subject. |
| `GIT_HOOK_COMMIT_PATH` | – | Extra `PATH` entries (colon-separated) for git subprocesses, e.g. a credential helper. |
| `GIT_HOOK_PULL_TIMEOUT_S` | `12` | Per-worktree pull timeout. |
| `GIT_HOOK_HOOK_BUDGET_S` | `20` | Total budget for one `pre_tool_call` callback across every root it touches. The host allows 30 s before it fails the hook closed and blocks the tool call, so this stays under it. A root that would overrun is skipped and retried on the next call. |
| `GIT_HOOK_PUSH_TIMEOUT_S` | `20` | Per-worktree push timeout. |
| `PROJECTS_ROOT` | `$HERMES_HOME/projects` | Treated as the agent's own workspace: never skipped by the secret rules. |
| `HERMES_HOME` | `$HOME/.hermes` | Root of the agent's state; `$HERMES_HOME/plugins` is never synced. |

Falsy values are `0`, `false`, `no`, `off` and the empty string.

## What it will never do

- Touch a repo outside `GIT_HOOK_ROOTS` (default: only `PROJECTS_ROOT`).
- Run a repo's `core.fsmonitor` command: every git call passes
  `-c core.fsmonitor=`.
- Stage a secret-looking file in any repo: `.env*`, `auth.json`,
  `credentials*`, `id_rsa*`/`id_dsa*`/`id_ecdsa*`/`id_ed25519*`, `*.pem`,
  `*.key`. The filter runs on every path before `git add`, and untracked
  directories are listed file by file (`-uall`), so a collapsed `dir/` entry is
  never staged whole.
- Touch anything under `/nix`, `/proc`, `/sys`, `/dev`, `/run`, `/tmp`,
  `/var/tmp`.
- Stage the agent's own state: `credentials`, `secrets`, `mcp-tokens`,
  `sessions`, `memories`, `state`, `hmc_state`, `logs`, `cache`,
  `cost-snapshots`, or `auth.json`, `config.yaml`, `.env`, `*.db` directly
  under `$HERMES_HOME` or `$HERMES_HOME/profiles/<name>/`.
- Sync the plugin install tree (`$HERMES_HOME/plugins/**`). A catalog install
  is a single-commit checkout pinned to the SHA that was reviewed; this plugin
  will not pull or commit it away from that commit.
- Run git under its module lock. One session's slow pull cannot make another
  session's `post_tool_call` callback look "still running" and get skipped.
- Refuse a tool call. The hook is fail-open end to end: a deleted cwd, an
  unreadable path argument, or a raising git subprocess leaves that turn
  unsynced instead of failing the callback — an exception escaping a
  `pre_tool_call`/`post_tool_call` callback makes the plugin manager refuse the
  tool call outright, which is real work lost to a housekeeping hook.

## Branches that reject direct pushes

If the remote refuses a push for **policy** reasons — GitHub branch protection
or a ruleset that requires a pull request (GH013/GH006), a server-side
`pre-receive` check, a non-bare remote with that branch checked out — then that
commit can never be pushed, and neither can any later one. The hook:

1. drops the commit it just made (`reset --soft` back over **its own** commit,
   guarded by a re-read of `HEAD`, so a commit made by anyone else in the
   meantime is never touched) and leaves the change staged, so the edit stays
   in the working tree and visible in `git status`;
2. stops committing on that branch for the rest of the process, leaving later
   edits dirty too;
3. says so once per branch, in-session:
   `repo: origin rejects direct pushes to main (a PR is required) — git-hook
   left sync.py uncommitted and pushed nothing. Put them on a branch and open
   a PR.`

Network, authentication and timeout failures are **not** policy: those keep the
original contract (commit, report, retry on the next flush). A commit already
stranded on such a branch (from an older version) stops being re-pushed too, so
the same remote error is no longer logged once per flush.

Why this shape, not "move the commit to `hook/<timestamp>` and push that
branch": that would make the hook a branch manager — one throwaway branch per
rejected edit, the agent left on a branch it did not ask for (and fighting the
agent's own `git switch`), or the edit reverted out of the tree once the
checked-out branch is reset. Refusing is the narrow contract: commit only where
a push can land, and make the alternative visible instead.

Two deliberate costs: on a PR-protected branch the tree stays dirty for the rest
of the session, so the ff-only pull is skipped (the pull path is dirty-gated by
design); and the memory of "this branch is closed" lasts for the process, not on
disk, so a removed ruleset heals itself at the next start while a long-lived
process re-tests at most once per session.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

The suite is in-process: it builds throwaway repositories under a temp dir and
drives the real code paths (`_skipped`, `commit_and_push`, `_push`, the
transient-path filter). No network access.

## License

MIT — see [LICENSE](LICENSE).
