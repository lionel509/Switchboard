# Cleanup — `lionel509/Switchboard`

Teardown for this checkout. **Switchboard** is stdlib-only Python — a local
Anthropic-compatible gateway (`router.py`), the lineup/CLI (`switchboard.py`),
a curses TUI (`tui.py`), the picker publisher (`sync-models.py`) and 18 test
files. No manifest, nothing to install: `python3 -m pytest -q` runs on a bare
checkout (**54 passed** as of this write).

Almost everything that matters here lives **outside** the repo on purpose —
keys, tokens, the rewritten `~/.claude/settings.json`, the picker cache, and
the installed copy under `~/.local/share/claude-router/`.

## ⚠ What must NOT be cleaned

| Path | Why it stays |
| --- | --- |
| `models.json` | the catalog + lineup + provider declarations; `switchboard.py add/remove/apply` **writes to it in place** — a dirty `models.json` is a real change to review, not debris |
| `router.py`, `switchboard.py`, `tui.py`, `sync-models.py`, `test_*.py` | source and tests |
| `README.md`, `LICENSE` | docs and licence |
| `*.log`, `*-limits.json` | ignored, but **local records**: `requests.log` is the request/usage log (model names, token counts, costs) and the `*-limits.json` files are vendor quota snapshots (`router.py:819-827`). Deleting them loses history — do it only if you mean to. |

`git clean -fdX` cannot touch the tracked files at all. The `*key*` rule above
is deliberately broad; nothing tracked matches it (`git ls-files -ci` is empty).

## What this project leaves behind

| Path / thing | Created by | Size note |
| --- | --- | --- |
| `requests.log`, `router.log` | the router and the CLI's restart path (`switchboard.py:1277`) | KBs–MBs; **already ignored**, kept by policy above |
| `<vendor>-limits.json` | `router.py:827`, beside whatever `CLAUDE_ROUTER_LOG` points at — in-tree when run from the checkout | KBs; ignored |
| `.venv/`, `venv/`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `.coverage`, `*.egg-info/` | only if someone creates them — there is no manifest, but a venv is the obvious way to get `pytest` | MBs |
| `__pycache__/`, `*.pyc` | running any of the modules | KBs |
| `.env`, `.env.*`, `.envrc` | hand-made; **this program never reads them** (the key is `~/.config/openrouter-key`) | bytes |
| `.idea/`, `.vscode/`, `*.swp`, `*~`, `.DS_Store`, `Thumbs.db` | editors / macOS | bytes |
| `.claude/settings.local.json` | Claude Code | bytes |

**Deliberately written somewhere else — do not go looking for them here:**

| Thing | Where it lands |
| --- | --- |
| OpenRouter keys (`openrouter-key`, `-key-nonzdr`, `-key-gemma`) | `~/.config/` (`router.py:20-26`, `switchboard.py:28`), mode 600 |
| OAuth tokens | `~/.config/<vendor>-oauth.json` (`switchboard.py:723,817`) |
| `~/.claude/settings.json` **and its `settings.json.bak` backup** | rewritten by `switchboard.py apply` (`:332-337,1243`) |
| Picker cache | `~/.claude/cache/gateway-models.json` (`sync-models.py:27`) |
| The installed copy the README's shell wrapper points at | `~/.local/share/claude-router/` — the README says to **clone straight there** |
| The `switchboard` shim | `~/.local/bin/switchboard` |
| Probe log | `/tmp/probe.log` (README **Troubleshooting**) |

## Preview

```bash
git clean -ndX -e '!.env' -e '!.env.*'
```

## Clean the repo

Stop the router first if it is running from this directory (`switchboard restart`
afterwards, or kill the `router.py` on `CLAUDE_ROUTER_PORT`, default 8787) — it
holds `router.log` open.

```bash
git clean -fdX -e '!.env' -e '!.env.*'
```

```bash
git status --short                    # no tracked file modified or deleted
git ls-files -ci --exclude-standard   # must print nothing
```

Nothing else to run: no packages were installed by this repo, no build, no
hooks, no cache it is responsible for. To re-run the checks afterwards:
`python3 -m pytest -q` (needs only `pytest`).

## Outside the repo

Everything below is **real and specific to Switchboard**, but it is the
program's *configuration and credentials* — removing it is uninstalling the
tool, not tidying a checkout. Preview each line before the `rm`.

**The installed copy + shim (the README's Install):**

```bash
ls -la ~/.local/share/claude-router ~/.local/bin/switchboard 2>/dev/null
rm -rf ~/.local/share/claude-router          # the clone the shell wrapper execs
rm -f  ~/.local/bin/switchboard              # the PATH shim
```

**Credentials — mode 600, and revoking them upstream first is the only real
way to undo a leak:**

```bash
ls -la ~/.config/openrouter-key* ~/.config/*-oauth.json 2>/dev/null
rm -f ~/.config/openrouter-key ~/.config/openrouter-key-nonzdr ~/.config/openrouter-key-gemma
rm -f ~/.config/*-oauth.json
```

**Claude Code state this tool rewrote:**

```bash
ls -la ~/.claude/settings.json ~/.claude/settings.json.bak 2>/dev/null
rm -f ~/.claude/settings.json.bak            # the backup switchboard.py:332 wrote
# ~/.claude/settings.json is live Claude Code config — do NOT delete it here;
# it still holds the lineup even after Switchboard is gone.
rm -f ~/.claude/cache/gateway-models.json    # the picker cache; regenerated by sync-models.py
```

**Logs and quota snapshots outside the tree** (default log dir is the install
dir, so removing `~/.local/share/claude-router` already took them):

```bash
ls -la ~/.local/share/claude-router/*.log ~/.local/share/claude-router/*-limits.json 2>/dev/null
rm -f /tmp/probe.log                         # the README's throwaway probe
```

**Nothing else outside the repo.** No venvs outside the tree (there is no
dependency install), no pip/npm caches worth purging for this project, no
Playwright browsers, no Docker images, no launchd plists, no model caches —
the models are OpenRouter's, not local.

## Secrets

The real credentials are **not in this repo and never were** — they are
`~/.config/openrouter-key*` and `~/.config/*-oauth.json`. Inside the tree,
`*key*`, `.env`, and now `.env.*` are ignored, and every command above keeps
`.env` via `-e '!.env' -e '!.env.*'`.

To remove local env files by hand:

```bash
rm -f .env .env.local .envrc
```

If a key ever *is* found tracked in git, rotate it on OpenRouter first, then
open an issue — never just delete the file. (This repo has no gitleaks hook;
the `*key*` pattern is the only thing standing between a `printf > key.txt`
and a push.)
