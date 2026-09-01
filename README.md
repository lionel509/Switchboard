# Switchboard

Make Claude Code's `/model` picker switch **providers**, not just Claude tiers.

```
 1. Default (recommended)   Opus 5 with 1M context
 2. Opus (1M context)  ✔    Opus 5 with 1M context
 ...
 7. Z.ai: GLM Latest        From gateway
 8. xAI: Grok Latest        From gateway
 9. MoonshotAI Kimi Latest  From gateway
10. Google Gemini Flash…    From gateway
```

Pick entry 8 and the next turn runs on Grok via OpenRouter. Pick entry 2 and it runs on your
Anthropic subscription. Nothing else about the session changes.

> [!warning]
> **Unsupported by design.** Anthropic's docs state plainly that they don't support routing
> Claude Code to non-Claude models through any gateway, and the CLI enforces it (see
> [How it works](#how-it-works)). This works by writing a cache file the picker reads. It
> depends on internals of a specific build and **can break with no error message** on any
> update — the picker just quietly goes back to showing six entries. Use it accordingly.

Two files, ~350 lines, Python standard library only. No dependencies.

---

## How it routes

One rule, on the `model` field of each request:

| Model id | Upstream | Credential |
|---|---|---|
| bare — `claude-opus-5` | `api.anthropic.com` | whatever the client sent, **forwarded untouched** |
| contains `/` — `~x-ai/grok-latest` | `openrouter.ai` | `~/.config/openrouter-key` |

Your subscription token is never read, stored, or logged. It is relayed verbatim on the
Anthropic path, and **stripped** on the OpenRouter path — `Authorization`, `x-api-key` and
`anthropic-beta` are removed and replaced with the OpenRouter bearer.

Two transforms make non-Claude models work at all:

- **Field trimming.** Claude Code sends Anthropic-only fields (`thinking`,
  `context_management`, …) that other models reject with a 400. OpenRouter-bound requests are
  filtered to a portable allowlist and every `cache_control` marker is stripped recursively.
- **Provider preferences.** `zdr: true` restricts to zero-data-retention providers;
  `sort: "throughput"` picks the fastest of those.

Each request appends one JSON object to `requests.log` — upstream, model requested vs. model
actually served, latency, tokens, and cost when reported.

## How it works

Claude Code *does* support gateway models in the picker, via
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`. Setting it alone does nothing, because two
independent gates stop it, neither of which prints a visible error:

**1. Discovery never fires.** Before fetching, the CLI requires `ANTHROPIC_AUTH_TOKEN`, an API
key, or an `apiKeyHelper`. A claude.ai subscription is OAuth and supplies none, so it logs
`[gatewayDiscovery] skipped: no credential` and never issues `GET /v1/models`. Your gateway is
never contacted.

**2. The model-name filter.** Had it fetched, the response is filtered:

```js
B = data.filter(m => /(claude|anthropic)/i.test(m.id));
if (B.length === 0) { /* "0 usable models after filter" */ return }
```

No third-party id contains `claude` or `anthropic`, so every model is dropped and no cache is
written.

**The way through:** the picker does not call the gateway. It reads
`~/.claude/cache/gateway-models.json`, and its reader applies *neither* gate — it needs only
the env flag, a first-party provider, and `baseUrl` matching `ANTHROPIC_BASE_URL`. So
`sync-models.py` writes that file directly:

```json
{"baseUrl": "http://127.0.0.1:8787",
 "fetchedAt": 1788221821567,
 "models": [{"id": "~x-ai/grok-latest", "display_name": "xAI: Grok Latest"}]}
```

`fetchedAt` is integer milliseconds; `display_name` is what the menu shows, so the ugly
`~vendor/slug` id can stay ugly. Only ids containing `/` are published — bare `claude-*` ids are
already in the picker natively and would duplicate every entry.

The cache is read **at startup**, so a newly surfaced model appears one launch later.

## Install

Requires Python 3.9+ and an [OpenRouter](https://openrouter.ai) key.

```sh
git clone https://github.com/lionel509/Switchboard.git ~/.local/share/claude-router
printf '%s' 'sk-or-v1-…' > ~/.config/openrouter-key && chmod 600 ~/.config/openrouter-key
```

Add to `~/.zshrc`:

```zsh
export ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
# which OpenRouter models appear in /model (comma-separated substring match)
export CLAUDE_ROUTER_MODELS="grok-latest,gemini-flash-latest,glm-latest,kimi-latest"

claude() {
  nc -z 127.0.0.1 8787 2>/dev/null || {
    (CLAUDE_ROUTER_MODELS="$CLAUDE_ROUTER_MODELS" \
     python3 ~/.local/share/claude-router/router.py \
       >/dev/null 2>>~/.local/share/claude-router/router.log &)
    sleep 0.5
  }
  # Publish the picker cache. Backgrounded: it is read at startup, so a change
  # to CLAUDE_ROUTER_MODELS lands one launch later rather than costing ~2s every start.
  (python3 ~/.local/share/claude-router/sync-models.py \
     >/dev/null 2>>~/.local/share/claude-router/router.log &)
  command claude "$@"
}
```

Then `exec zsh`, launch `claude`, and **restart it once more** so the picker reads the cache.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_BASE_URL` | — | must point at the router |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | — | must be `1` |
| `CLAUDE_ROUTER_MODELS` | `grok` | substrings of OpenRouter ids to surface |
| `CLAUDE_ROUTER_PORT` | `8787` | listen port |
| `CLAUDE_ROUTER_LOG` | `~/.local/share/claude-router/requests.log` | request log path |

## Troubleshooting

**The picker shows only the built-in models.** Check the cache exists and its `baseUrl` matches
your `ANTHROPIC_BASE_URL` exactly:

```sh
cat ~/.claude/cache/gateway-models.json
python3 sync-models.py          # prints what it published, or why it didn't
```

**A gateway model returns 404 or an empty response.** `zdr: true` restricts to zero-data-retention
providers; some models have none. Loosen `DEFAULT_PREFS` in `router.py`.

**Diagnosing anything else** — run a second instance on another port and point one throwaway
client at it, rather than reading logs from the instance your live session depends on:

```sh
CLAUDE_ROUTER_PORT=8788 python3 router.py > /tmp/probe.log 2>&1 &
ANTHROPIC_BASE_URL=http://127.0.0.1:8788 claude -p "say ok" --model haiku
cat /tmp/probe.log      # exactly which paths the client requested
```

That is what proved `GET /v1/models` was never being sent — something no amount of reading the
router could establish, because the bug was in the caller.

> On macOS, `timeout` does not exist; it is `gtimeout` from coreutils. Three probe runs returned
> empty and looked like evidence before that surfaced.

## License

MIT
