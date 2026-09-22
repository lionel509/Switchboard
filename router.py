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
import base64, hashlib, http.client, json, os, re, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT     = int(os.environ.get("CLAUDE_ROUTER_PORT", "8787"))
KEYFILE  = os.path.expanduser("~/.config/openrouter-key")
LOGFILE  = os.path.expanduser(os.environ.get(
    "CLAUDE_ROUTER_LOG", "~/.local/share/claude-router/requests.log"))
# substrings of OpenRouter model ids to surface in /model
SURFACE  = [s for s in os.environ.get("CLAUDE_ROUTER_MODELS", "grok").split(",") if s]

# --- Catalog ---------------------------------------------------------------
# models.json is the source of truth for what appears in /model and what Auto
# may choose from. Edit it with switchboard.py. If it is missing we fall back to
# the old CLAUDE_ROUTER_MODELS substring behaviour so nothing breaks.
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

# Reasoning budget handed to OpenRouter models, in output tokens. These are run
# at their strongest rather than sliding with the effort setting, because on this
# setup the ←/→ slider is spent selecting the model instead (see ARROW_MODEL).
# Thinking tokens bill as output tokens, so this is a real cost lever.
THINKING_BUDGET = int(os.environ.get("CLAUDE_ROUTER_THINKING", "12000"))


def strip_cache_control(node):
    """Remove every cache_control marker, recursively. Anthropic-only."""
    if isinstance(node, dict):
        node.pop("cache_control", None)
        for v in node.values():
            strip_cache_control(v)
    elif isinstance(node, list):
        for v in node:
            strip_cache_control(v)


