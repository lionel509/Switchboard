#!/usr/bin/env python3
"""Kimi OAuth refresh, shown end to end with fake tokens (#101).

Run: python3 examples/kimi_refresh_demo.py

Three scenarios drive the real Router.relay -> Router.issue -> renew_key ->
oauth_refresh -> save_oauth path:
  1. expired access token, refresh succeeds: caller gets 200, key file renewed.
  2. refresh token dead (invalid_grant): caller gets 403 with the `!` re-login
     line, and nothing failed over to another model.
  3. hostile OAuth reply carrying fake tokens: caller and log see fixed text only.

Hermetic: HOME and the request log point at a temp dir, the OAuth POST is a stub
(no socket is opened), and the API connection is a fake that accepts FAKE-NEW only.
"""
import atexit, contextlib, http.client, io, json, os, sys, tempfile

TMP = tempfile.TemporaryDirectory(prefix="kimi-refresh-demo-")
atexit.register(TMP.cleanup)
os.environ["HOME"] = TMP.name                                   # before router import
os.environ["CLAUDE_ROUTER_LOG"] = os.path.join(TMP.name, "requests.log")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import router as r            # noqa: E402
import switchboard as sbm     # noqa: E402

sys.stdout.reconfigure(errors="replace")

OLD, NEW = "FAKE-OLD", "FAKE-NEW"
RT1, RT2 = "FAKE-RT1", "FAKE-RT2"
FAKE_TOKENS = (OLD, NEW, RT1, RT2)
KIMI_HOST, OTHER_HOST = "api.kimi.test", "api.other.test"

API_CALLS = []                # (host, Authorization) for every fake API request
OAUTH_CALLS = []              # refresh tokens sent to the OAuth host
OAUTH_REPLY = {"status": 200, "body": {}}
OUT = []                      # every line printed, for the token scan


def say(line=""):
    OUT.append(line)
    print(line)


def header(title):
    say("")
    say("=== " + title + " ===")


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


class FakeConn:
    def close(self):
        pass


class FakeHTTPS:
    """Stands in for http.client.HTTPSConnection. Only NEW is accepted."""

    def __init__(self, host, timeout=None):
        self.host, self._ok = host, False

    def request(self, method, path, body=None, headers=None):
        auth = (headers or {}).get("Authorization")
        API_CALLS.append((self.host, auth))
        self._ok = auth == "Bearer " + NEW

    def getresponse(self):
        return FakeResp(200, b'{"ok":true}') if self._ok else FakeResp(401, b"{}")

    def close(self):
        pass


def fake_oauth_post(oa, path, fields):
    """Stub for switchboard.oauth_post: returns the canned (status, reply)."""
    OAUTH_CALLS.append(fields.get("refresh_token"))
    return OAUTH_REPLY["status"], OAUTH_REPLY["body"]


def setup(name, status, body):
    d = os.path.join(TMP.name, name)
    os.makedirs(d)
    keyfile, tokenfile = os.path.join(d, "kimi-key"), os.path.join(d, "kimi-oauth.json")
    with open(keyfile, "w") as f:
        f.write(OLD)
    with open(tokenfile, "w") as f:
        json.dump({"access_token": OLD, "refresh_token": RT1, "expires_in": 900,
                   "obtained_at": 0}, f)
    r.PROVIDERS["kimi"] = {
        "host": KIMI_HOST, "key_file": keyfile,
        "auth_header": "Authorization", "auth_prefix": "Bearer",
        "oauth": {"host": "https://oauth.invalid", "client_id": "demo",
                  "token_path": "/api/oauth/token", "token_file": tokenfile,
                  "device_id_file": os.path.join(d, "devid")}}
    r._renew_failed.clear()
    API_CALLS.clear()
    OAUTH_CALLS.clear()
    OAUTH_REPLY.update(status=status, body=body)
    return keyfile, tokenfile


def kimi_att(key):
    return {"vendor": "kimi", "upstream": "kimi", "host": KIMI_HOST,
            "path": "/coding/v1/messages", "body": b"{}", "model": "kimi/k2.8",
            "headers": {"Authorization": "Bearer " + key}, "key": key}


def other_att():
    """The next hop a failover would take. It must never be reached."""
    return {"upstream": "other", "host": OTHER_HOST, "path": "/v1/messages",
            "body": b"{}", "model": "other/m1",
            "headers": {"Authorization": "Bearer FAKE-OTHER"}}


