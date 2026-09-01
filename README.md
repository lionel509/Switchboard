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

## Auto

An extra picker entry, **"Auto — routed per task"**. Rather than defaulting to something cheap,
Auto spends one call on a **cheap router model** that reads the task and names the model best
suited to it — choosing across the whole catalog, **your Claude models included**.

That last part is what makes it work. The router weighs two currencies that do not trade against
each other: Claude models cost **no cash but consume plan quota**, and OpenRouter models cost
**cash but no quota**. So mundane bulk work goes to the cheapest paid model to preserve quota,
and anything needing reliable tool use goes to Claude, where it is free at the margin.

Measured picks, `~deepseek/deepseek-v4-flash-latest` routing at ~2 s and ~$0.00002 a call:

| Task | Picked | Costs |
|---|---|---|
| rename `.txt` → `.md`, fix links | DeepSeek Flash | $0.05/1M |
| reformat a 900-line JSON file | DeepSeek Flash | $0.05/1M |
| what does `chmod 755` mean | Claude Haiku | quota |
| debug a null deref in auth middleware | Claude Sonnet | quota |
| refactor auth across 12 files until tests pass | Claude Sonnet | quota |
| prove the bound is tight, implement in Rust | Claude Opus | quota |
| reconcile three 200-page lab PDFs | Claude Opus | quota |

Only the **last user message, truncated to 2000 characters**, is sent to the router model — it
needs the gist, and it is a third party that does not need the whole conversation.

Two design decisions, both load-bearing:

**The decision is made once per conversation, not per request, and never downgrades.** Claude
Code's system prompt plus tool schemas is large — a real turn in this repo's own session logged
`"cache_read": 71346`. Cached, those 71k tokens are nearly free on every turn after the first.
Switching providers is a **full cache miss at full input price**, so a router that re-decides
each turn can lose more on cache misses than it saves on cheap tokens. Auto pins its choice to a
conversation key derived from the opening turn, and escalation is one-way.

**Escalation is a safety net, and it is structural rather than semantic.** Picking the model is
the router model's job; escalation only catches a session that *became* real work after that
decision was made — more than 3 tools offered, more than 6 messages deep, or a prompt past
`CLAUDE_ROUTER_AUTO_CHARS`. It escalates to `fallback` and never comes back down.

`fallback` also absorbs every failure: router model unreachable, a reply naming nothing in the
catalog, no OpenRouter key. Auto degrades to a working model rather than to an error.

> [!tip]
> Cheap models worth using as the router are **reasoning** models, and a routing decision does
> not need reasoning. Left on, the entire `max_tokens` budget goes to thinking tokens and the
> reply comes back `stop_reason: max_tokens` with no text at all. OpenRouter's own
> `{"reasoning": {"enabled": false}}` is **not** honoured on the Messages endpoint — the
> Anthropic-style `{"thinking": {"type": "disabled"}}` is, and it cut the call from 2.8 s to
> 1.4 s. Ids also come back without their leading `~` about half the time, so matching ignores it.

> [!note]
> **On a subscription, this saves quota rather than money.** Anthropic models are already paid
> for and free at the margin; OpenRouter models cost real cents. Auto is worth it because
> mundane turns stop burning Claude rate limit you would rather spend on real work — not
> because it is cheaper. That is also why `CLAUDE_ROUTER_AUTO_STRONG` defaults to an Anthropic
> model: there is no reason to pay OpenRouter for the hard turns.

## Models

`models.json` is the catalog: what appears in the picker, and what Auto may choose from. Manage
it with `switchboard.py`, which looks each id up on OpenRouter so the catalog cannot drift into
models that do not exist:

```sh
python3 switchboard.py list
python3 switchboard.py add ~google/gemini-pro-latest --good-at "long documents, large codebases"
python3 switchboard.py add qwen/qwen3.8-flash --no-zdr --good-at "cheap agentic and coding work"
python3 switchboard.py remove qwen/qwen3.8-flash
python3 switchboard.py sync          # publish to the picker, then restart Claude Code
```

`add` fills in name, price, context and a guessed family/tier from OpenRouter. Restart the router
and re-run `sync` for a change to reach the picker.

Anthropic ids are not on OpenRouter, so they are added with `--subscription`, which skips the
lookup and marks them as costing quota rather than cash:

```sh
python3 switchboard.py add claude-opus-5 --subscription --good-at "hardest reasoning, large refactors"
```

Those entries are **never published to the picker** — Claude Code already lists them natively, and
publishing them again would duplicate every Claude row. They live in the catalog purely so Auto
can pick them.

**Tiers work like Claude's levels.** Each entry carries a `family` and a `tier`, so a family can
appear at more than one strength — Gemini Pro alongside Gemini Flash, DeepSeek Pro alongside
DeepSeek Flash — and the picker lists them as siblings. Claude Code's picker is a flat list with
no submenus, so this is naming convention rather than nesting, but it reads the same way and Auto
uses `tier` and `price` when choosing.

### Keeping the menu short

Claude Code always shows its own six Claude rows, and past ten the picker starts hiding the rest
behind a `… +N models` line. One row per model variant overflows it immediately.

So the picker publishes **family rows**. `~fam/deepseek` is one entry, "DeepSeek (flash/pro)",
and it resolves its own tier per task the same way Auto does — restricted to that family:

