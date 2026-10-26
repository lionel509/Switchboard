"""Breaker round 2 for PR #101 (head 0e2279c). Throwaway; not part of the PR.

Run: timeout 300 python3 -m pytest -q test_break_101_r2.py
Hermetic: HOME is tmp, fake OAuth server on 127.0.0.1, fake HTTPS API.
"""
import http.client, importlib.util, io, json, os, shlex, threading, time, types, urllib.parse
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
API_CALLS = []


class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status, self.body, self._sent = status, body, False

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


class FakeHTTPS:
    debuglevel = 0
    _http_vsn = 11
    _http_vsn_str = "HTTP/1.1"

    def __init__(self, host, timeout=None):
        self.host, self._ok = host, False

    def set_debuglevel(self, level):
        self.debuglevel = level

    def request(self, method, path, body=None, headers=None):
        auth = (headers or {}).get("Authorization")
        API_CALLS.append((self.host, path, auth))
        self._ok = auth == "Bearer " + NEW

    def getresponse(self):
        return FakeResp(200, b'{"ok":true}') if self._ok else FakeResp(401, b"{}")

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
    # reply: the JSON the fake OAuth host returns for a refresh; status: its HTTP code
    state = {"hits": 0, "status": 200, "reply": {"access_token": NEW, "refresh_token": RT2},
             "sent_rts": [], "delay": 0.0, "rotate": True, "accept_rt": RT1}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            state["hits"] += 1
            state["sent_rts"].append(form.get("refresh_token", [None])[0])
            if state["delay"]:
                time.sleep(state["delay"])
            reply, status = state["reply"], state["status"]
            if state["rotate"] and form.get("refresh_token", [None])[0] != state["accept_rt"]:
                reply, status = {"error": "invalid_grant", "error_description": "spent"}, 400
            elif status == 200 and state["rotate"] and isinstance(reply.get("refresh_token"), str):
                state["accept_rt"] = reply["refresh_token"]      # single-use rotation
            body = json.dumps(reply).encode()
            self.send_response(status)
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

    e = types.SimpleNamespace(tmp=tmp_path, state=state, prov=prov, keyfile=keyfile,
                              tokenfile=tokenfile, att=att)
    yield e
    srv.shutdown()


# Break 1: a refresh reply with refresh_token null (or "") overwrites the stored
# grant. The code comment says the old refresh token is kept when the server omits
# one, but setdefault() does not replace an explicit null or empty string.
@pytest.mark.parametrize("bad", [None, ""])
def test_break1_null_refresh_token_in_reply_kills_grant(env, bad):
    env.state["reply"] = {"access_token": NEW, "refresh_token": bad}
    assert r.renew_key("kimi", env.prov, OLD) == NEW          # first renewal: fine
    # access token expired again; the next renewal should still reach the server
    assert r.renew_key("kimi", env.prov, NEW) == NEW, r._renew_failed
    assert env.state["hits"] == 2, "second renewal never reached the OAuth host"
    assert env.state["sent_rts"][-1] == RT1


# Break 2: the server's error text goes verbatim into the reason, which is printed to
# stderr, cached, written to requests.log and returned to the client. A server that
# echoes the rejected credential in error_description leaks it everywhere.
def test_break2_server_error_text_echoing_refresh_token_is_logged(env, monkeypatch, capsys):
    env.state["status"] = 400
    env.state["reply"] = {"error": "invalid_grant",
                          "error_description": "refresh_token %s is revoked" % RT1}
    recs = []
    monkeypatch.setattr(r, "log_request", recs.append)
    assert r.renew_key("kimi", env.prov, OLD) is None
    out = capsys.readouterr().err
    cached = json.dumps(r._renew_failed)
    logged = json.dumps(recs)
    assert RT1 not in out, "refresh token printed to stderr: %r" % out
    assert RT1 not in cached, "refresh token kept in _renew_failed"
    assert RT1 not in logged


# Break 3: a token file that is valid JSON but not an object (e.g. "[]" or "null")
# makes oauth_refresh raise AttributeError, which renew_key files as transient. The
# grant needs a re-login (same as a missing file, which is classed dead), so the
# caller should get the re-login command, not "retry shortly".
@pytest.mark.parametrize("content", ["[]", "null"])
def test_break3_malformed_token_file_is_transient_not_dead(env, content):
    env.tokenfile.write_text(content)
    assert r.renew_key("kimi", env.prov, OLD) is None
    f = r._renew_failed.get("kimi") or {}
    assert f.get("dead") is True, "malformed token file classed as transient: %r" % f


# Break 4: relogin_cmd() prints the script path unquoted. A checkout under a folder
# with a space gives a `!` command that the shell splits into the wrong arguments.
def test_break4_relogin_command_survives_space_in_path(env, monkeypatch, tmp_path):
    spaced = tmp_path / "My Projects" / "router.py"
    monkeypatch.setattr(r, "__file__", str(spaced))
    cmd = r.relogin_cmd("kimi")
    parts = shlex.split(cmd)
    assert parts[1] == str(tmp_path / "My Projects" / "switchboard.py"), parts


# Break 5: the token file is rewritten (rotation) but the write of the token file
# fails after the server has already rotated the refresh token. The stored refresh
# token is now spent, so the next renewal is refused and the grant is marked dead,
# even though the new token was valid and only the disk write failed.
def test_break5_failed_save_after_rotation_marks_grant_dead(env, monkeypatch):
    import switchboard
    real_replace = os.replace
    tf = str(env.tokenfile)

    def failing_replace(src, dst):
        if os.path.abspath(dst) == os.path.abspath(tf):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(switchboard.os, "replace", failing_replace)
    # server rotates: it accepts RT1 once, then only RT2
    env.state["reply"] = {"access_token": NEW, "refresh_token": RT2}
    assert r.renew_key("kimi", env.prov, OLD) is None          # save failed
    monkeypatch.setattr(switchboard.os, "replace", real_replace)
    # next expiry: the stored refresh token is still RT1 (spent on the server side)
    got = r.renew_key("kimi", env.prov, OLD)
    f = r._renew_failed.get("kimi") or {}
    assert got == NEW and f.get("dead") is not True, \
        "failed disk write turned into a dead grant: got=%r state=%r sent=%r" % (
            got, f, env.state["sent_rts"])
