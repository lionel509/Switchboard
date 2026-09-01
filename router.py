#!/usr/bin/env python3
"""Route Claude Code requests by model name, so /model switches providers.

  model has no "/"  ->  api.anthropic.com, forwarding whatever credential the
                        client sent, untouched. Your claude.ai subscription.
  model has a "/"   ->  openrouter.ai (e.g. x-ai/grok-4.6), using the key in
                        ~/.config/openrouter-key.

Nothing is stored. The subscription token is forwarded, never read or written.
"""
import hashlib, http.client, json, os, sys, threading, time
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


def sanitize(body):
    """Drop Anthropic-only fields, and cache_control markers, for other models."""
    try:
        req = json.loads(body)
    except Exception:
        return body
    model = req.get("model", "")
    req = {k: v for k, v in req.items() if k in PORTABLE}
    req["provider"] = prefs_for(model)

    def strip_cc(node):
        if isinstance(node, dict):
            node.pop("cache_control", None)
            for v in node.values():
                strip_cc(v)
        elif isinstance(node, list):
            for v in node:
                strip_cc(v)

    strip_cc(req)
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
2. Bulk mechanical work - renames, greps, reformatting, simple mechanical edits - \
goes to the cheapest priced model that covers it. This is the whole point: it \
preserves quota for work that needs it.
3. Real coding, multi-step tool use, debugging, or anything where a wrong answer \
costs time goes to a [subscription] model. They are free at the margin, so paying \
cash for this would be strictly worse.
4. Only pick a priced model over a [subscription] one when its description names a \
strength the task actually needs - very long context, or a specific modality.

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
        return "$%s/$%s per 1M" % (p[0], p[1])

    def line(m):
        s = "%s | %s | %s" % (m["id"], cost(m), m.get("good_at", ""))
        if m.get("specialties"):
            s += " SPECIALTY: %s." % ", ".join(m["specialties"])
        return s

    listing = "\n".join(line(m) for m in cands)
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


FAMILY_PREFIX = "~fam/"
FAMILY_LABELS = CATALOG.get("family_labels", {})
PICKER         = CATALOG.get("picker", {})


def family_of(fam):
    """Catalog entries in one family, cheapest first."""
    return sorted([m for m in CANDIDATES if m.get("family") == fam],
                  key=lambda m: (m.get("price") or [0])[0])


def resolve_family(req, fam):
    """One picker row per family; the tier is chosen per task.

    Claude Code's picker cannot put variants behind a row — the arrow-key adjust
    on the effort line is its own UI, not something a gateway entry can hook. So
    a family row resolves its own tier the way Auto does, restricted to that
    family, and the list stays one row per family instead of one per variant.
    """
    cands = family_of(fam)
    if not cands:
        return FALLBACK
    key = conv_key(req) + ":" + fam
    with _auto_lock:
        pinned = _auto_pins.get(key)
    # On failure take the family's top tier rather than its cheapest: the family
    # was chosen deliberately, so erring toward capable beats erring toward cheap.
    chosen = pinned or ask_router_model(task_text(req), cands) or cands[-1]["id"]
    with _auto_lock:
        _auto_pins[key] = chosen
    return chosen


def picker_rows():
    """What to publish to /model. Families collapse; the rest is opt-in.

    Everything in the catalog stays available to Auto whether it is listed here
    or not — this only controls how long the menu is.
    """
    rows = []
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
    rows.append({"id": AUTO_MODEL, "display_name": "Auto — routed per task"})
    return rows


def or_key():
    try:
        with open(KEYFILE) as f:
            return f.read().strip()
    except OSError:
        return None


def upstream_for(model):
    """Provider-qualified slugs (with a /) go to OpenRouter; bare ids to Anthropic."""
    return "openrouter" if "/" in (model or "") else "anthropic"


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
                elif requested.startswith(FAMILY_PREFIX):
                    model = resolve_family(req, requested[len(FAMILY_PREFIX):])
                if model != requested:
                    req["model"] = model
                    body = json.dumps(req).encode()

        target = upstream_for(model)
        if target == "openrouter":
            key = or_key()
            if not key:
                return self.fail(500, "no OpenRouter key at ~/.config/openrouter-key")
            host, path = "openrouter.ai", "/api" + self.path
            headers = self.headers_for_openrouter(key)
            body = sanitize(body)
        else:
            host, path = "api.anthropic.com", self.path
            headers = self.headers_passthrough()
        if body:
            # The body may have been rewritten (auto) or trimmed (sanitize), so
            # the client's Content-Length can no longer be trusted on either path.
            for k in [k for k in headers if k.lower() == "content-length"]:
                del headers[k]
            headers["Content-Length"] = str(len(body))

        self.relay("POST" if body else self.command, host, path, headers, body,
                   model=model, upstream=target, requested=requested)

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

    def relay(self, method, host, path, headers, body, model="", upstream="",
              requested=""):
        t0 = time.time()
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "upstream": upstream,
               "model_requested": model}
        if requested and requested != model:
            rec["auto_from"] = requested        # picker said auto; we chose model
        try:
            conn = http.client.HTTPSConnection(host, timeout=900)
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:
            rec.update(status=502, error=str(e), ms=int((time.time() - t0) * 1000))
            log_request(rec)
            return self.fail(502, "upstream %s: %s" % (host, e))

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
                     "provider/slug -> OpenRouter)\n" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Router).serve_forever()
