# git-hook — auto fetch/pull/commit/push for every worktree Hermes touches

A Hermes Agent plugin (hooks only, no tools) that keeps the git worktrees the
agent works in honest:

- **Before a read**: `git fetch` and, when the worktree is clean,
  `git pull --ff-only`, so the agent reads current code instead of a stale
  checkout.
- **After the turn**: commit **only the files this turn changed** and push.
  It does not sweep up unrelated dirty files, and it does not commit the
  agent's own secrets, state, caches or logs.

Failure is never silent: a busy index, a hook rejection, a failed or timed-out
push is reported back into the session as tool-call context, and the paths stay
queued for a retry.

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
| `GIT_HOOK_COMMIT` | `1` (on) | `0` disables all commits and pushes. Reads still pull. |
| `GIT_HOOK_PUSH` | `1` (on) | `0` keeps commits local. Set it when you do not want an agent turn to push. |
| `GIT_HOOK_COMMIT_MSG` | `update <name>` | Overrides the commit subject. |
| `GIT_HOOK_COMMIT_PATH` | – | Extra `PATH` entries (colon-separated) for git subprocesses, e.g. a credential helper. |
| `GIT_HOOK_PULL_TIMEOUT_S` | `12` | Per-worktree pull timeout. |
| `GIT_HOOK_PUSH_TIMEOUT_S` | `20` | Per-worktree push timeout. |
| `PROJECTS_ROOT` | `$HERMES_HOME/projects` | Treated as the agent's own workspace: never skipped by the secret rules. |
| `HERMES_HOME` | `$HOME/.hermes` | Root of the agent's state; `$HERMES_HOME/plugins` is never synced. |

Falsy values are `0`, `false`, `no`, `off` and the empty string.

## What it will never do

- Touch anything under `/nix`, `/proc`, `/sys`, `/dev`, `/run`, `/tmp`,
  `/var/tmp`.
- Stage the agent's own state: `credentials`, `secrets`, `mcp-tokens`,
  `sessions`, `memories`, `state`, `hmc_state`, `logs`, `cache`,
  `cost-snapshots`, or `auth.json`, `config.yaml`, `.env`, `*.db` under
  `$HERMES_HOME`.
- Sync the plugin install tree (`$HERMES_HOME/plugins/**`). A catalog install
  is a single-commit checkout pinned to the SHA that was reviewed; this plugin
  will not pull or commit it away from that commit.
- Run git under its module lock. One session's slow pull cannot make another
  session's `post_tool_call` callback look "still running" and get skipped.

## Tests

```bash
python -m pytest tests/ -q
```

The suite is in-process: it builds throwaway repositories under a temp dir and
drives the real code paths (`_skipped`, `commit_and_push`, `_push`, the
transient-path filter). No network access.

## License

MIT — see [LICENSE](LICENSE).
