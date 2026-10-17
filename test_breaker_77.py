"""Breaker round for PR #77 (re-pin /gemma/or to DeepInfra). Run: python3 -m pytest -q test_breaker_77.py"""
import importlib.util, http.client, json, os, threading
from http.server import ThreadingHTTPServer

import pytest

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

PIN = {"only": ["deepinfra"], "allow_fallbacks": False, "zdr": True}
MSG = [{"role": "user", "content": "x"}]


# --- 1. a second path that sends the same Gemma model to OpenRouter, unpinned ---
def test_break_model_path_for_same_gemma_is_not_pinned():
    # Premise: the catalog maps this exact model id to the same OpenRouter key.
    assert r.BY_ID["google/gemma-4-31b-it"]["upstream"] == "openrouter-gemma"
    body = r.sanitize(json.dumps({"model": "google/gemma-4-31b-it", "messages": MSG,
                                  "max_tokens": 100}).encode())
    prov = json.loads(body)["provider"]
    assert prov.get("only") == ["deepinfra"]          # actual: {'sort': 'throughput'}
    assert prov.get("allow_fallbacks") is False       # actual: key absent, fallbacks allowed


# --- 2. the pin dict is one shared object, so a returned body can mutate it ---
def test_break_returned_provider_is_alias_of_global_pin():
    req = {"model": "x", "messages": MSG, "tools": []}
    saved = list(r.GEMMA_OR_PROVIDER["only"])
    try:
        out = r.gemma_or_body(req)
        out["provider"]["only"].append("reka")
        assert r.GEMMA_OR_PROVIDER["only"] == ["deepinfra"]     # actual: ['deepinfra', 'reka']
        assert r.gemma_or_body(req)["provider"]["only"] == ["deepinfra"]
    finally:
        r.GEMMA_OR_PROVIDER["only"][:] = saved


# --- 3. stale docstring: the pinned provider is no longer the cheapest ---
def test_break_docstring_still_claims_cheapest_provider():
    doc = r.gemma_or_body.__doc__ or ""
    assert "CHEAPEST" not in doc                      # actual: 'the CHEAPEST zero-retention provider'


# --- wire-level checks through the real handler, upstream faked ---
CAPTURED = []
RealConn = http.client.HTTPConnection   # captured before the fixture patches it


class FakeResp:
    status = 200

    def read(self):
        return b'{"usage":{"prompt_tokens":1,"completion_tokens":1},"provider":"DeepInfra"}'

    def getheader(self, k, d=None):
        return "application/json"


class FakeConn:
    def __init__(self, host, timeout=None):
        self.host = host

    def request(self, method, path, body=None, headers=None):
        CAPTURED.append({"host": self.host, "path": path, "body": body})

    def getresponse(self):
        return FakeResp()


@pytest.fixture
def server(monkeypatch):
    CAPTURED.clear()
    monkeypatch.setattr(r.http.client, "HTTPSConnection", FakeConn)
    monkeypatch.setattr(r.http.client, "HTTPConnection", FakeConn)
    monkeypatch.setattr(r, "read_key", lambda path: "k")
    monkeypatch.setattr(r, "take_turn", lambda model: True)
    monkeypatch.setattr(r, "log_request", lambda rec: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), r.Router)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def post(port, path, obj=None, raw=None):
    data = raw if raw is not None else json.dumps(obj).encode()
    c = RealConn("127.0.0.1", port, timeout=10)
    c.request("POST", path, body=data, headers={"Content-Type": "application/json"})
    resp = c.getresponse()
    out = resp.status
    resp.read()
    c.close()
    return out


def test_break_non_object_json_to_or_route_gets_an_error_not_a_dropped_connection(server):
    # Pre-existing: gemma_or_body does dict(req, ...), which raises on a JSON list.
    # Not introduced by #77, but the same route the pin protects.
    assert post(server, "/gemma/or/v1/chat/completions", raw=b"[1, 2]") == 400