```
 7. DeepSeek (flash/pro)      From gateway
 8. Google Gemini (flash/pro) From gateway
 9. Kimi K3                   From gateway
10. Auto — routed per task    From gateway
```

| Row | Task | Resolves to |
|---|---|---|
| DeepSeek | rename all `.txt` files | `~deepseek/deepseek-v4-flash-latest` |
| DeepSeek | prove this bound is tight | `deepseek/deepseek-v4-pro` |
| Gemini | summarise this paragraph | `~google/gemini-flash-latest` |
| Gemini | read three 200-page PDFs | `~google/gemini-pro-latest` |

`picker.families` and `picker.models` in `models.json` control **only menu length**. Everything in
the catalog stays available to Auto whether it is listed or not — Qwen, Grok and GLM are all still
routable while not taking up a row.

> [!note]
> **There is no arrow-key variant switch.** The binary has keybindings
> `modelPicker:decreaseEffort` and `modelPicker:increaseEffort` and no variant equivalent — the
> `←/→` axis is the effort slider and nothing else. Effort cannot be repurposed either:
> `CLAUDE_EFFORT` at low, high and xhigh all send an identical body
> (`thinking.budget_tokens: 31999`), so there is no signal for the router to read. The picker
> does have a search, so a long menu stays navigable.

### Replacing the whole menu (better than family rows)

Family rows work around the ten-row limit by collapsing variants. But the limit only bites
because six Claude rows are fixed — and they need not be. Claude Code has a `modelPicker`
setting, whose schema the binary gives as:

```
an object with an "options" array of { model, label?, description? } rows
```

With `replaceBuiltInOptions: true` the lineup **is** the menu, so dropping Claude rows you never
use buys room for every gateway variant as its own selectable row:

```sh
python3 switchboard.py apply            # write the lineup
python3 switchboard.py apply --revert   # restore the built-in menu
```

```
 1. Opus (1M)        4. DeepSeek Flash    7. Google Gemini Pro
 2. Sonnet           5. DeepSeek Pro      8. Kimi K3
 3. Haiku            6. Google Gemini Flash   9. Auto
```

Nine rows, no overflow, every variant directly selectable. Edit `claude_rows` and `apply_models`
in `models.json` to change the lineup; anything omitted is still routable by Auto. The previous
settings are copied to `settings.json.bak` first.

`replaceBuiltInOptions` hides gateway-discovered rows, so with a lineup applied the `sync` cache
no longer drives the menu — the lineup does. Verified that Claude Code starts clean with gateway
ids in it.

### Specialties

A `specialties` list is a **measured** strength, and it is the highest-priority rule when Auto
routes — above the cheap-for-bulk rule and above preferring a free subscription model. The point
is that a benchmarked win at the actual job beats saving money:

```sh
python3 switchboard.py add moonshotai/kimi-k3 \
  --specialty frontend --specialty ui --specialty css --specialty react \
  --good-at "building user interfaces - React, CSS, layout, data visualisation" \
  --source "Arena WebDev #1 (1679) Jul 2026; Vercel Next.js eval tied #1 at 92%"
```

Kimi K3 ships with that entry. It opened **#1 on Arena's WebDev leaderboard** at 1679, ahead of
Claude Fable 5 (1631) and GPT-5.6 Sol (1618), first in 6 of 7 frontend domains, and independently
tied first on Vercel's Next.js eval at 92%. So "build a React dashboard" routes to K3 even though
Sonnet is free — and at $3/$15 per 1M that is a **deliberate decision to spend cash for a better
result**, not an accident.

> [!important]
> Because a specialty outranks everything, an unfounded one quietly misroutes work and bills you
> for it. `--source` exists to keep each claim answerable to something. Add a specialty for a
> strength you can point at evidence for, not one you have a hunch about — and re-check them,
> since leaderboards move and these entries do not.

Verified not to over-trigger: with K3 in the catalog, "write a Python script to parse this CSV",
"refactor auth across 12 files" and "rename all the .txt files" all still route elsewhere.

> [!warning]
> **Zero-data-retention is per model, and it is not free.** The router asks OpenRouter for ZDR
> providers by default. A model whose entry says `"zdr": false` has **no** ZDR provider at all —
> requesting one returns no candidates and the request simply fails — so for those the constraint
> is dropped and the prompt does reach a provider that may retain it. Those entries are labelled
> **"(no ZDR)"** in the picker so the trade is made knowingly. Qwen Flash is the one shipped that
> way: a single Alibaba endpoint, no ZDR option.
>
> ZDR also costs money on models that *do* offer it. DeepSeek Pro's cheapest providers
> (StreamLake at $0.66/1M, GMICloud, Alibaba) are all non-ZDR, so insisting on ZDR lands you
> around $1.30/1M — roughly double. `switchboard.py list` shows the price you are actually
> choosing.

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
| `CLAUDE_ROUTER_PORT` | `8787` | listen port |
| `CLAUDE_ROUTER_LOG` | `~/.local/share/claude-router/requests.log` | request log path |
| `CLAUDE_ROUTER_AUTO_ROUTER` | `models.json` `router_model` | cheap model that picks for Auto |
| `CLAUDE_ROUTER_AUTO_FALLBACK` | `models.json` `fallback` | used on escalation or any failure |
| `CLAUDE_ROUTER_AUTO_CHARS` | `24000` | prompt size that forces escalation |
| `CLAUDE_ROUTER_MODELS` | `grok` | legacy substring filter, used only if `models.json` is missing |

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
