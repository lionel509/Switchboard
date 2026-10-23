"""Apple Foundation Models provider on a localhost shim (#98). Run: python3 -m pytest test_apple_provider.py"""
import email, http.client, importlib.util, json, os, socket, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


r = _load("_r", "router.py")
sb = _load("_sb", "switchboard.py")
APPLE = ["apple/on-device", "apple-pcc/pcc"]
MSG = {"model": "apple/on-device", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}
CANNED = {"id": "msg_apple_1", "type": "message", "role": "assistant", "model": "on-device",
          "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
          "stop_sequence": None, "usage": {"input_tokens": 3, "output_tokens": 1}}


def _headers(text):
    return email.message_from_string(text)


CLIENT_HEADERS = ("Authorization: Bearer sk-secret\r\nx-api-key: sk-ant\r\n"
                  "anthropic-beta: x\r\nContent-Type: application/json\r\n\r\n")


# --- catalog ------------------------------------------------------------------

def test_catalog_rows():
    a, p = r.PROVIDERS["apple"], r.PROVIDERS["apple-pcc"]
    for prov in (a, p):
        assert prov.get("local") is True and prov.get("auto") is False
        assert "key_file" not in prov and "path_prefix" not in prov and "quota" not in prov
        assert r.is_loopback(prov["host"])
    assert a.get("zdr") is True and p.get("zdr") is not True
    rows = {m["id"]: m for m in r.CANDIDATES if m["id"] in APPLE}
    assert set(rows) == set(APPLE)
    assert rows["apple/on-device"]["context"] == 4096
    assert rows["apple-pcc/pcc"]["context"] == 32768
    for m in rows.values():
        assert m["billing"] == "subscription" and m["price"] == [0, 0]
        assert m["failover"] == []
    assert not set(APPLE) & set(r.PICKER.get("models", []))
    assert not any(x.get("model") in APPLE for x in r.CATALOG.get("picker_lineup", []))
    assert "apple" in r.CATALOG["family_labels"] and "apple-pcc" in r.CATALOG["family_labels"]


# --- money and privacy invariants --------------------------------------------

def test_never_a_failover_target_and_never_fails_over(monkeypatch):
    monkeypatch.setitem(r.CATALOG, "failover_default", ["kimi/k3", "xiaomi/mimo-v2.6-pro"])
    for m in r.CANDIDATES:
        assert not any(f in APPLE for f in r.failover_chain(m["id"])), m["id"]
    for mid in APPLE:
        assert r.failover_chain(mid) == []


def test_folder_policy_splits_on_device_from_pcc():
    for d in ("any", "zdr", "claude"):
        pol = {"data": d}
        assert r.allowed("apple/on-device", pol) == (d in ("any", "zdr")), d
        assert r.allowed("apple-pcc/pcc", pol) == (d == "any"), d
    assert r.attempts_for("apple-pcc/pcc", {"data": "zdr"}) == []


# --- loopback check -----------------------------------------------------------

PORTS = ["", ":1", ":8788", ":65535"]


def test_is_loopback_accepts_only_bare_loopback():
    for host in ("127.0.0.1", "127.5.6.7", "localhost", "[::1]"):
        for port in PORTS:
            assert r.is_loopback(host + port), host + port


def test_is_loopback_refuses_lookalikes():
    bad = ["127.0.0.1.evil.com", "localhost.evil.com", "evil.com", "10.0.0.1", "0.0.0.0",
           "192.168.1.2", "[::]", "127.0.0.1@evil.com", "user@localhost", "::1",
           "127.0.0.1/x", ""]
    for host in bad:
        for port in PORTS:
            assert not r.is_loopback(host + port), host + port


# --- prepare and issue --------------------------------------------------------

def test_prepare_strips_credentials_and_marks_local():
    for model, wire in (("apple/on-device", "on-device"), ("apple-pcc/pcc", "pcc")):
        h = r.Router.__new__(r.Router)
        h.path = "/v1/messages?beta=true"
        h.headers = _headers(CLIENT_HEADERS)
        att = h.prepare(model, json.dumps(dict(MSG, model=model)).encode())
        assert isinstance(att, dict), att
        assert att["local"] is True
        assert att["host"] == r.PROVIDERS[att["vendor"]]["host"]
        assert att["path"].endswith("/v1/messages?beta=true")
        assert not any(k.lower() in ("authorization", "x-api-key", "anthropic-beta")
                       for k in att["headers"])
        assert not any("sk-" in str(v) for v in att["headers"].values())
        assert json.loads(att["body"])["model"] == wire


def test_local_flag_on_a_remote_host_is_refused(monkeypatch):
    monkeypatch.setitem(r.PROVIDERS, "apple", dict(r.PROVIDERS["apple"], host="evil.com:80"))
    h = r.Router.__new__(r.Router)
    h.path, h.headers = "/v1/messages", _headers(CLIENT_HEADERS)
    got = h.prepare("apple/on-device", json.dumps(MSG).encode())
    assert isinstance(got, str) and "not loopback" in got
    made = []
    monkeypatch.setattr(http.client, "HTTPConnection", lambda *a, **k: made.append(a))
    att = {"local": True, "host": "evil.com:80", "path": "/v1/messages", "body": b"",
           "headers": {}, "vendor": "apple", "key": ""}
    try:
        r.Router.issue(h, "POST", att, {})
        raise AssertionError("issue() must refuse a non-loopback local host")
    except OSError:
        pass
    assert made == []


