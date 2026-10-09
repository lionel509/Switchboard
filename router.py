#!/usr/bin/env python3
"""Route Claude Code requests by model name, so /model switches providers.

  model has no "/"    ->  api.anthropic.com, forwarding whatever credential the
                          client sent, untouched. Your claude.ai subscription.
  vendor in providers ->  that vendor's own Anthropic-compatible endpoint, on its
                          own key. For outside *subscriptions* — Kimi For Coding
                          and friends: flat monthly fee, no cash per token.
  model has a "/"     ->  openrouter.ai (e.g. x-ai/grok-4.6), using the key in
                          ~/.config/openrouter-key. Metered per token.

Nothing is stored. The claude.ai token is forwarded on the Anthropic path only,
and is stripped on both third-party paths — it never leaves this machine toward
OpenRouter or a vendor endpoint.
"""
import base64, hashlib, http.client, json, os, re, shutil, subprocess, sys, threading, time, unicodedata, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT     = int(os.environ.get("CLAUDE_ROUTER_PORT", "8787"))
KEYFILE  = os.path.expanduser("~/.config/openrouter-key")
# A second OpenRouter key whose workspace has no zero-data-retention guardrail.
# Only models flagged zdr:false are sent with it -- see or_key().
NONZDR_KEYFILE = os.path.expanduser("~/.config/openrouter-key-nonzdr")
# /gemma/or only: its own key with a $2/day OpenRouter credit limit. No fallback
# to KEYFILE -- falling back would bypass the cap the key exists for.
GEMMA_KEYFILE = os.path.expanduser("~/.config/openrouter-key-gemma")
LOGFILE  = os.path.expanduser(os.environ.get(
    "CLAUDE_ROUTER_LOG", "~/.local/share/claude-router/requests.log"))
# substrings of OpenRouter model ids to surface in /model
SURFACE  = [s for s in os.environ.get("CLAUDE_ROUTER_MODELS", "grok").split(",") if s]

# --- Catalog ---------------------------------------------------------------
# models.json is the source of truth for what appears in /model and what Auto
# may choose from. Edit it with switchboard.py. If it is missing we fall back to
# the old CLAUDE_ROUTER_MODELS substring behaviour so nothing breaks.
# Gemma 4 Kaggle harness (swegemma) pass-through. The harness speaks OpenAI
# /v1/chat/completions and always sends the competition model name, so the route
# is picked by URL path, not model: point MODEL_PROXY_URL at one of
#   http://127.0.0.1:8787/gemma/local/v1  -> llama-server (Gaming PC, via tunnel)
#   http://127.0.0.1:8787/gemma/or/v1     -> OpenRouter, strict ZDR key,
#                                            google/gemma-4-31b-it, $0.09/$0.34 per 1M
# Competition Data may not leave his hardware (rules 2.4.b), so /local never fails
# over to anything: if llama-server is down the request errors.
GEMMA_LOCAL = os.environ.get("CLAUDE_ROUTER_GEMMA_LOCAL", "http://127.0.0.1:8000")
GEMMA_OR_MODEL = "google/gemma-4-31b-it"


# Pinned to Reka ($0.08/$0.30 per 1M, the cheapest of 15 providers on 2026-09-28)
# with no fallback, by Lionel's call: an outage fails the call instead of moving
# to a pricier provider. zdr stays as a guard: if Reka stops qualifying, calls fail
# rather than reaching a provider that retains.
GEMMA_OR_PROVIDER = {"only": ["reka"], "allow_fallbacks": False, "zdr": True}


# /gemma/kaggle: a fast, free stand-in for Gemma while iterating on the agent
# config (#28). Gemini 3.1 Flash-Lite through Kaggle's Model Proxy, which a local
# token can reach; Gemma 4 it cannot. The competition serves Gemma with a 32K
# context, and Flash-Lite takes 1M, so the stand-in refuses what vLLM would --
# otherwise a config could win here only by using histories Kaggle never allows.
GEMMA_KAGGLE_MODEL = "google/gemini-3.1-flash-lite-preview"
GEMMA_CONTEXT = 32768


def gemma_kaggle_body(req):
    """The harness request for the stand-in, or ValueError if it wouldn't fit Gemma."""
    prompt = len(json.dumps(req.get("messages", [])) + json.dumps(req.get("tools", []))) // 4
    need = prompt + (req.get("max_tokens") or 0)
    if need > GEMMA_CONTEXT:
        raise ValueError("This model's maximum context length is %d tokens. However, you requested "
                         "~%d tokens (~%d in the messages, %d for the completion)."
                         % (GEMMA_CONTEXT, need, prompt, req.get("max_tokens") or 0))
    out = {k: v for k, v in req.items() if k != "temperature"}     # the proxy rejects it
    out["model"] = GEMMA_KAGGLE_MODEL
    return out


def gemma_error_text(data):
    """An upstream's error message, from OpenAI-style, Kaggle-style or plain bodies."""
    try:
        d = json.loads(data)
        e = d.get("error") if isinstance(d.get("error"), dict) else d
        msg = e.get("message") or json.dumps(d)
    except (ValueError, AttributeError):
        msg = data.decode(errors="replace") if isinstance(data, bytes) else str(data)
    return msg[:300]


def gemma_or_body(req):
    """The harness request as sent to OpenRouter: the real model name, and the
    CHEAPEST zero-retention provider. Providers of this one model ranged
    $0.08-$0.75/1M input on 2026-09-28, and left to OpenRouter 42% of calls
    landed above $0.09 -- a quarter of the day's spend for the same tokens."""
    return dict(req, model=GEMMA_OR_MODEL, provider=GEMMA_OR_PROVIDER)

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")


def load_catalog():
    try:
        with open(CATALOG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


CATALOG      = load_catalog()
CANDIDATES   = [m for m in CATALOG.get("models", []) if m.get("id")]
BY_ID        = {m["id"]: m for m in CANDIDATES}

# --- Outside subscriptions -------------------------------------------------
# A vendor with its own Anthropic-compatible endpoint, billed by its own plan
# instead of per token. Keyed by the first path segment of the model id, so
# "kimi/k3" routes here while "moonshotai/kimi-k3" still goes to OpenRouter --
# the same model, bought two different ways, and they must stay distinguishable.
#
# Ids are deliberately split in two. The picker id stays bracket-free, because
# "[1m]" is Claude Code's own syntax for a context variant and it rewrites ids
# carrying it; upstream_id is what actually goes on the wire.
PROVIDERS    = CATALOG.get("providers", {})
UPSTREAM_ID  = {m["id"]: m["upstream_id"] for m in CANDIDATES if m.get("upstream_id")}

# --- Auto ------------------------------------------------------------------
# A pseudo-model in the picker. Rather than defaulting to something cheap, Auto
# spends one call on a *cheap router model* that reads the task and names the
# model best suited to it.
#
# The decision is made ONCE PER CONVERSATION, not per request, and escalation is
# one-way. Reason: Claude Code's system prompt + tool schemas is 10-25k tokens,
# and prompt caching makes turn 2+ nearly free (a real turn here logged
# cache_read=71346). Every provider switch is a full cache miss at full input
# price, so a per-request router loses more on cache misses than it saves.
AUTO_MODEL   = "~auto/auto"
ROUTER_MODEL = os.environ.get("CLAUDE_ROUTER_AUTO_ROUTER",
                              CATALOG.get("router_model", ""))
# Used when the router model is unavailable, returns nonsense, or a session
# escalates. Defaults to Anthropic: free at the margin on a subscription.
FALLBACK     = os.environ.get("CLAUDE_ROUTER_AUTO_FALLBACK",
                              CATALOG.get("fallback", "claude-sonnet-5"))
# Escalate above this many characters of serialised prompt (~4 chars/token).
AUTO_ESCALATE_CHARS = int(os.environ.get("CLAUDE_ROUTER_AUTO_CHARS", "24000"))
# Only this much of the task is shown to the router model — it needs the gist,
# and this is a third party that does not need the whole conversation.
AUTO_TASK_CHARS = 2000

_auto_pins = {}                 # conversation key -> resolved model id
_auto_lock = threading.Lock()

HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
       "proxy-authenticate", "proxy-authorization", "te", "trailers"}

# Standard Messages API fields. Claude Code also sends Anthropic-only extras
# (thinking, context_management, ...) that non-Claude models reject with a 400,
# so requests bound for OpenRouter are trimmed to these.
PORTABLE = {"model", "messages", "system", "max_tokens", "metadata",
            "stop_sequences", "stream", "temperature", "top_k", "top_p",
            "tools", "tool_choice"}

# Default reasoning budget for OpenRouter models, in output tokens, used when a
# request carries no effort level. When it does, the ←/→ slider scales this via
# EFFORT_BUDGETS below -- the slider adjusts the chosen model's effort rather
# than swapping models (ARROW_MODEL still does that, for anyone who wants it).
# Thinking tokens bill as output tokens, so this is a real cost lever.
THINKING_BUDGET = int(os.environ.get("CLAUDE_ROUTER_THINKING", "12000"))

