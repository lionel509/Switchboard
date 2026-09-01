#!/usr/bin/env python3
"""Route Claude Code requests by model name, so /model switches providers.

  model has no "/"  ->  api.anthropic.com, forwarding whatever credential the
                        client sent, untouched. Your claude.ai subscription.
  model has a "/"   ->  openrouter.ai (e.g. x-ai/grok-4.6), using the key in
                        ~/.config/openrouter-key.

Nothing is stored. The subscription token is forwarded, never read or written.
"""
import http.client, json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT     = int(os.environ.get("CLAUDE_ROUTER_PORT", "8787"))
KEYFILE  = os.path.expanduser("~/.config/openrouter-key")
LOGFILE  = os.path.expanduser(os.environ.get(
    "CLAUDE_ROUTER_LOG", "~/.local/share/claude-router/requests.log"))
# substrings of OpenRouter model ids to surface in /model
SURFACE  = [s for s in os.environ.get("CLAUDE_ROUTER_MODELS", "grok").split(",") if s]

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
    for frag, prefs in PROVIDER_PREFS.items():
        if frag in (model or "").lower():
            return prefs
    return DEFAULT_PREFS


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

        model = ""
        if body:
            try:
                model = json.loads(body).get("model", "") or ""
            except Exception:
                pass

        target = upstream_for(model)
        if target == "openrouter":
            key = or_key()
            if not key:
                return self.fail(500, "no OpenRouter key at ~/.config/openrouter-key")
            host, path = "openrouter.ai", "/api" + self.path
            headers = self.headers_for_openrouter(key)
            body = sanitize(body)
            headers["Content-Length"] = str(len(body))
        else:
            host, path = "api.anthropic.com", self.path
            headers = self.headers_passthrough()

        self.relay("POST" if body else self.command, host, path, headers, body,
                   model=model, upstream=target)

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

    def relay(self, method, host, path, headers, body, model="", upstream=""):
        t0 = time.time()
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "upstream": upstream,
               "model_requested": model}
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

        key = or_key()
        if key:
            try:
                c = http.client.HTTPSConnection("openrouter.ai", timeout=30)
                c.request("GET", "/api/v1/models",
                          headers={"Authorization": "Bearer " + key})
                for m in json.loads(c.getresponse().read()).get("data", []):
                    mid = m.get("id", "")
                    # skip async batch endpoints; keep ~latest aliases
                    if ":" in mid:
                        continue
                    if any(s in mid.lower() for s in SURFACE):
                        out.append({"type": "model", "id": mid,
                                    "display_name": m.get("name") or mid,
                                    "created_at": "2026-01-01T00:00:00Z"})
                c.close()
            except Exception as e:
                self.log_message("openrouter model list failed: %s", e)

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