def call(prepared):
    """Run the real relay and capture what the caller sees: status, body, stderr."""
    h = r.Router.__new__(r.Router)
    h.wfile = io.BytesIO()
    h.statuses = []
    h.send_response = lambda code, *a: h.statuses.append(code)
    h.send_header = lambda *a: None
    h.end_headers = lambda: None
    with contextlib.redirect_stderr(io.StringIO()) as err:
        h.relay("POST", prepared, requested="", run="")
    return h.statuses[-1], h.wfile.getvalue(), err.getvalue()


def last_log():
    with open(r.LOGFILE) as f:
        return json.loads([ln for ln in f if ln.strip()][-1])


def read(path):
    with open(path) as f:
        return f.read()


def say_stderr(err):
    say("router stderr:")
    for line in err.splitlines():
        say("  " + line)


def scenario_1():
    header("(1) expired access token, refresh succeeds")
    keyfile, tokenfile = setup("s1", 200, {"access_token": NEW, "refresh_token": RT2,
                                           "expires_in": 900})
    status, body, err = call([kimi_att(OLD), other_att()])
    say("caller: HTTP %d %s" % (status, body.decode()))
    kimi = [auth for host, auth in API_CALLS if host == KIMI_HOST]
    say("API attempts: %d" % len(kimi))
    say("  first attempt sent the old token: %s" % (kimi[0] == "Bearer " + OLD))
    say("  retry sent the renewed token: %s" % (kimi[-1] == "Bearer " + NEW))
    say("key file holds the new token: %s" % (read(keyfile) == NEW))
    say("token file refresh token rotated: %s"
        % (json.loads(read(tokenfile))["refresh_token"] == RT2))
    say("OAuth refreshes: %d" % len(OAUTH_CALLS))
    say_stderr(err)
    assert status == 200 and body == b'{"ok":true}'
    assert read(keyfile) == NEW


def scenario_2():
    header("(2) refresh token dead (invalid_grant)")
    keyfile, _ = setup("s2", 400, {"error": "invalid_grant",
                                   "error_description": "The provided authorization grant is invalid"})
    status, body, err = call([kimi_att(OLD), other_att()])
    msg = json.loads(body)["error"]["message"]
    say("caller: HTTP %d" % status)
    say("caller message: " + msg)
    say_stderr(err)
    rec = last_log()
    say("log line: " + json.dumps(rec, sort_keys=True))
    say("calls to another model: %d" % sum(1 for h, _ in API_CALLS if h == OTHER_HOST))
    say("key file unchanged: %s" % (read(keyfile) == OLD))
    assert status == 403 and "! python3 " in msg and "login kimi --oauth" in msg
    assert not any(h == OTHER_HOST for h, _ in API_CALLS)
    assert "failed_over_to" not in rec and rec["status"] == 401


def scenario_3():
    header("(3) hostile server reply carrying fake tokens")
    hostile = {"error": "FAKE-OLD leaked FAKE-RT1 for FAKE-NEW",
               "error_description": "refresh_token FAKE-RT1 revoked; access FAKE-OLD"}
    say("server reply: HTTP 400, carries %d fake token(s) (not printed)"
        % sum(json.dumps(hostile).count(t) for t in FAKE_TOKENS))
    setup("s3", 400, hostile)
    status, body, err = call([kimi_att(OLD), other_att()])
    msg = json.loads(body)["error"]["message"]
    say("caller: HTTP %d %s" % (status, msg))
    say("log line: " + json.dumps(last_log(), sort_keys=True))
    say_stderr(err)
    leaked = sum("".join(OUT).count(t) for t in FAKE_TOKENS)
    say("fake tokens in any output: %d" % leaked)
    assert leaked == 0, "fake token reached the output"
    assert status == 403 and "unrecognised error" in msg


def main():
    real_conn, real_post = http.client.HTTPSConnection, sbm.oauth_post
    http.client.HTTPSConnection = FakeHTTPS
    sbm.oauth_post = fake_oauth_post
    try:
        scenario_1()
        scenario_2()
        scenario_3()
    finally:
        http.client.HTTPSConnection = real_conn
        sbm.oauth_post = real_post
    say("")
    say("all three scenarios passed")


if __name__ == "__main__":
    main()