# Per-level thinking budgets for the ←/→ slider. Without a level the flat
# THINKING_BUDGET above applies, which is what every request used to get.
EFFORT_BUDGETS = CATALOG.get("effort_budgets", {})
# OpenRouter's reasoning.effort only has three stops, so the five map onto them.
OR_EFFORT = {"low": "low", "medium": "medium", "high": "high",
             "xhigh": "high", "max": "high"}


def strip_cache_control(node):
    """Remove every cache_control marker, recursively. Anthropic-only."""
    if isinstance(node, dict):
        node.pop("cache_control", None)
        for v in node.values():
            strip_cache_control(v)
    elif isinstance(node, list):
        for v in node:
            strip_cache_control(v)


def effort_of(req):
    """The ←/→ slider's level, or None. Claude Code sends it as output_config."""
    return (req.get("output_config") or {}).get("effort")


def sanitize(body):
    """Drop Anthropic-only fields, and cache_control markers, for other models."""
    try:
        req = json.loads(body)
    except Exception:
        return body
    model = req.get("model", "")
    # Read the slider BEFORE trimming: output_config is Anthropic-only, so the
    # allowlist below drops it, and the level has to survive as a thinking budget.
    effort = effort_of(req)
    req = {k: v for k, v in req.items() if k in PORTABLE}
    req["provider"] = prefs_for(model)

    strip_cache_control(req)

    # Restore reasoning. `thinking` is dropped above as an Anthropic-only field,
    # but OpenRouter honours exactly that one — measured on deepseek-v4-pro:
    # no field at all gives thinking_tokens=0, {"reasoning": {"effort": "high"}}
    # also gives 0 (silently ignored), and an explicit budget gives 222. So
    # without this these models run with NO reasoning whatsoever.
    # Anthropic requires 1024 <= budget_tokens < max_tokens.
    #
    # The slider chooses how much of that budget to spend. Without it every
    # model ran at a flat THINKING_BUDGET regardless of where the dial sat,
    # which is what made ←/→ look broken on a gateway row.
    mt = req.get("max_tokens") or 0
    # A model can set its own budget ("thinking_budget"); 0 turns thinking off
    # for it, slider or not.
    base = (BY_ID.get(model) or {}).get("thinking_budget", THINKING_BUDGET)
    want = 0 if base == 0 else (EFFORT_BUDGETS.get(effort, base) if effort else base)
    budget = min(want, mt - 1024)
    if budget >= 1024:
        req["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if effort:
            # OpenRouter's own documented lever, accepted alongside the Anthropic
            # one; whichever the upstream honours wins.
            req["reasoning"] = {"effort": OR_EFFORT.get(effort, "medium")}
    else:
        # Anthropic rejects a budget under 1024, so the honest encoding of "no
        # room to think" is off, not an invalid budget.
        req["thinking"] = {"type": "disabled"}
    return json.dumps(req).encode()


# OpenRouter provider routing. Keys are matched as substrings of the model id;
# DEFAULT applies to anything unmatched. zdr=True restricts to zero-data-retention
# providers; sort="throughput" picks the fastest of those.
PROVIDER_PREFS = {
    "gemini": {"zdr": True, "order": ["google-vertex/global"]},
    # Bulk audit worker: cheapest ZDR provider, not fastest. Floor $0.045/$0.14
    # (inference.net) if it qualifies; the ZDR pack lists at $0.15/$0.50.
    "glm-5.3-flash": {"zdr": True, "sort": "price"},
}
DEFAULT_PREFS = {"zdr": True, "sort": "throughput"}


def log_request(rec):
    """One JSON object per request, appended to requests.log."""
    try:
        with open(LOGFILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def read_usage(payload):
    """Pull usage/provider from a response: whole-body JSON first, then SSE tail."""
    text = payload.decode("utf-8", "replace")
    try:                                    # non-streaming (may still be chunked)
        d = json.loads(text)
        return d.get("usage") or {}, d.get("provider"), d.get("model")
    except Exception:
        pass

    usage, provider, model = {}, None, None
    for line in text.splitlines():          # streaming: last usage wins
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        for src in (ev, ev.get("message") or {}):
            if src.get("usage"):
                usage.update(src["usage"])
            provider = src.get("provider") or provider
            model = src.get("model") or model
    return usage, provider, model


def prefs_for(model):
    prefs = None
    for frag, p in PROVIDER_PREFS.items():
        if frag in (model or "").lower():
            prefs = dict(p)
            break
    if prefs is None:
        prefs = dict(DEFAULT_PREFS)
    # A model whose catalog entry says zdr:false has NO zero-data-retention
    # provider. Asking for one returns no candidates and the request just fails,
    # so for those the choice is to send it or not to use the model at all.
    # The picker labels them "(no ZDR)" so the trade is made knowingly.
    entry = BY_ID.get(model or "")
    if entry is not None and entry.get("zdr", True) is False:
        prefs.pop("zdr", None)
    return prefs


def conv_key(req):
    """Stable id for a conversation, from its opening turn.

    Requests carry no session id, but the first user message and the head of the
    system prompt are fixed for the life of a conversation and differ between
    them. Good enough to pin a routing decision to.
    """
    msgs = req.get("messages") or []
    first = json.dumps(msgs[0], sort_keys=True) if msgs else ""
    system = req.get("system")
    if isinstance(system, list):
        system = json.dumps(system[:1], sort_keys=True)
    return hashlib.sha256((str(system)[:2000] + first[:2000]).encode()).hexdigest()[:16]


def looks_hard(req):
    """Escalation signals from the request itself — the safety net, not the picker.

    Deliberately not 'is this question difficult'; that is what the router model
    is for. These only catch a session that has *become* real work after the
    routing decision was already made.
    """
    if len(req.get("tools") or []) > 3:
        return "tools"
    if len(req.get("messages") or []) > 6:
        return "depth"
    if len(json.dumps(req.get("messages") or [])) > AUTO_ESCALATE_CHARS:
        return "context"
    return None


def task_text(req):
    """The last user message, flattened to text and truncated."""
    for m in reversed(req.get("messages") or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c[:AUTO_TASK_CHARS]
        if isinstance(c, list):
            parts = [b.get("text", "") for b in c if isinstance(b, dict)]
            return " ".join(p for p in parts if p)[:AUTO_TASK_CHARS]
    return ""


ROUTER_PROMPT = """You pick which model should handle a task. Reply with ONE model id \
from the list and nothing else.

Models:
%s

There are two currencies here, and they do not trade against each other:
- [subscription] models cost NO money. They consume a limited monthly quota, and \
they are the most reliable at multi-step tool use.
- Priced models cost real cash per token but consume no quota.

Some models list SPECIALTY tags. Those are measured, benchmarked strengths, not \
general descriptions.

Rules, in order:
1. If the task falls squarely inside a model's SPECIALTY, pick that model - even \
over a [subscription] model, and even if it is expensive. A measured win at the \
actual job beats saving money. Only apply this when the task really is that kind \
of work, not merely adjacent to it.
2. Real coding, multi-step tool use, debugging, or anything where a wrong answer \
costs time goes to a [subscription] model. They are free at the margin, so paying \
cash for this would be strictly worse.
3. Bulk mechanical work - renames, greps, reformatting, simple mechanical edits - \
goes to the CHEAPEST priced model that covers it. This is the whole point: it \
preserves quota for work that needs it, at a price you can predict. NEVER send \
mechanical work to an expensive priced model.
4. If a priced model is right but none of the descriptions clearly fits the task, \
send it to openrouter/auto, which delegates the choice to OpenRouter's own task \
classifier. Use it when unsure - not for work rule 3 already covers, because a \
delegated choice can land on an expensive model.
5. Name one specific priced model when its description matches the task closely - \
a particular modality, or context far beyond the others.

Task:
%s

Model id:"""


def ask_router_model(task, cands=None):
    """One cheap call to name the best model for this task. None on any failure."""
    if cands is None:
        cands = CANDIDATES
    if not ROUTER_MODEL or not cands or not task:
        return None
    key = or_key()
    if not key:
        return None
    def cost(m):
        if m.get("billing") == "subscription":
            return "[subscription]"
        p = m.get("price", ["?", "?"])
        if isinstance(p[0], (int, float)) and p[0] < 0:
            return "[priced by whatever it selects]"
        return "$%s/$%s per 1M" % (p[0], p[1])

    def line(m):
        s = "%s | %s | %s" % (m["id"], cost(m), m.get("good_at", ""))
        if m.get("specialties"):
            s += " SPECIALTY: %s." % ", ".join(m["specialties"])
        return s

    # Group and order the list rather than relying on a rule to convey price.
    # Told only in prose, "pick the cheapest" loses to a mid-priced model whose
    # description sounds apt; shown as an ordering, it holds.
    subs = [m for m in cands if m.get("billing") == "subscription"]
    paid = sorted([m for m in cands if m.get("billing") != "subscription"],
                  key=lambda m: (m.get("price") or [0])[0] if
                  (m.get("price") or [0])[0] >= 0 else 1e9)
    parts = []
    if subs:
        parts.append("NO CASH COST (consume plan quota):\n"
                     + "\n".join(line(m) for m in subs))
    if paid:
        parts.append("COST CASH (listed cheapest first):\n"
                     + "\n".join(line(m) for m in paid))
    listing = "\n\n".join(parts) if parts else "\n".join(line(m) for m in cands)
    body = json.dumps({
        # The cheap models worth using here are reasoning models, and a routing
        # decision does not need reasoning. Left on, the whole max_tokens budget
        # goes to thinking tokens and the reply comes back stop_reason=max_tokens
        # with no text at all. OpenRouter's own {"reasoning": {"enabled": false}}
        # is NOT honoured on this endpoint; the Anthropic-style field is.
        "model": ROUTER_MODEL, "max_tokens": 200, "temperature": 0,
        "thinking": {"type": "disabled"},
        "provider": prefs_for(ROUTER_MODEL),
        "messages": [{"role": "user", "content": ROUTER_PROMPT % (listing, task)}],
    }).encode()
    try:
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=20)
        conn.request("POST", "/api/v1/messages", body=body, headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"})
        payload = json.loads(conn.getresponse().read())
        conn.close()
    except Exception:
        return None
    blocks = [b for b in payload.get("content", []) if isinstance(b, dict)]
    text = " ".join(b.get("text") or b.get("thinking") or "" for b in blocks)
    # Match, don't parse. The reply drifts in three ways depending on which
    # provider throughput-sorting landed on: it drops the leading "~" of an alias
    # roughly half the time, sometimes gives only the slug, and sometimes the
    # display name. Longest id first so a short id cannot shadow a longer one.
    # Thinking text is included for when the budget ran out before any text.
    flat = text.replace("~", "").lower()
    cands = sorted(cands, key=lambda x: -len(x["id"]))
    for m in cands:                                  # full id
        if m["id"].replace("~", "").lower() in flat:
            return m["id"]
    for m in cands:                                  # bare slug
        slug = m["id"].split("/")[-1].lower()
        if len(slug) > 6 and slug in flat:
            return m["id"]
    for m in cands:                                  # display name
        name = (m.get("name") or "").lower()
        if len(name) > 4 and name in flat:
            return m["id"]
    return None


def resolve_auto(req, pol=None):
    """Map the auto pseudo-model onto a real one. One-way: never downgrades.

    The folder's data policy narrows what Auto may choose from rather than
    refusing its pick afterwards. Auto exists to land on something workable, so
    a policy that rules a model out should steer the choice, not 403 the turn.
    """
    key = conv_key(req)
    with _auto_lock:
        pinned = _auto_pins.get(key)
    if pinned == FALLBACK:
        return FALLBACK                  # escalation is permanent for this conversation
    if pinned and allowed(pinned, pol or {}):
        chosen = FALLBACK if looks_hard(req) else pinned
    elif looks_hard(req):
        chosen = FALLBACK                # already real work; nothing to decide
    else:
        # First turn: spend one cheap call working out what this task needs.
        # A provider can opt out of Auto ("auto": false): Kaggle's credit is for a
        # hand-pick, never for a router model to spend on its own.
        cands = [m for m in CANDIDATES if allowed(m["id"], pol or {})
                 and (BY_ID.get(m["id"]) or m).get("auto", True)
                 and (PROVIDERS.get(m["id"].split("/")[0]) or {}).get("auto", True)]
        chosen = ask_router_model(task_text(req), cands) or FALLBACK
    with _auto_lock:
        _auto_pins[key] = chosen
        if len(_auto_pins) > 512:        # bounded; oldest insertions drop first
            for k in list(_auto_pins)[:128]:
                _auto_pins.pop(k, None)
    return chosen


# --- Arrow-key model selection ---------------------------------------------
# The picker's ←/→ adjuster is the effort slider, and effort reaches the wire as
# output_config.effort ("low".."max"). A row declared with
# ANTHROPIC_CUSTOM_MODEL_OPTION_SUPPORTED_CAPABILITIES="effort,xhigh_effort,max_effort"
# gets that adjuster even though it is a gateway id — verified: selecting
# ~pick/openrouter sends output_config {"effort": "low"} / {"effort": "xhigh"}.
#
# So one picker row can carry a whole shortlist, with ←/→ choosing between them.
# Effort is spent as the selector rather than as effort, which is the right trade
# here: these models are always run at their highest reasoning anyway (see
# REASONING_EFFORT below), so there is nothing left for the slider to mean.
ARROW_MODEL = "~pick/openrouter"
ARROW_MAP   = CATALOG.get("arrow_models", {})
ARROW_ORDER = ["low", "medium", "high", "xhigh", "max"]

FAMILY_PREFIX = "~fam/"
FAMILY_LABELS = CATALOG.get("family_labels", {})
PICKER         = CATALOG.get("picker", {})


def family_of(fam):
    """Catalog entries in one family, cheapest first."""
    return sorted([m for m in CANDIDATES if m.get("family") == fam],
                  key=lambda m: (m.get("price") or [0])[0])


def resolve_family(req, fam, tier=None, pol=None):
    """One picker row per family; the tier is chosen per task.

    Claude Code's picker cannot put variants behind a row — the arrow-key adjust
    on the effort line is its own UI, not something a gateway entry can hook. So
    a family row resolves its own tier the way Auto does, restricted to that
    family, and the list stays one row per family instead of one per variant.

    A folder that names a tier settles it outright: no router call, no latency,
    and the answer is the same every turn. Default is pro, set per folder.
    """
    cands = [m for m in family_of(fam) if allowed(m["id"], pol or {})]
    if not cands:
        return FALLBACK
    if tier:
        want = [m for m in cands if m.get("tier") == tier]
        if want:
            return want[0]["id"]
    key = conv_key(req) + ":" + fam
    with _auto_lock:
        pinned = _auto_pins.get(key)
    # On failure take the family's top tier rather than its cheapest: the family
    # was chosen deliberately, so erring toward capable beats erring toward cheap.
    chosen = pinned or ask_router_model(task_text(req), cands) or cands[-1]["id"]
    with _auto_lock:
        _auto_pins[key] = chosen
    return chosen


def resolve_arrow(req, pol=None):
    """Map the picker's effort level onto a model. This is the arrow-key row.

    No conversation pin: the whole point is that ←/→ switches the model mid
    session, so the effort on each request is the live answer.
    """
    eff = (req.get("output_config") or {}).get("effort")
    chosen = ARROW_MAP.get(eff)
    if chosen and allowed(chosen, pol or {}):
        return chosen
    # Effort absent, unmapped, or ruled out by the folder: fall to the highest
    # level that is configured AND permitted here. The shortlist is ordered
    # weakest-to-strongest, so this keeps as much of the arrow's intent as the
    # policy allows instead of dropping straight to the fallback.
    for lvl in reversed(ARROW_ORDER):
        cand = ARROW_MAP.get(lvl)
        if cand and allowed(cand, pol or {}):
            return cand
    return FALLBACK


def picker_id(mid, entries=()):
    """The id Claude Code sees: bare, or carrying its own "[1m]" context variant.

    Claude Code does not know these ids, so it assumes a window for them --
    measured on 2.1.280 (2026-09-23): an unrecognized id gets
    CLAUDE_CODE_MAX_CONTEXT_TOKENS (256,000 here), not the window this catalog
    records, so a 1M model compacts at 256k. "[1m]" is the binary's own lever
    ("append [1m] to the model name for 1M"): it sets the window to 1,000,000,
    and it is stripped before the wire -- measured: "~fam/mimo[1m]" arrives as
    "~fam/mimo", "kimi/k3[1m]" as "kimi/k3". So the suffix lives only in what
    the picker publishes and never in models.json ids or upstream_id.

    A row takes its smallest member's window (flash must not inherit pro's),
    and Auto stays bare on purpose: it pins to a delegate of unknown size,
    haiku included, and a 1M window would let a 200k delegate be overpacked.
    """
    if mid == AUTO_MODEL:
        return mid
    windows = [m.get("context") or 0 for m in entries if m]
    if windows and min(windows) >= 1_000_000:
        return mid + "[1m]"
    return mid


def picker_rows():
    """What to publish to /model. Families collapse; the rest is opt-in.

    Everything in the catalog stays available to Auto whether it is listed here
    or not — this only controls how long the menu is.
    """
    # Auto leads: gateway rows land after the six built-in Claude rows and the
    # custom arrow row, so only the first few sit above the fold. Everything
    # after row ten is still reachable — the picker scrolls, and it has a search.
    rows = [{"id": AUTO_MODEL, "display_name": "Auto — routed per task"}]
    for fam in PICKER.get("families", []):
        members = family_of(fam)
        if not members:
            continue
        label = FAMILY_LABELS.get(fam, fam.title())
        seen, order = set(), []                   # dedupe, keep cheapest-first
        for m in members:
            t = m.get("tier", "?")
            if t not in seen:
                seen.add(t)
                order.append(t)
        tiers = "/".join(order)
        rows.append({"id": picker_id(FAMILY_PREFIX + fam, members),
                     "display_name": "%s (%s)" % (label, tiers)})
    for mid in PICKER.get("models", []):
        m = BY_ID.get(mid)
        if not m or "/" not in mid:
            continue
        label = m.get("name") or mid
        if m.get("zdr", True) is False:
            label += " (no ZDR)"
        rows.append({"id": picker_id(mid, [m]), "display_name": label})
    return rows


def read_key(path):
    """A credential from a file, or None. Never logged, never cached."""
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return None


def or_key(model=None):
    """The OpenRouter credential, picked by whether the model needs a non-ZDR route.

    An OpenRouter *workspace* carries the zero-data-retention guardrail, and a key
    belongs to a workspace — so which key is sent decides whether a non-ZDR
    provider is reachable at all. Keeping the permissive key on its own file means
    the strict key stays the default: a model has to be flagged zdr:false in the
    catalog to reach the permissive workspace, and the folder policy has already
    decided whether such a model may run here at all.

    Falls back to the strict key when no permissive one is installed, so the only
    cost of not having it is the guardrail refusal you would have had anyway.

    A model naming an "upstream" is sent with that upstreams row's key_file and
    nothing else: a missing row or file is None, never the main key, because a
    per-model key usually exists to cap what that model can spend.
    """
    if model:
        m = BY_ID.get(model) or {}
        if "upstream" in m:
            upstream_name = m["upstream"]
            upstream = next((u for u in CATALOG.get("upstreams", []) if u.get("name") == upstream_name), None)
            if upstream and "key_file" in upstream:
                return read_key(upstream["key_file"])
            return None

        if m.get("zdr", True) is False:
            return read_key(NONZDR_KEYFILE) or read_key(KEYFILE)
    return read_key(KEYFILE)


# --- credential renewal -------------------------------------------------
# The router still does not speak OAuth. It asks switchboard -- which owns the
# flow -- to mint a new access token, then re-reads the key file exactly as
# before. Without this a vendor token dying mid-run surfaces as a 401 the client
# can only retry into, which is what it was doing: an agent would work for
# fifteen minutes and then stop, signed out, with every retry equally dead.

RENEW_MARGIN = 300          # renew a token with less than this much life left
_renew_locks = {}
_locks_guard = threading.Lock()


def _renew_lock(vendor):
    with _locks_guard:
        return _renew_locks.setdefault(vendor, threading.Lock())


def expiring(key, margin=RENEW_MARGIN):
    """True if `key` is a JWT that is spent, or nearly.

    Only the payload is read, and only its `exp`. A credential that is not a JWT
    has no expiry to inspect -- those are left to the 401 path, which catches the
    same failure a few hundred milliseconds later.
    """
    try:
        parts = key.split(".")
        if len(parts) < 3 or not key.startswith("ey"):
            return False
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(pad)).get("exp")
        if exp is None:
            return False
        exp = float(exp)
        if exp > 1e11:                     # milliseconds
            exp /= 1000.0
        return exp - time.time() < margin
    except Exception:
        return False                       # unreadable is not expired


def renew_key(vendor, prov, stale):
    """A fresh access token for `vendor`, or None. Serialised per provider.

    The lock matters more than it looks. A refresh token is rotated on use, so
    two renewals racing means the second invalidates the first and both sessions
    are signed out -- exactly what a fan-out of parallel subagents would cause.
    Whoever waits re-reads the file first: if the key changed underneath, another
    thread already renewed and that token is used instead of burning a second.
    """
    if not prov.get("oauth"):
        return None                        # a static key cannot be renewed
    with _renew_lock(vendor):
        current = read_key(prov.get("key_file", ""))
        if current and current != stale:
            return current
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            from switchboard import oauth_refresh, save_oauth
        except Exception as e:
            sys.stderr.write("renew %s: switchboard unavailable (%s)\n" % (vendor, e))
            return None
        try:
            tok = oauth_refresh(vendor, prov)
            if not tok:
                return None                # reason already on stderr
            save_oauth(vendor, prov, tok)
        except Exception as e:
            sys.stderr.write("renew %s: %s\n" % (vendor, e))
            return None
        sys.stderr.write("renewed %s access token\n" % vendor)
        return tok.get("access_token")


# --- Minted tokens (Kaggle Model Proxy) --------------------------------------
# A provider with "token_env" gets its credential from an env file that a vendor
# CLI writes: KEY=VALUE lines carrying the URL, the key and an ISO expiry. Kaggle's
# `kaggle benchmarks auth` mints one that lasts an hour, so it is re-minted here
# once it has under RENEW_MARGIN left. The file is 0600 and is replaced whole, never
# appended to, so a stale key cannot outlive a fresh one further down the file.

def read_env(path):
    out = {}
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                k, eq, v = line.strip().partition("=")
                if eq and not k.startswith("#"):
                    out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def env_expires(env, field):
    try:
        t = env.get(field, "").replace("Z", "+00:00")
        import datetime
        return datetime.datetime.fromisoformat(t).timestamp()
    except Exception:
        return 0.0


def minted_token(vendor, prov):
    """(base_url, key) for a token_env provider, minting a new one if the file
    is missing or nearly spent. None if minting fails (reason on stderr)."""
    tok = prov["token_env"]
    path = os.path.expanduser(tok["file"])
    for attempt in (False, True):
        env = read_env(path)
        url, key = env.get(tok["url_var"]), env.get(tok["key_var"])
        if url and key and env_expires(env, tok["expiry_var"]) - time.time() > RENEW_MARGIN:
            return url.rstrip("/"), key
        if attempt:
            return None
        with _renew_lock(vendor):
            env = read_env(path)                 # another thread may have just minted
            if env_expires(env, tok["expiry_var"]) - time.time() > RENEW_MARGIN:
                continue
            tmp = path + ".minting"
            try:
                os.remove(tmp)
            except OSError:
                pass
            cmd = [os.path.expanduser(c).replace("{file}", tmp) for c in tok["mint"]]
            exe = shutil.which(cmd[0]) or next(
                (p for p in (os.path.expanduser("~/.local/bin/" + cmd[0]),
                             "/opt/homebrew/bin/" + cmd[0]) if os.path.exists(p)), cmd[0])
            try:
                old = os.umask(0o077)
                try:
                    r = subprocess.run([exe] + cmd[1:], capture_output=True, text=True, timeout=120)
                finally:
                    os.umask(old)
                if r.returncode or not os.path.exists(tmp):
                    sys.stderr.write("mint %s: exit %s %s\n" % (vendor, r.returncode, r.stderr[-300:]))
                    return None
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
                sys.stderr.write("minted a %s token\n" % vendor)
            except Exception as e:
                sys.stderr.write("mint %s: %s\n" % (vendor, e))
                return None
    return None


# --- Subscription quota ------------------------------------------------------
# A provider can declare "quota": {"path": ..., "interval": ...}. After serving
# (or being refused by) that vendor, the router polls the endpoint with the
# provider's own credential and rewrites <logdir>/<vendor>-limits.json beside
# requests.log. That is how the notch shows plan quota while holding no keys of
# its own: same folder as the log, so the one folder grant already covers it.
_quota_last = {}                    # vendor -> monotonic time of last attempt
_quota_lock = threading.Lock()


def quota_file(vendor):
    return os.path.join(os.path.dirname(LOGFILE), "%s-limits.json" % vendor)


def flatten_quota(payload):
    """Normalise a quota payload to the limits-file shape. None on an unknown
    shape, and the old file is left standing -- a stale meter beats a vanished
    one.

    Kimi (/coding/v1/usages) answers:

      {"limits": [{"window": {"duration": 300, ...}, "detail": {...}}],
       "usages": {"limit_5h":         {"used_ratio": 0.231, "reset_time": "..."},
                  "limit_month_total": {...},
                  "limit_month_code":  {...}}}

    Ratios become percents; reset times stay ISO strings. `month_code` is kept
    on record although the panel displays `month_total` -- they count different
    pools against different denominators, and the total is the one nearer its
    ceiling today, so it is the one that would actually run out first.
    """
    usages = payload.get("usages") or {}
    five = usages.get("limit_5h") or {}
    if five.get("used_ratio") is None:
        return None
    month = usages.get("limit_month_total") or {}
    month_code = usages.get("limit_month_code") or {}
    out = {"five_hour_pct": round(float(five["used_ratio"]) * 100, 1),
           "five_hour_resets_at": five.get("reset_time"),
           "month_pct": round(float(month.get("used_ratio") or 0) * 100, 1),
           "month_resets_at": month.get("reset_time")}
    if month_code.get("used_ratio") is not None:
        out["month_code_pct"] = round(float(month_code["used_ratio"]) * 100, 1)
        out["month_code_resets_at"] = month_code.get("reset_time")
    return out


def fetch_quota(vendor, force=False):
    """Poll one vendor's quota endpoint and rewrite its limits file.

    The number moves on the scale of hours, so the per-vendor throttle is finer
    than the display needs; setting the timestamp before the fetch also makes it
    the in-flight guard, so two threads cannot double-poll.
    """
    prov = PROVIDERS.get(vendor) or {}
    quota = prov.get("quota") or {}
    path = quota.get("path")
    if not path:
        return
    interval = int(quota.get("interval", 60))
    with _quota_lock:
        now = time.monotonic()
        if not force and now - _quota_last.get(vendor, 0) < interval:
            return
        _quota_last[vendor] = now
    key = read_key(prov.get("key_file", ""))
    if not key:
        return
    prefix = prov.get("auth_prefix", "")
    try:
        conn = http.client.HTTPSConnection(prov["host"], timeout=30)
        conn.request("GET", path, headers={
            prov.get("auth_header", "x-api-key"):
                (prefix + " " + key) if prefix else key})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status != 200:
            return
        flat = flatten_quota(json.loads(raw))
    except Exception:
        return
    if flat is None:
        return
    flat["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = quota_file(vendor) + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(json.dumps(flat) + "\n")
        os.replace(tmp, quota_file(vendor))
    except OSError:
        pass


def maybe_fetch_quota(upstream):
    """Poll the plan's usage meters after touching that plan.

    Quota only ever changes by using the plan, so the moments that can move the
    number are exactly the requests routed there -- including a 429, which is
    the one everyone then watches the meter for. Off the response path in a
    daemon thread, so it costs the stream nothing.
    """
    if upstream in PROVIDERS and (PROVIDERS[upstream].get("quota") or {}).get("path"):
        threading.Thread(target=fetch_quota, args=(upstream,), daemon=True).start()


# --- Messages <-> chat completions (wire: openai providers) -----------------

def _text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def to_chat(req, wire_model):
    """An Anthropic Messages request as an OpenAI chat request. Thinking blocks are
    dropped; tool results become role:tool messages ahead of the user's text."""
    msgs = []
    if req.get("system"):
        msgs.append({"role": "system", "content": _text(req["system"])})
    for m in req.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": m["role"], "content": c})
        elif m["role"] == "assistant":
            out = {"role": "assistant", "content": _text(c) or None}
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                     for b in c if b.get("type") == "tool_use"]
            if calls:
                out["tool_calls"] = calls
            msgs.append(out)
        else:
            for b in c:
                if b.get("type") == "tool_result":
                    msgs.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                 "content": _text(b.get("content", ""))})
            if _text(c):
                msgs.append({"role": "user", "content": _text(c)})
    out = {"model": wire_model, "messages": msgs}
    if "max_tokens" in req:
        out["max_tokens"] = req["max_tokens"]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    if req.get("tools"):
        out["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object"})}} for t in req["tools"]]
    tc = req.get("tool_choice")
    if tc:
        out["tool_choice"] = ({"auto": "auto", "any": "required", "none": "none"}.get(tc.get("type"))
                              or {"type": "function", "function": {"name": tc.get("name")}})
    return out