def sanitize(body):
    """Drop Anthropic-only fields, and cache_control markers, for other models."""
    try:
        req = json.loads(body)
    except Exception:
        return body
    model = req.get("model", "")
    req = {k: v for k, v in req.items() if k in PORTABLE}
    req["provider"] = prefs_for(model)

    strip_cache_control(req)

    # Restore reasoning. `thinking` is dropped above as an Anthropic-only field,
    # but OpenRouter honours exactly that one — measured on deepseek-v4-pro:
    # no field at all gives thinking_tokens=0, {"reasoning": {"effort": "high"}}
    # also gives 0 (silently ignored), and an explicit budget gives 222. So
    # without this these models run with NO reasoning whatsoever.
    # Anthropic requires 1024 <= budget_tokens < max_tokens.
    mt = req.get("max_tokens") or 0
    budget = min(THINKING_BUDGET, mt - 1024)
    if budget >= 1024:
        req["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return json.dumps(req).encode()


# OpenRouter provider routing. Keys are matched as substrings of the model id;
# DEFAULT applies to anything unmatched. zdr=True restricts to zero-data-retention
# providers; sort="throughput" picks the fastest of those.
PROVIDER_PREFS = {
    "gemini": {"zdr": True, "order": ["google-vertex/global"]},
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


def resolve_auto(req):
    """Map the auto pseudo-model onto a real one. One-way: never downgrades."""
    key = conv_key(req)
    with _auto_lock:
        pinned = _auto_pins.get(key)
    if pinned == FALLBACK:
        return FALLBACK                  # escalation is permanent for this conversation
    if pinned:
        chosen = FALLBACK if looks_hard(req) else pinned
    elif looks_hard(req):
        chosen = FALLBACK                # already real work; nothing to decide
    else:
        # First turn: spend one cheap call working out what this task needs.
        chosen = ask_router_model(task_text(req)) or FALLBACK
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


def resolve_family(req, fam, tier=None):
    """One picker row per family; the tier is chosen per task.

    Claude Code's picker cannot put variants behind a row — the arrow-key adjust
    on the effort line is its own UI, not something a gateway entry can hook. So
    a family row resolves its own tier the way Auto does, restricted to that
    family, and the list stays one row per family instead of one per variant.

    A folder that names a tier settles it outright: no router call, no latency,
    and the answer is the same every turn. Default is pro, set per folder.
    """
    cands = family_of(fam)
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


def resolve_arrow(req):
    """Map the picker's effort level onto a model. This is the arrow-key row.

    No conversation pin: the whole point is that ←/→ switches the model mid
    session, so the effort on each request is the live answer.
    """
    eff = (req.get("output_config") or {}).get("effort")
    chosen = ARROW_MAP.get(eff)
    if chosen:
        return chosen
    # Effort absent or unmapped: fall to the highest level that is configured,
    # since the shortlist is ordered weakest-to-strongest.
    for lvl in reversed(ARROW_ORDER):
        if ARROW_MAP.get(lvl):
            return ARROW_MAP[lvl]
    return FALLBACK


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
        rows.append({"id": FAMILY_PREFIX + fam,
                     "display_name": "%s (%s)" % (label, tiers)})
    for mid in PICKER.get("models", []):
        m = BY_ID.get(mid)
        if not m or "/" not in mid:
            continue
        label = m.get("name") or mid
        if m.get("zdr", True) is False:
            label += " (no ZDR)"
        rows.append({"id": mid, "display_name": label})
    return rows


def read_key(path):
    """A credential from a file, or None. Never logged, never cached."""
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return None


def or_key():
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
CWD_RE = re.compile(r"Primary working directory:\s*([^\r\n\"\\]+)")


def cwd_of(body):
    """The session's working directory, or "" if the request never said."""
    if not body:
        return ""
    m = CWD_RE.search(body.decode("utf-8", "replace"))
    return m.group(1).strip().rstrip("/") if m else ""


def policy_for(cwd):
    """Longest-prefix folder policy, so a subfolder inherits its vault's rule.

    ponytail: no cwd -> the default. A bare API client sends no Environment
    block, and failing closed on an unknown cwd would break every non-Claude-Code
    caller. Tighten only if something other than Claude Code starts talking here.
    """
    pol = dict(POLICY.get("_default") or POLICY_DEFAULT)
    best, found = "", None
    for path, p in POLICY.items():
        if path.startswith("_") or not isinstance(p, dict):
            continue
        root = os.path.expanduser(path).rstrip("/")
        if (cwd == root or cwd.startswith(root + "/")) and len(root) >= len(best):
            best, found = root, p
    if found:
        pol.update(found)
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
    data = (pol.get("data") or "any").lower()
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
        if body:
            try:
                req = json.loads(body)
            except Exception:
                req = None
            if req is not None:
                model = requested = req.get("model", "") or ""
                if requested == AUTO_MODEL:
                    model = resolve_auto(req)
                elif requested == ARROW_MODEL:
                    model = resolve_arrow(req)
                elif requested.startswith(FAMILY_PREFIX):
                    model = resolve_family(req, requested[len(FAMILY_PREFIX):],
                                           pol.get("tier"))
                if model != requested:
                    req["model"] = model
                    body = json.dumps(req).encode()

        # The data policy filters the whole chain, so failover cannot route around
        # it. A hand-picked model that the folder forbids is refused rather than
        # silently swapped -- a swap looks like the pick worked.
        attempts = [m for m in [model] + failover_chain(model) if allowed(m, pol)]
        if not attempts:
            return self.fail(403,
                "%s is set to data policy %r, which does not permit %s. "
                "Pick a Claude model, or change the folder's policy: "
                "switchboard.py policy <folder> --data any"
                % (cwd_of(body) or "this folder", pol.get("data", "any"),
                   model or "that model"))
        prepared, errors = [], []
        for cand in attempts:
            got = self.prepare(cand, body)
            if isinstance(got, str):
                errors.append("%s: %s" % (cand, got))
            else:
                prepared.append(got)
        if not prepared:
            return self.fail(500, "; ".join(errors) or "no usable upstream")

        self.relay("POST" if body else self.command, prepared,
                   requested=requested)

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
            key = or_key()
            if not key:
                return "no OpenRouter key at ~/.config/openrouter-key"
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
                "headers": headers, "body": body, "vendor": vendor, "key": key}

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


    def relay(self, method, prepared, requested=""):
        """Issue attempts in order until one has capacity, then stream it.

        The decision is safe to make here because nothing has been written to the
        client yet -- the status line is known before the first byte goes out, so
        a 429 on the first choice can be abandoned silently. Once streaming
        starts there is no going back, which is why failover is status-based and
        never mid-stream.
        """
        t0 = time.time()
        resp = conn = None
        att = prepared[0]
        for i, att in enumerate(prepared):
            last = (i == len(prepared) - 1)
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "upstream": att["upstream"], "model_requested": att["model"]}
            if requested and requested != att["model"]:
                rec["auto_from"] = requested
            if i:
                rec["failover_from"] = prepared[i - 1]["model"]
            try:
                conn, resp = self.issue(method, att, rec)
            except Exception as e:
                rec.update(status=502, error=str(e), ms=int((time.time() - t0) * 1000))
                log_request(rec)
                if last:
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
