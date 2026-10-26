"""Kimi OAuth renewal, dead-grant handling and atomic token saves (#100).

Run: python3 -m pytest -q test_kimi_renew.py

Hermetic: HOME is a tmp dir, the Kimi provider points at a fake OAuth server on
127.0.0.1, and the API connection is a fake. No test reaches auth.kimi.com or
the real ~/.config credential files.
"""
import http.client, importlib.util, io, json, os, stat, sys, threading, time, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("_r", os.path.join(HERE, "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

OLD = "sk-old-SECRET-a1"
NEW = "sk-new-SECRET-b2"
RT1 = "rt-SECRET-c3"
RT2 = "rt-SECRET-d4"
RT_LEAK = "rt-SECRET-e5"

API_CALLS = []          # (host, path, Authorization) for every fake API request


class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status, self.body = status, body
        self._sent = False

    def getheader(self, name):
        return str(len(self.body)) if name == "content-length" else None

    def getheaders(self):
        return [("content-length", str(len(self.body)))]

    def read(self, *a):
        return self.body

    def read1(self, n):
        if self._sent:
            return b""
        self._sent = True
        return self.body


class FakeConn:
    def close(self):
        pass


class FakeHTTPS:
    """Stands in for http.client.HTTPSConnection. Only NEW is accepted."""
    def __init__(self, host, timeout=None):
        self.host = host
        self._ok = False

    def request(self, method, path, body=None, headers=None):
        auth = (headers or {}).get("Authorization")
        API_CALLS.append((self.host, path, auth))
        self._ok = auth == "Bearer " + NEW

    def getresponse(self):
        if self._ok:
            return FakeResp(200, b'{"ok":true}')
        return FakeResp(401, b"{}")

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(r, "_renew_failed", {})
    monkeypatch.setattr(r.http.client, "HTTPSConnection", FakeHTTPS)
    API_CALLS.clear()

    state = {"mode": "ok", "hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            state["hits"] += 1
            mode = state["mode"]
            if mode == "ok":
                code, out = 200, {"access_token": NEW, "refresh_token": RT2, "expires_in": 900}
            elif mode == "dead":
                code, out = 400, {"error": "invalid_grant",
                                  "error_description": "The provided authorization grant is invalid"}
            elif mode == "flaky":
                code, out = 503, {}
            else:                       # "noaccess": 200 with no access_token
                code, out = 200, {"refresh_token": RT_LEAK}
            body = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    keyfile = tmp_path / "kimi-key"
    tokenfile = tmp_path / "kimi-oauth.json"
    keyfile.write_text(OLD)
    tokenfile.write_text(json.dumps({"access_token": OLD, "refresh_token": RT1,
                                     "expires_in": 900, "obtained_at": 0}))
    prov = {"host": "api.kimi.test", "key_file": str(keyfile),
            "auth_header": "Authorization", "auth_prefix": "Bearer",
            "oauth": {"host": "http://127.0.0.1:%d" % srv.server_address[1],
                      "client_id": "test", "token_path": "/api/oauth/token",
                      "token_file": str(tokenfile),
                      "device_id_file": str(tmp_path / "devid")}}
    monkeypatch.setitem(r.PROVIDERS, "kimi", prov)

    def att(key=OLD):
        return {"vendor": "kimi", "upstream": "kimi", "host": "api.kimi.test",
                "path": "/coding/v1/messages", "body": b"{}", "model": "kimi/k2.8",
                "headers": {"Authorization": "Bearer " + key}, "key": key}

    env = types.SimpleNamespace(tmp=tmp_path, state=state, prov=prov, keyfile=keyfile,
                                tokenfile=tokenfile, att=att)
    yield env
    srv.shutdown()


def issue(att):
    rec = {"upstream": att["upstream"], "model_requested": att["model"]}
    conn, resp = r.Router.issue(None, "POST", att, rec)
    return rec, resp


def relay_harness(monkeypatch, recs):
    h = r.Router.__new__(r.Router)
    monkeypatch.setattr(r, "log_request", recs.append)
    monkeypatch.setattr(r, "take_turn", lambda model: True)
    monkeypatch.setattr(r, "maybe_fetch_quota", lambda upstream: None)
    h.fail = lambda code, msg: ("fail", code, msg)
    h.send_response = h.send_header = h.end_headers = lambda *a: None
    h.wfile = io.BytesIO()
    return h



# 1. a 401 renews once, retries with the new token, and persists both files --
def test_401_renews_once_then_retries(env):
    rec, resp = issue(env.att())
    assert resp.status == 200
    assert rec["renewed"] == "kimi"
    assert [c[2] for c in API_CALLS] == ["Bearer " + OLD, "Bearer " + NEW]
    assert env.keyfile.read_text() == NEW
    assert json.loads(env.tokenfile.read_text())["refresh_token"] == RT2
    for p in (env.keyfile, env.tokenfile):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not [p for p in os.listdir(env.tmp) if p.endswith(".tmp")]
    assert env.state["hits"] == 1


# 2. a dead grant is cached: one OAuth call, one loud line, both attempts flagged
def test_dead_grant_is_cached_and_loud_once(env, capsys):
    env.state["mode"] = "dead"
    rec1, resp1 = issue(env.att())
    rec2, resp2 = issue(env.att())
    assert resp1.status == resp2.status == 401
    assert env.state["hits"] == 1
    assert rec1["auth_dead"] is True and rec2["auth_dead"] is True
    assert "400" in rec1["auth_error"]
    err = capsys.readouterr().err
    assert err.count(r.relogin_cmd("kimi")) == 1


# 3. a re-login (token file rewritten) clears the dead state and renews again
def test_relogin_clears_dead_state(env):
    env.state["mode"] = "dead"
    issue(env.att())
    assert env.state["hits"] == 1
    env.state["mode"] = "ok"
    env.tokenfile.write_text(json.dumps({"access_token": OLD, "refresh_token": RT1,
                                         "expires_in": 900, "obtained_at": 0}))
    t = os.stat(env.tokenfile).st_mtime + 2
    os.utime(env.tokenfile, (t, t))
    rec, resp = issue(env.att())
    assert env.state["hits"] == 2
    assert resp.status == 200
    assert "auth_dead" not in rec


# 4. a transient failure is not treated as dead, so the next request tries again
def test_transient_failure_is_not_dead(env):
    env.state["mode"] = "flaky"
    rec1, _ = issue(env.att())
    assert rec1["auth_dead"] is False
    rec2, _ = issue(env.att())
    assert env.state["hits"] == 2
    assert rec2["auth_dead"] is False


# 5. relay: dead auth answers the caller with a 403 and the re-login command --
def test_relay_dead_auth_fails_loud_403(monkeypatch):
    recs = []
    h = relay_harness(monkeypatch, recs)

    def issue_stub(method, att, rec):
        rec["auth_error"], rec["auth_dead"] = "HTTP 400: x", True
        return FakeConn(), FakeResp(401)

    h.issue = issue_stub
    att = {"upstream": "kimi", "vendor": "kimi", "model": "kimi/k2.8", "host": "h",
           "path": "/p", "body": b"", "headers": {}}
    out = h.relay("POST", [att], run="")
    assert out[0] == "fail" and out[1] == 403
    assert "! python3 " in out[2] and "login kimi --oauth" in out[2]
    assert h.wfile.getvalue() == b""
    assert recs[-1]["status"] == 401
    assert recs[-1]["error"].startswith("auth dead:")


# 5b. negative control: a 401 with no auth_error still streams through as today
def test_relay_plain_401_is_not_rewritten(monkeypatch):
    recs = []
    h = relay_harness(monkeypatch, recs)
    h.issue = lambda method, att, rec: (FakeConn(), FakeResp(401, b'{"e":1}'))
    att = {"upstream": "kimi", "vendor": "kimi", "model": "kimi/k2.8", "host": "h",
           "path": "/p", "body": b"", "headers": {}}
    out = h.relay("POST", [att], run="")
    assert not (isinstance(out, tuple) and out[0] == "fail")
    assert not recs[-1].get("error", "").startswith("auth")


# 6. relay: transient auth failure answers 503, no re-login advice ------------
def test_relay_transient_auth_fails_503(monkeypatch):
    recs = []
    h = relay_harness(monkeypatch, recs)

    def issue_stub(method, att, rec):
        rec["auth_error"], rec["auth_dead"] = "HTTP 503: no access_token", False
        return FakeConn(), FakeResp(401)

    h.issue = issue_stub
    att = {"upstream": "kimi", "vendor": "kimi", "model": "kimi/k2.8", "host": "h",
           "path": "/p", "body": b"", "headers": {}}
    out = h.relay("POST", [att], run="")
    assert out[0] == "fail" and out[1] == 503
    assert "python3" not in out[2]
    assert recs[-1]["error"].startswith("auth renew failed:")


# 7. invariant: an unrenewable Kimi 401 never reaches a later hop -------------
@pytest.mark.parametrize("dead", [True, False])
def test_dead_auth_never_reaches_a_later_hop(monkeypatch, env, dead):
    checked = 0
    for policy in (r.POLICY_DEFAULT, {"data": "any"}):
        for m in list(r.BY_ID):
            chain = r.attempts_for(m, policy, False)
            idx = next((i for i, hop in enumerate(chain)
                        if r.upstream_for(hop) == "direct:kimi"), None)
            if idx is None:
                continue
            checked += 1
            recs = []
            h = relay_harness(monkeypatch, recs)
            calls = []

            def issue_stub(method, att, rec, _calls=calls, _kimi=chain[idx]):
                _calls.append(att["model"])
                if att["model"] == _kimi:
                    rec["auth_error"], rec["auth_dead"] = "HTTP 400: x", dead
                    return FakeConn(), FakeResp(401)
                return FakeConn(), FakeResp(429)

            h.issue = issue_stub
            prepared = [{"upstream": r.upstream_for(hop).split(":")[-1], "model": hop,
                         "vendor": r.upstream_for(hop).split(":")[-1],
                         "host": "h", "path": "/p", "body": b"", "headers": {}}
                        for hop in chain]
            out = h.relay("POST", prepared, run="")
            assert calls == chain[:idx + 1], (m, calls, chain)
            assert out[0] == "fail" and out[1] in (403, 503), (m, out)
    assert checked > 0, "catalog no longer chains to kimi; this test went vacuous"


# 8. no token value reaches stderr, the request log or the caller -------------
@pytest.mark.parametrize("scenario", ["ok", "dead", "flaky", "noaccess"])
def test_no_secret_reaches_any_output(monkeypatch, env, capsys, scenario):
    env.state["mode"] = scenario
    recs = []
    h = relay_harness(monkeypatch, recs)
    out = h.relay("POST", [env.att()], run="")
    text = capsys.readouterr().err + json.dumps(recs, default=str) + repr(out)
    text += h.wfile.getvalue().decode("utf-8", "replace")
    for secret in (OLD, NEW, RT1, RT2, RT_LEAK):
        assert secret not in text, scenario


# 9. save_oauth replaces both files atomically at mode 0600 -------------------
def test_save_oauth_is_atomic(env, monkeypatch):
    sys.path.insert(0, HERE)
    import switchboard as sb
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append(os.path.abspath(dst))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    sb.save_oauth("kimi", env.prov, {"access_token": NEW, "refresh_token": RT2,
                                     "expires_in": 900})
    assert set(replaced) == {os.path.abspath(str(env.tokenfile)),
                             os.path.abspath(str(env.keyfile))}
    assert not [p for p in os.listdir(env.tmp) if p.endswith(".tmp")]
    for p in (env.keyfile, env.tokenfile):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert env.keyfile.read_text() == NEW