def from_chat(resp, model):
    """An OpenAI chat response as an Anthropic message."""
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = [{"type": "text", "text": msg["content"]}] if msg.get("content") else []
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            args = {"_unparsed": tc["function"].get("arguments")}
        content.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": args})
    stop = ("tool_use" if msg.get("tool_calls")
            else {"length": "max_tokens"}.get(ch.get("finish_reason"), "end_turn"))
    u = resp.get("usage") or {}
    return {"id": resp.get("id") or "msg_" + os.urandom(12).hex(), "type": "message",
            "role": "assistant", "model": model, "content": content, "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}


def message_sse(msg):
    """A whole Anthropic message replayed as its streaming events."""
    def ev(d):
        return ("event: %s\ndata: %s\n\n" % (d["type"], json.dumps(d))).encode()
    out = [ev({"type": "message_start", "message": dict(msg, content=[],
               usage=dict(msg["usage"], output_tokens=0))})]
    for i, b in enumerate(msg["content"]):
        if b["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": b["text"]}
        else:
            start = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
        out += [ev({"type": "content_block_start", "index": i, "content_block": start}),
                ev({"type": "content_block_delta", "index": i, "delta": delta}),
                ev({"type": "content_block_stop", "index": i})]
    out += [ev({"type": "message_delta", "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": msg["usage"]["output_tokens"]}}),
            ev({"type": "message_stop"})]
    return b"".join(out)