def _recording_connections(monkeypatch):
    seen = []

    class Conn:
        def __init__(self, host, timeout=None):
            seen.append((type(self), host))

        def request(self, *a, **k):
            pass

        def getresponse(self):
            class Resp:
                status = 200
            return Resp()

    class Plain(Conn):
        pass

    class Tls(Conn):
        pass

    monkeypatch.setattr(http.client, "HTTPConnection", Plain)
    monkeypatch.setattr(http.client, "HTTPSConnection", Tls)
    return seen, Plain, Tls


def test_only_local_providers_use_plain_http(monkeypatch):
    monkeypatch.setattr(r, "read_key", lambda path: "k")
    monkeypatch.setattr(r, "expiring", lambda key, margin=0: False)
    seen, Plain, Tls = _recording_connections(monkeypatch)
    checked = 0
    for name, prov in sorted(r.PROVIDERS.items()):
        if prov.get("wire") == "openai":
            continue
        cand = next(m["id"] for m in r.CANDIDATES if m["id"].startswith(name + "/"))
        h = r.Router.__new__(r.Router)
        h.path, h.headers = "/v1/messages", _headers(CLIENT_HEADERS)
        att = h.prepare(cand, json.dumps(dict(MSG, model=cand)).encode())
        assert isinstance(att, dict), (name, att)
        seen.clear()
        r.Router.issue(h, "POST", att, {})
        kind = seen[0][0]
        if name in ("apple", "apple-pcc"):
            assert kind is Plain, name
        else:
            assert kind is Tls, name
        checked += 1
    assert checked >= 3


# --- end to end ---------------------------------------------------------------

def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _point_apple_at(monkeypatch, port):
    monkeypatch.setitem(r.PROVIDERS, "apple", dict(r.PROVIDERS["apple"], host="127.0.0.1:%d" % port))


def _post(port, body, auth=True):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdr = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if auth:
        hdr["Authorization"] = "Bearer sk-secret"
    c.request("POST", "/v1/messages?beta=true", body=json.dumps(body), headers=hdr)
    resp = c.getresponse()
    data = resp.read()
    c.close()
    return resp.status, json.loads(data)


def _ledger(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_end_to_end_through_router(tmp_path, monkeypatch):
    seen = []

    class Shim(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen.append((self.path, dict(self.headers), json.loads(self.rfile.read(n))))
            payload = json.dumps(CANNED).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    shim = _serve(Shim)
    _point_apple_at(monkeypatch, shim.server_address[1])
    monkeypatch.setattr(r, "LOGFILE", str(tmp_path / "req.log"))
    monkeypatch.setattr(r, "POLICY", {})
    front = _serve(r.Router)
    status, body = _post(front.server_address[1], MSG)
    assert status == 200 and body == CANNED
    assert len(seen) == 1
    path, headers, sent = seen[0]
    assert path == "/v1/messages?beta=true" and sent["model"] == "on-device"
    lower = {k.lower(): v for k, v in headers.items()}
    assert "authorization" not in lower and "x-api-key" not in lower
    recs = _ledger(str(tmp_path / "req.log"))
    assert len(recs) == 1 and recs[0]["upstream"] == "apple"
    assert "failover_from" not in recs[0] and "failed_over_to" not in recs[0]
    shim.shutdown(); front.shutdown()


def test_shim_down_is_a_clear_502_with_no_failover(tmp_path, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    _point_apple_at(monkeypatch, dead)
    monkeypatch.setattr(r, "LOGFILE", str(tmp_path / "req.log"))
    monkeypatch.setattr(r, "POLICY", {})
    front = _serve(r.Router)
    status, body = _post(front.server_address[1], MSG)
    assert status == 502
    assert body["error"]["type"] == "api_error"
    assert "not running" in body["error"]["message"]
    recs = _ledger(str(tmp_path / "req.log"))
    assert len(recs) == 1 and recs[0]["upstream"] == "apple"
    assert "failed_over_to" not in recs[0]
    front.shutdown()


def test_over_window_is_a_400_and_never_reaches_the_shim(tmp_path, monkeypatch):
    seen = []

    class Shim(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    shim = _serve(Shim)
    _point_apple_at(monkeypatch, shim.server_address[1])
    monkeypatch.setattr(r, "LOGFILE", str(tmp_path / "req.log"))
    monkeypatch.setattr(r, "POLICY", {})
    front = _serve(r.Router)
    big = dict(MSG, messages=[{"role": "user", "content": "x" * 20000}])
    status, body = _post(front.server_address[1], big)
    assert status == 400
    assert body["error"]["type"] == "invalid_request_error"
    assert "holds 4,096" in body["error"]["message"]
    assert seen == []
    shim.shutdown(); front.shutdown()


# --- switchboard --------------------------------------------------------------

def test_switchboard_hides_local_providers_from_login():
    with open(os.path.join(HERE, "models.json")) as f:
        cat = json.load(f)
    assert "apple" in cat["providers"] and "apple-pcc" in cat["providers"]
    names = [name for name, _p, _k in sb.provider_rows(cat)]
    assert "apple" not in names and "apple-pcc" not in names
    if "kimi" in cat["providers"]:
        assert "kimi" in names