def sanitize_native(body, prov, wire_model):
    """Rewrite a request for a vendor's own Anthropic-compatible endpoint.

    Deliberately not sanitize(). That one is built for OpenRouter and injects a
    `provider` preference block, which is OpenRouter's own extension and a 400
    anywhere else. Here the endpoint speaks the Messages API by definition, so
    the body is left intact apart from the model name and whatever fields the
    vendor is declared not to understand.

    cache_control is kept by default. These are subscription endpoints, so the
    win is latency and rate-limit headroom rather than cash; set
    "cache_control": false on the provider if one starts rejecting the markers.
    """
    try:
        req = json.loads(body)
    except Exception:
        return body
    if wire_model:
        req["model"] = wire_model
    for field in prov.get("drop_fields", []):
        req.pop(field, None)
    if not prov.get("cache_control", True):
        strip_cache_control(req)
    return json.dumps(req).encode()


# Statuses that mean "this route is out of capacity", not "this request is wrong".
# 429 rate limit / quota, 402 payment required, 529 overloaded. A 401/403 is a
# credential problem and must NOT silently drain a different quota instead.
FAILOVER_STATUSES = frozenset((402, 429, 529))


def metered(model):
    """True unless the catalog bills this model to a plan. Unknown ids count as
    metered -- the safe guess when the question is whether a retry costs cash.
    A bare claude-* id goes to the Anthropic plan, listed or not (#41)."""
    if model.startswith("claude-") and "/" not in model:
        return False
    return (BY_ID.get(model) or {}).get("billing") != "subscription"


# --- turn caps ------------------------------------------------------------
# Requests per rolling hour on metered models (models.json `turn_caps`). The
# router never sees an agent session, so the cap is per model: a looping agent
# hits it within the hour instead of spending all night.
TURNS = {}
TURNS_LOCK = threading.Lock()


def turn_cap(model):
    caps = CATALOG.get("turn_caps") or {}
    if model == "gemma-or":
        return caps.get("gemma_or_per_hour")
    if not metered(model):
        return None
    
    model_info = BY_ID.get(model) or {}
    if "turn_cap" in model_info:
        cap = model_info["turn_cap"]
        return cap if cap != 0 else None

    price = (model_info.get("price") or [-1])[0]
    for ceiling, cap in caps.get("per_hour") or []:
        if ceiling is None or 0 <= price < ceiling:
            return cap
    return None


def take_turn(model):
    """Count one request against `model`'s cap. False (nothing counted) if full."""
    cap = turn_cap(model)
    if cap is None:
        return True
    now = time.time()
    with TURNS_LOCK:
        q = [t for t in TURNS.get(model, []) if now - t < 3600]
        if len(q) >= cap:
            TURNS[model] = q
            return False
        TURNS[model] = q + [now]
        return True


def failover_chain(model):
    """Models to try after `model` runs out of capacity, best-first.

    Per-model `failover` wins; otherwise the catalog-wide `failover_default`.
    Self-references and duplicates are dropped so a bad config cannot loop.
    """
    entry = BY_ID.get(model) or {}
    chain = entry.get("failover")
    if chain is None:
        chain = CATALOG.get("failover_default") or []
    if isinstance(chain, str):
        chain = [chain]
    seen, out = {model}, []
    for m in chain:
        if m and m not in seen and m in BY_ID:
            seen.add(m)
            out.append(m)
    return out


def attempts_for(model, pol, plan_only=False):
    """The model plus its failover chain, filtered by the folder policy -- and,
    for a dispatcher run tagged plan:, stripped of anything metered, so the
    walk can try Antigravity before any cash is spent."""
    return [m for m in [model] + failover_chain(model)
            if allowed(m, pol) and not (plan_only and metered(m))]


def upstream_for(model):
    """Vendor prefix first, then the generic slash rule.

    "kimi/k3"            -> direct:kimi   its own plan, its own endpoint
    "moonshotai/kimi-k3" -> openrouter    same model, metered per token
    "claude-opus-5"      -> anthropic
    """
    vendor = (model or "").split("/")[0]
    if vendor in PROVIDERS:
        return "direct:" + vendor
    return "openrouter" if "/" in (model or "") else "anthropic"


# --- Per-folder data policy -------------------------------------------------
# Which models a folder is allowed to talk to. Claude Code states its working
# directory in the Environment block it sends with every request, so the folder
# is knowable at the wire without the client cooperating.
POLICY         = CATALOG.get("folder_policy", {})
POLICY_DEFAULT = {"data": "any", "tier": "pro"}
DATA_RANK      = {"claude": 0, "zdr": 1, "any": 2}   # strictest first


def data_level(v):
    """What a folder_policy "data" value means: "claude", "zdr" or "any".

    allowed(), policy_for() and dispatch.tier_allowed() all read data through
    here, so they cannot disagree. None and "" mean any; "claude", "zdr" and
    "any" are matched lowercased. Anything else -- a typo such as "clade", a
    padded " claude", false, 0, a list -- fails closed as claude (#49).
    """
    if v is None or v == "":
        return "any"
    if not isinstance(v, str):
        return "claude"
    v = v.lower()
    return v if v in DATA_RANK else "claude"


def _rank(v):
    return DATA_RANK[data_level(v)]


CWD_RE = re.compile(r"Primary working directory:\s*([^\r\n\"\\]+)")


def cwd_of(body):
    """The session's working directory, or "" if the request never said."""
    if not body:
        return ""
    m = CWD_RE.search(body.decode("utf-8", "replace"))
    return m.group(1).strip().rstrip("/") if m else ""


def _fold(p):
    """One spelling per name, compared the way APFS does: full case fold, then
    NFD. .lower() missed ß/SS, final sigma, and NFC against NFD (#49)."""
    return unicodedata.normalize("NFD", p.casefold())


def _real(p):
    """p with symlinks, "." and ".." resolved; lexical when the disk can't say
    (realpath raises ValueError on a NUL in the path)."""
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return os.path.normpath(p)


def _layer(cwd, norm):
    """The policy for one spelling of cwd; norm is applied to cwd and every root.

    Every row whose folder holds cwd applies, shortest root first, so a subfolder
    inherits its vault's rule. Rows that fold to one root (`policy private` written
    beside Private) apply in key order, so a JSON re-save cannot change the result.
    data is then the strictest of them, a row with no data key counting as what it
    inherits: the enclosing rows' data, or _default's when that is stricter. A row
    that is not an object fails closed as claude. A data of null or "" counts as no data key.
    """
    pol = dict(POLICY.get("_default") or POLICY_DEFAULT)
    dflt = data = pol.get("data")
    cwd = _fold(norm(cwd))
    groups = {}
    for path, p in POLICY.items():
        if path.startswith("_"):
            continue
        if not isinstance(p, dict):
            p = {"data": "claude"}
        root = _fold(norm(os.path.expanduser(path))).rstrip("/")
        if cwd == root or cwd.startswith(root + "/"):
            groups.setdefault(root, []).append((path, p))
    for root in sorted(groups, key=len):
        hits = sorted(groups[root], key=lambda h: h[0])
        inherited = min((data, dflt), key=_rank)
        for _, p in hits:
            pol.update(p)
        data = pol["data"] = min((p["data"] if p.get("data") not in (None, "") else inherited for _, p in hits), key=_rank)
    return pol, bool(groups)


def policy_for(cwd):
    """The folder policy for a session's cwd; _layer says how rows combine.

    The cwd is matched as sent (what #40 matched) and, when absolute, as the folder
    on disk: symlinks, "." and ".." resolved on the cwd and on every root. Every key
    but data comes from the as-sent match when it hit a row (what master used), else
    from the on-disk match; data is the stricter of the two, so resolving a path can
    never loosen what the as-sent spelling matched. A relative
    cwd is matched as sent only: resolving it would read the router's own cwd.

    ponytail: no cwd -> the default. A bare API client sends no Environment
    block, and failing closed on an unknown cwd would break every non-Claude-Code
    caller. Tighten only if something other than Claude Code starts talking here.
    """
    cwd = cwd or ""
    pol, hit = _layer(cwd, lambda p: p)
    if os.path.isabs(cwd):
        real, _ = _layer(cwd, _real)
        keep, other = (pol, real) if hit else (real, pol)
        if _rank(other.get("data")) < _rank(keep.get("data")):
            keep["data"] = other["data"]
        pol = keep
    return pol


def allowed(model, pol):
    """Whether a folder's data policy permits this model.

    claude  Anthropic only -- nothing leaves the plan.
    zdr     plus OpenRouter routes that have a zero-data-retention provider. A
            vendor subscription is excluded unless its provider declares
            "zdr": true, because a plan retains under the vendor's own terms and
            an undeclared one must not be assumed private.
    any     no restriction.
    """
    data = data_level(pol.get("data"))
    if data == "any":
        return True
    up = upstream_for(model)
    if up == "anthropic":
        return True
    if data == "claude":
        return False
    if up.startswith("direct:"):
        return PROVIDERS.get(up.split(":", 1)[1], {}).get("zdr") is True
    return (BY_ID.get(model) or {}).get("zdr", True) is not False


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def pin_effort(req, model, pol):
    """Overwrite the slider with the folder's effort level, if it sets one.

    A Claude request is only rewritten when it already carries an effort: the
    client sends none to a model that takes none (Haiku-class), and an injected
    one would 400. A gateway model gets it regardless -- sanitize() turns it
    into a thinking budget, and an absent level would mean the flat default.
    """
    level = pol.get("effort")
    if level not in EFFORT_LEVELS:
        return
    oc = req.get("output_config")
    if upstream_for(model) == "anthropic" and not (oc or {}).get("effort"):
        return
    req["output_config"] = dict(oc or {}, effort=level)


class Router(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-router"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/gemma/"):
            return self.gemma()
        self.route()

    def do_GET(self):
        if self.path.rstrip("/").endswith("/v1/models"):
            return self.models()
        self.route()

    # --- request forwarding -------------------------------------------------
    def route(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""

        pol = policy_for(cwd_of(body))
        model = requested = ""
        effort = None
        if body:
            try:
                req = json.loads(body)
            except Exception:
                req = None
            if req is not None:
                model = requested = req.get("model", "") or ""
                if requested == AUTO_MODEL:
                    model = resolve_auto(req, pol)
                elif requested == ARROW_MODEL:
                    model = resolve_arrow(req, pol)
                elif requested.startswith(FAMILY_PREFIX):
                    model = resolve_family(req, requested[len(FAMILY_PREFIX):],
                                           pol.get("tier"), pol)
                before = effort_of(req)
                pin_effort(req, model, pol)
                effort = effort_of(req)
                if model != requested or effort != before:
                    req["model"] = model
                    body = json.dumps(req).encode()

        # The data policy filters the whole chain, so failover cannot route around
        # it. A hand-picked model that the folder forbids is refused rather than
        # silently swapped -- a swap looks like the pick worked.
        run = self.headers.get("x-switchboard-run", "") or ""
        plan_only = run.startswith("plan:")
        attempts = attempts_for(model, pol, plan_only)
        if not attempts:
            return self.fail(403,
                "%s is set to data policy %r, which does not permit %s. "
                "Pick a Claude model, or change the folder's policy: "
                "switchboard.py policy <folder> --data any"
                % (cwd_of(body) or "this folder", pol.get("data", "any"),
                   model or "that model"))
        # An OpenAI-wire provider is answered on its own path, first choice only:
        # it is never in a failover chain, so nothing can fall over onto it.
        vendor = attempts[0].split("/")[0]
        if (PROVIDERS.get(vendor) or {}).get("wire") == "openai":
            return self.openai_wire(attempts[0], body, requested)
        prepared, errors = [], []
        for cand in attempts:
            got = self.prepare(cand, body)
            if isinstance(got, str):
                errors.append("%s: %s" % (cand, got))
            else:
                got["effort"] = effort
                prepared.append(got)
        if not prepared:
            return self.fail(500, "; ".join(errors) or "no usable upstream")

        self.relay("POST" if body else self.command, prepared,
                   requested=requested, run=run)

    def prepare(self, model, body):
        """Everything needed to issue one attempt, or a string explaining why not.

        Split out of route() so a failover attempt is built the same way as the
        first one -- each target needs its own credential, path, body sanitising
        and model name on the wire, and reusing the previous target's would send
        one vendor's request to another.
        """
        target = upstream_for(model)
        vendor = key = None
        if body:
            try:
                req = json.loads(body)
                req["model"] = model
                body = json.dumps(req).encode()
            except Exception:
                pass
        if target.startswith("direct:"):
            vendor = target.split(":", 1)[1]
            prov = PROVIDERS[vendor]
            keyfile = prov.get("key_file", "")
            key = read_key(keyfile)
            if key and expiring(key):
                key = renew_key(vendor, prov, key) or key
            if not key:
                return "no %s key at %s" % (vendor, keyfile or "<unset>")
            host = prov["host"]
            path = prov.get("path_prefix", "").rstrip("/") + self.path
            headers = self.headers_for_direct(prov, key)
            wire = UPSTREAM_ID.get(model) or model.split("/", 1)[-1]
            body = sanitize_native(body, prov, wire)
            limit = (BY_ID.get(model) or {}).get("context")
            approx = len(body) // 4                    # ~4 chars per token
            if limit and approx > limit:
                return ("this conversation is ~%s tokens and %s holds %s"
                        % ("{:,}".format(approx), model, "{:,}".format(limit)))
            target = vendor            # log each subscription as its own pool
        elif target == "openrouter":
            key = or_key(model)
            if not key:
                up = (BY_ID.get(model) or {}).get("upstream")
                return ("no key for upstream %r -- check its key_file" % up if up
                        else "no OpenRouter key at ~/.config/openrouter-key")
            host, path = "openrouter.ai", "/api" + self.path
            headers = self.headers_for_openrouter(key)
            body = sanitize(body)
        else:
            host, path = "api.anthropic.com", self.path
            headers = self.headers_passthrough()
        if body:
            for k in [k for k in headers if k.lower() == "content-length"]:
                del headers[k]
            headers["Content-Length"] = str(len(body))
        return {"model": model, "upstream": target, "host": host, "path": path,
                "headers": headers, "body": body, "vendor": vendor, "key": key,
                # Audit: a privacy rail is only trustworthy if you can ask later
                # what actually went to the permissive workspace.
                "nonzdr": target == "openrouter" and
                          not (BY_ID.get(model) or {}).get("upstream") and
                          (BY_ID.get(model) or {}).get("zdr", True) is False,
                "key_upstream": target == "openrouter" and
                                (BY_ID.get(model) or {}).get("upstream")}

    def headers_passthrough(self):
        """Everything the client sent, minus hop-by-hop. Credential untouched."""
        h = {k: v for k, v in self.headers.items()
             if k.lower() not in HOP and k.lower() not in ("host", "accept-encoding")}
        return h

    def headers_for_openrouter(self, key):
        h = self.headers_passthrough()
        for k in list(h):
            if k.lower() in ("authorization", "x-api-key", "anthropic-beta"):
                del h[k]
        h["Authorization"] = "Bearer " + key
        return h

    def headers_for_direct(self, prov, key):
        """The vendor's own credential, and never yours.

        Same contract as headers_for_openrouter: the claude.ai OAuth token and
        any Anthropic API key are removed before the request leaves the machine.
        anthropic-beta goes with them -- a compatible endpoint implements the
        Messages API, not Anthropic's beta flags, and an unknown one is a 400.
        """
        h = self.headers_passthrough()
        for k in list(h):
            if k.lower() in ("authorization", "x-api-key", "anthropic-beta"):
                del h[k]
        prefix = prov.get("auth_prefix", "")
        h[prov.get("auth_header", "x-api-key")] = (
            prefix + " " + key if prefix else key)
        if not any(k.lower() == "anthropic-version" for k in h):
            h["anthropic-version"] = "2023-06-01"
        return h

    def issue(self, method, att, rec):
        """One attempt, renewing a dead vendor credential once before giving up.

        A 401 is not a failover case: the model has capacity and the request is
        fine, the token has simply expired. Failing over would answer from a
        different model, and returning it makes the client retry a credential
        that cannot recover -- so it is retried here, on the same upstream, with
        a fresh token. Safe for the same reason failover is: nothing has been
        written to the client yet, so the first attempt can be abandoned
        silently. Only the vendor paths renew; the Anthropic path forwards the
        client's own credential and is not the router's to refresh.
        """
        for renewed in (False, True):
            conn = http.client.HTTPSConnection(att["host"], timeout=900)
            conn.request(method, att["path"], body=att["body"],
                         headers=att["headers"])
            resp = conn.getresponse()
            if resp.status != 401 or renewed or not att.get("vendor"):
                return conn, resp
            prov = PROVIDERS.get(att["vendor"]) or {}
            fresh = renew_key(att["vendor"], prov, att.get("key"))
            if not fresh:
                return conn, resp          # nothing better to answer with
            prefix = prov.get("auth_prefix", "")
            att["headers"][prov.get("auth_header", "x-api-key")] = (
                prefix + " " + fresh if prefix else fresh)
            att["key"] = fresh
            rec["renewed"] = att["vendor"]
            try:
                resp.read()                # drain before reusing the socket
                conn.close()
            except Exception:
                pass
        return conn, resp


    def relay(self, method, prepared, requested="", run=""):
        """Issue attempts in order until one has capacity, then stream it.

        The decision is safe to make here because nothing has been written to the
        client yet -- the status line is known before the first byte goes out, so
        a 429 on the first choice can be abandoned silently. Once streaming
        starts there is no going back, which is why failover is status-based and
        never mid-stream. A transport error only fails over to a plan-billed model.
        """
        t0 = time.time()
        resp = conn = None
        att = prepared[0]
        for i, att in enumerate(prepared):
            last = (i == len(prepared) - 1)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "upstream": att["upstream"], "model_requested": att["model"]}
            if run:
                rec["run"] = run
            if att.get("nonzdr"):
                rec["nonzdr"] = True
            if att.get("key_upstream"):
                rec["key"] = att["key_upstream"]
            if requested and requested != att["model"]:
                rec["auto_from"] = requested
            if att.get("effort"):
                rec["effort"] = att["effort"]
            if i:
                rec["failover_from"] = prepared[i - 1]["model"]
            if not take_turn(att["model"]):
                rec.update(status=429, error="turn cap: %s/h" % turn_cap(att["model"]))
                log_request(rec)
                if last:
                    return self.fail(429, "%s hit its Switchboard turn cap (%s requests/hour, "
                                     "models.json turn_caps). Wait, or pick a plan model."
                                     % (att["model"], turn_cap(att["model"])))
                continue
            try:
                conn, resp = self.issue(method, att, rec)
            except Exception as e:
                rec.update(status=502, error=str(e), ms=int((time.time() - t0) * 1000))
                log_request(rec)
                # A dropped connection is not a capacity signal, so it may move on
                # to a plan-billed route but never buy the same turn again per token.
                # This branch used to continue unconditionally: $3.11 on 2026-09-16.
                if last or metered(prepared[i + 1]["model"]):
                    return self.fail(502, "upstream %s: %s" % (att["host"], e))
                continue
            if resp.status in FAILOVER_STATUSES and not last:
                rec.update(status=resp.status, failed_over_to=prepared[i + 1]["model"],
                           ms=int((time.time() - t0) * 1000))
                log_request(rec)
                maybe_fetch_quota(att["upstream"])
                try:
                    resp.read()          # drain so the socket can be closed cleanly
                    conn.close()
                except Exception:
                    pass
                continue
            break
        if resp is None:                      # every attempt raised
            return self.fail(502, "no upstream answered")
        if run.startswith("plan:") and resp.status in FAILOVER_STATUSES:
            # Every plan-billed attempt was refused. Say so with a status Claude Code
            # does not retry, so `switchboard run` moves on at once instead of after
            # minutes of 429 backoff -- and never onto cash from here.
            rec.update(status=resp.status, error="plan exhausted",
                       ms=int((time.time() - t0) * 1000))
            log_request(rec)
            try:
                resp.read(); conn.close()
            except Exception:
                pass
            return self.fail(402, "plan quota exhausted on %s; nothing plan-billed left "
                           "to try (switchboard run moves to its next tier)"
                           % ", ".join(p["model"] for p in prepared))
        host, model, upstream = att["host"], att["model"], att["upstream"]

        streaming = resp.getheader("content-length") is None
        rec["status"], rec["stream"] = resp.status, streaming

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        if streaming:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", resp.getheader("content-length"))
        self.end_headers()

        captured = bytearray()
        try:
            if streaming:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    captured.extend(chunk)          # keep only the tail; usage
                    if len(captured) > 32768:       # arrives near the end
                        del captured[:-32768]
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            else:
                data = resp.read()
                captured.extend(data)
                self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            rec["client_disconnected"] = True
        finally:
            conn.close()

        usage, provider, used = read_usage(bytes(captured))
        rec["ms"] = int((time.time() - t0) * 1000)
        rec["model_used"] = used or model
        rec["provider"] = provider
        for src, dst in (("input_tokens", "in"), ("output_tokens", "out"),
                         ("cache_read_input_tokens", "cache_read"),
                         ("cache_creation_input_tokens", "cache_write")):
            if usage.get(src) is not None:
                rec[dst] = usage[src]
        if usage.get("cost") is not None:
            rec["cost"] = usage["cost"]
        log_request(rec)
        maybe_fetch_quota(upstream)

        self.log_message("%s %s -> %s %s (%dms%s)", rec["status"], model or path,
                         provider or host, used or "", rec["ms"],
                         ", $%.5f" % rec["cost"] if rec.get("cost") else "")

    # --- model discovery ----------------------------------------------------
    def models(self):
        """Anthropic's list plus the OpenRouter models worth showing in /model."""
        out = []
        try:
            c = http.client.HTTPSConnection("api.anthropic.com", timeout=30)
            c.request("GET", "/v1/models?limit=100", headers=self.headers_passthrough())
            out = json.loads(c.getresponse().read()).get("data", [])
            c.close()
        except Exception as e:
            self.log_message("anthropic model list failed: %s", e)

        if CANDIDATES:
            # Bare Anthropic ids are never published — Claude Code lists them
            # natively and a second copy would duplicate every Claude row.
            for r in picker_rows():
                out.append(dict(r, type="model",
                                created_at="2026-01-01T00:00:00Z"))
        elif or_key():
            # No catalog: fall back to the old substring filter over OpenRouter.
            try:
                c = http.client.HTTPSConnection("openrouter.ai", timeout=30)
                c.request("GET", "/api/v1/models",
                          headers={"Authorization": "Bearer " + or_key()})
                for m in json.loads(c.getresponse().read()).get("data", []):
                    mid = m.get("id", "")
                    if ":" in mid:            # skip async batch endpoints
                        continue
                    if any(s in mid.lower() for s in SURFACE):
                        out.append({"type": "model", "id": mid,
                                    "display_name": m.get("name") or mid,
                                    "created_at": "2026-01-01T00:00:00Z"})
                c.close()
            except Exception as e:
                self.log_message("openrouter model list failed: %s", e)

        out.append({"type": "model", "id": AUTO_MODEL,
                    "display_name": "Auto — cheap first, escalates",
                    "created_at": "2026-01-01T00:00:00Z"})

        payload = json.dumps({"data": out, "has_more": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        # Tells `switchboard run` this router understands run tags and converts
        # plan exhaustion to a 402; without it the plan tier refuses to run.
        self.send_header("x-switchboard-run", "plan-402")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def gemma(self):
        """Relay one harness request to /local or /or, verbatim apart from auth
        and, for /or, the model name. No policy, no failover: the path decides."""
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        route, _, rest = self.path[len("/gemma/"):].partition("/")
        rest = "/" + rest
        headers = {"Content-Type": "application/json"}
        if route == "local":
            u = urllib.parse.urlsplit(GEMMA_LOCAL)
            host, tls, path = u.netloc, u.scheme == "https", rest
            if self.headers.get("authorization"):
                headers["Authorization"] = self.headers["authorization"]
        elif route == "or":
            key = read_key(GEMMA_KEYFILE)
            if not key:
                return self.fail(500, "no Gemma OpenRouter key at ~/.config/openrouter-key-gemma")
            if not take_turn("gemma-or"):
                return self.fail(429, "/gemma/or hit its turn cap (%s requests/hour)"
                                 % turn_cap("gemma-or"))
            try:
                req = json.loads(body)
            except Exception:
                return self.fail(400, "body is not JSON")
            body = json.dumps(gemma_or_body(req)).encode()
            host, tls = "openrouter.ai", True
            path = "/api" + rest
            headers["Authorization"] = "Bearer " + key
        elif route == "kaggle":
            prov = PROVIDERS.get("kaggle")
            got = prov and minted_token("kaggle", prov)
            if not got:
                return self.fail(401, "no Kaggle Model Proxy token -- run: kaggle benchmarks auth")
            try:
                body = json.dumps(gemma_kaggle_body(json.loads(body))).encode()
            except ValueError as e:
                return self.fail(400, str(e))
            u = urllib.parse.urlsplit(got[0])
            host, tls = u.netloc, True
            path = u.path.rstrip("/") + "/openapi" + (rest[3:] if rest.startswith("/v1/") else rest)
            headers["Authorization"] = "Bearer " + got[1]
        else:
            return self.fail(404, "gemma route must be /gemma/local/..., /gemma/or/... or /gemma/kaggle/...")

        t0 = time.time()
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "upstream": "gemma-" + route,
               "model_requested": "gemma-4-31b-it-qat-w4a16-ct"}
        try:
            conn = (http.client.HTTPSConnection if tls else http.client.HTTPConnection)(
                host, timeout=900)
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except OSError as e:
            rec.update(status=502, error=str(e)[:200])
            log_request(rec)
            return self.fail(502, "gemma %s upstream unreachable: %s" % (route, e))
        rec.update(status=resp.status, ms=int((time.time() - t0) * 1000))
        # Plain JSON, or SSE whose last usage-bearing chunk carries the totals.
        u = None
        for chunk in [data] + [l[5:] for l in reversed(data.splitlines())
                               if l.startswith(b"data:")]:
            try:
                u = json.loads(chunk).get("usage")
            except (ValueError, AttributeError):
                continue
            if u:
                break
        if u:
            rec.update(model_used={"or": GEMMA_OR_MODEL, "kaggle": GEMMA_KAGGLE_MODEL}.get(route, "llama-server"),
                       **{"in": u.get("prompt_tokens"), "out": u.get("completion_tokens")})
            if isinstance(u.get("cost"), dict):   # Kaggle: {"*_nanodollars": n} (#32)
                rec["cost"] = sum(u["cost"].values()) / 1e9
            elif u.get("cost") is not None:
                rec["cost"] = u["cost"]
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            if cached:
                rec["cached"] = cached
        try:                                   # which provider actually served it
            rec["provider"] = json.loads(data).get("provider")
        except (ValueError, AttributeError):
            pass
        if resp.status >= 400:                 # the reason, not just the status
            rec["error"] = gemma_error_text(data)
        log_request(rec)
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def openai_wire(self, model, body, requested):
        """Answer a Messages request from an OpenAI chat-completions provider.

        The request is translated (to_chat), sent without streaming -- the Kaggle
        proxy does not stream -- and the reply is translated back (from_chat) and,
        if the client asked to stream, replayed as SSE (message_sse)."""
        t0 = time.time()
        vendor = model.split("/")[0]
        prov = PROVIDERS[vendor]
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "upstream": vendor, "model_requested": model}
        if requested and requested != model:
            rec["auto_from"] = requested
        if "count_tokens" in self.path:          # no such endpoint upstream: estimate
            payload = json.dumps({"input_tokens": len(body) // 4}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        got = minted_token(vendor, prov)
        if not got:
            return self.fail(401, "could not mint a %s token -- run: %s" % (vendor, " ".join(prov["token_env"]["mint"])))
        url, key = got
        req = json.loads(body)
        chat = to_chat(req, UPSTREAM_ID.get(model) or model.split("/", 1)[1])
        u = urllib.parse.urlsplit(url + prov.get("chat_path", "/openapi/chat/completions"))
        try:
            conn = http.client.HTTPSConnection(u.netloc, timeout=900)
            conn.request("POST", u.path, body=json.dumps(chat).encode(),
                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except Exception as e:
            rec.update(status=502, error=str(e), ms=int((time.time() - t0) * 1000))
            log_request(rec)
            return self.fail(502, "%s: %s" % (vendor, e))
        rec.update(status=resp.status, stream=bool(req.get("stream")), ms=int((time.time() - t0) * 1000))
        if resp.status != 200:
            try:
                why = json.loads(data).get("message") or data[:300].decode(errors="replace")
            except Exception:
                why = data[:300].decode(errors="replace")
            rec["error"] = why
            log_request(rec)
            return self.fail(resp.status, "%s %s: %s" % (vendor, chat["model"], why))
        msg = from_chat(json.loads(data), model)
        rec.update(model_used=chat["model"], **{"in": msg["usage"]["input_tokens"],
                                                "out": msg["usage"]["output_tokens"]})
        log_request(rec)
        if req.get("stream"):
            payload, ctype = message_sse(msg), "text/event-stream"
        else:
            payload, ctype = json.dumps(msg).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def fail(self, code, msg):
        payload = json.dumps({"type": "error",
                              "error": {"type": "api_error", "message": msg}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    sys.stderr.write("claude-router on 127.0.0.1:%d  (bare ids -> Anthropic, "
                     "provider/slug -> OpenRouter, subscriptions: %s)\n"
                     % (PORT, ", ".join(sorted(PROVIDERS)) or "none"))
    # Say it now rather than at first use: a missing key file is a 500 halfway
    # through a task, and the picker row gives no hint that it is inert.
    for _name, _p in sorted(PROVIDERS.items()):
        if not read_key(_p.get("key_file", "")):
            sys.stderr.write("  ! %s has no key at %s — its models will fail "
                             "until that file exists (chmod 600)\n"
                             % (_name, _p.get("key_file") or "<unset>"))
        # Get each plan's meters on record now rather than at first traffic --
        # the file predating the next request is what lets the notch show quota
        # for a plan nobody has spent today.
        if (_p.get("quota") or {}).get("path"):
            threading.Thread(target=fetch_quota, args=(_name, True),
                             daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Router).serve_forever()
