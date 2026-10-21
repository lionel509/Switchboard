"""Headless dispatch tiers (#46). Run: python3 -m pytest test_dispatch.py"""
import http.client, importlib.util, io, json, os, re, subprocess, sys, time, types

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

import dispatch as d

HERE = os.path.dirname(os.path.abspath(__file__))


def by_id(monkeypatch):
    monkeypatch.setattr(r, "BY_ID", {
        "claude-sonnet-5": {"id": "claude-sonnet-5", "billing": "subscription",
                            "failover": ["kimi/k2.8", "xiaomi/mimo-v2.6-pro"]},
        "kimi/k2.8": {"id": "kimi/k2.8", "billing": "subscription"},
        "xiaomi/mimo-v2.6-pro": {"id": "xiaomi/mimo-v2.6-pro",
                                 "price": [0.435, 0.87], "zdr": False},
    })


# 1. plan-only strips metered models from the attempt list -------------------
def test_attempts_for_plan_only_drops_metered(monkeypatch):
    by_id(monkeypatch)
    assert r.attempts_for("claude-sonnet-5", {"data": "any"}, True) == \
        ["claude-sonnet-5", "kimi/k2.8"]
    assert r.attempts_for("claude-sonnet-5", {"data": "any"}, False) == \
        ["claude-sonnet-5", "kimi/k2.8", "xiaomi/mimo-v2.6-pro"]
    # negative control: MiMo drops here on POLICY (zdr: false), not plan-only
    chain = r.attempts_for("claude-sonnet-5", {"data": "zdr"}, False)
    assert "xiaomi/mimo-v2.6-pro" not in chain


# 2. plan-tagged runs get a 402 when every plan-billed attempt was refused ---
class FakeResp:
    def __init__(self, status, body=b"{}", content_length=True):
        self.status, self.body, self.content_length = status, body, content_length
        self._sent = False

    def getheader(self, name):
        if name == "content-length":
            return str(len(self.body)) if self.content_length else None
        return None

    def getheaders(self):
        return [("content-length", str(len(self.body)))] if self.content_length else []

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


def relay_stub(monkeypatch, recs, statuses=(429, 429)):
    h = r.Router.__new__(r.Router)
    monkeypatch.setattr(r, "log_request", recs.append)
    monkeypatch.setattr(r, "take_turn", lambda model: True)
    monkeypatch.setattr(r, "maybe_fetch_quota", lambda upstream: None)
    # a 429 is remembered across relay() calls (#56); each stub starts clean
    monkeypatch.setattr(r, "SKIP_UNTIL", {})
    it = iter(statuses)

    def issue(method, att, rec):
        item = next(it)              # an int, or a ready FakeResp (#68 bodies)
        return FakeConn(), (item if isinstance(item, FakeResp) else FakeResp(item))

    h.issue = issue
    h.fail = lambda code, msg: ("fail", code, msg)
    h.send_response = h.send_header = h.end_headers = lambda *a: None
    h.wfile = io.BytesIO()
    return h


PREPARED = [
    dict(upstream="anthropic", model="claude-sonnet-5", host="h", headers={},
         path="/v1/messages", body=b""),
    dict(upstream="kimi", model="kimi/k2.8", host="h", headers={},
         path="/v1/messages", body=b""),
]


def test_relay_plan_only_answers_402_and_tags_run(monkeypatch):
    recs = []
    h = relay_stub(monkeypatch, recs)
    out = h.relay("POST", [dict(p) for p in PREPARED], run="plan:t1")
    assert out[:2] == ("fail", 402)
    assert "kimi/k2.8" in out[2]
    assert all(rec.get("run") == "plan:t1" for rec in recs)
    assert recs[-1]["status"] == 429
    assert recs[-1]["error"] == "plan exhausted"
    # negative control: an untagged run with the same 429s streams through
    recs2 = []
    h2 = relay_stub(monkeypatch, recs2)
    out2 = h2.relay("POST", [dict(p) for p in PREPARED], run="")
    assert out2 is None
    assert not any("run" in rec or rec.get("error") == "plan exhausted"
                   for rec in recs2)
    # a plan: run whose last attempt ANSWERED streams through -- only a final
    # capacity status becomes a 402 (this goes red if that check is dropped)
    recs3 = []
    h3 = relay_stub(monkeypatch, recs3, statuses=(429, 200))
    assert h3.relay("POST", [dict(p) for p in PREPARED], run="plan:t2") is None
    assert recs3[-1]["status"] == 200
    assert "error" not in recs3[-1]
    # same for a real upstream failure: a final 404 streams back to the caller
    recs4 = []
    h4 = relay_stub(monkeypatch, recs4, statuses=(404, 404))
    assert h4.relay("POST", [dict(p) for p in PREPARED], run="plan:t3") is None
    assert recs4[-1]["status"] == 404


# 2b. a Kimi 403 that says "usage limit" is a quota, not a credential (#68) ----
QUOTA_403 = (b"error, status code: 403, message: You've reached your monthly usage "
             b"limit for this billing cycle. Your quota will be refreshed in the next cycle.")
ACCESS_403 = b"error, status code: 403, message: You do not have access to model kimi-for-coding"
KIMI_FIRST = [PREPARED[1], PREPARED[0]]


def test_kimi_quota_403_fails_over(monkeypatch):
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, QUOTA_403), 200))
    assert h.relay("POST", [dict(p) for p in KIMI_FIRST], run="") is None
    assert recs[0]["status"] == 403
    assert recs[0]["failed_over_to"] == "claude-sonnet-5"
    assert "usage limit" in recs[0]["error"]
    assert recs[-1]["status"] == 200


def test_kimi_k3_quota_403_does_not_spill_onto_metered_hop(monkeypatch):
    # Real catalog: kimi/k3 chains to xiaomi/mimo-v2.6-pro (cash fallback, metered).
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, QUOTA_403), 200))
    models = r.attempts_for("kimi/k3", r.POLICY_DEFAULT, False)
    atts = [dict(upstream=r.upstream_for(m).split(":")[-1], model=m, host="h", headers={},
                 path="/v1/messages", body=b"") for m in models]
    assert r.metered(models[1])
    h.relay("POST", atts, run="")
    assert "failed_over_to" not in recs[0]
    assert recs[0]["status"] == 403
    assert h.wfile.getvalue() == QUOTA_403


def test_kimi_quota_403_marks_kimi_skipped(monkeypatch):
    # Merge-order pin from the #70 review: a quota 403 that moves on to a
    # plan-billed hop must be remembered like a 429, or the next run walks
    # back into the exhausted Kimi route.
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, QUOTA_403), 200))
    assert h.relay("POST", [dict(p) for p in KIMI_FIRST], run="") is None
    assert recs[0]["failed_over_to"] == "claude-sonnet-5"
    assert r.SKIP_UNTIL.get("kimi/k2.8", 0) > 0


def test_kimi_403_without_usage_limit_stays(monkeypatch):
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, ACCESS_403), 200))
    assert h.relay("POST", [dict(p) for p in KIMI_FIRST], run="") is None
    assert "failed_over_to" not in recs[0]
    assert recs[0]["status"] == 403
    assert "access" in recs[0]["error"]
    assert h.wfile.getvalue() == ACCESS_403          # bytes reach the client
    assert len(recs) == 1


def test_non_kimi_403_with_quota_text_stays(monkeypatch):
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, QUOTA_403), 200))
    assert h.relay("POST", [dict(p) for p in PREPARED], run="") is None
    assert "failed_over_to" not in recs[0]
    assert len(recs) == 1
    assert h.wfile.getvalue() == QUOTA_403


def test_kimi_quota_403_on_plan_run_is_exhausted(monkeypatch):
    # anthropic answers 429, then the Kimi hop is a quota 403 and is the last
    # attempt: it must become the plan-exhausted 402, not a raw 403.
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(429, FakeResp(403, QUOTA_403)))
    out = h.relay("POST", [dict(p) for p in PREPARED], run="plan:t4")
    assert out[:2] == ("fail", 402)
    assert recs[-1]["error"] == "plan exhausted"


def test_403_failover_invariant(monkeypatch):
    bodies = [
        b"error, status code: 403, message: You've reached your 5-hour usage limit.",
        b"error, status code: 403, message: You've reached your weekly (7-day) usage limit.",
        QUOTA_403,
        b'{"error":{"message":"You\'ve reached your 5-hour usage limit."}}',
        b"You've reached your concurrent request limit. Please wait...",
        b"invalid api key",
        b"",
        b"x" * 9000,
        b"error, status code: 403, message: Invalid API key. See the usage limit FAQ.",
        b'{"error":{"message":42}}',
        b'{"error":{"message":{"text":"You\'ve reached your 5-hour usage limit."}}}',
    ]
    models = {"anthropic": "claude-sonnet-5", "kimi": "kimi/k2.8",
              "openrouter": "xiaomi/mimo-v2.6-pro"}
    for upstream in ("kimi", "anthropic", "openrouter"):
        for status in (401, 403):
            for body in bodies:
                for content_length in (True, False):
                    first = dict(upstream=upstream, model=models[upstream], host="h",
                                 headers={}, path="/v1/messages", body=b"")
                    recs = []
                    h = relay_stub(monkeypatch, recs, statuses=(
                        FakeResp(status, body, content_length), 200))
                    h.relay("POST", [first, dict(PREPARED[0])], run="")
                    failover = (upstream == "kimi" and status == 403 and content_length
                                and len(body) <= 8192
                                and re.search(rb"(?i)reached your [^.\n]{0,40}usage limit", body) is not None)
                    cell = (upstream, status, body[:24], content_length)
                    assert ("failed_over_to" in recs[0]) == failover, cell
                    if not failover:
                        assert len(recs) == 1, cell
                        assert body in h.wfile.getvalue(), cell


def test_gemma_error_text_non_string_message():
    assert r.gemma_error_text(b'{"error":{"message":42}}') == "42"
    assert "a" in r.gemma_error_text(b'{"error":{"message":{"a":1}}}')


def test_kimi_403_non_string_message_does_not_crash(monkeypatch):
    for body in (b'{"error":{"message":42}}', b'{"error":{"message":["x"]}}',
                 b'{"error":{"message":{"a":1}}}'):
        recs = []
        h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, body), 200))
        assert h.relay("POST", [dict(p) for p in KIMI_FIRST], run="") is None
        assert recs[0]["status"] == 403
        assert "failed_over_to" not in recs[0]
        assert isinstance(recs[0]["error"], str)
        assert body in h.wfile.getvalue()


def test_kimi_403_mentioning_usage_limit_is_not_quota(monkeypatch):
    body = b"error, status code: 403, message: Invalid API key. See the usage limit FAQ."
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(FakeResp(403, body), 200))
    assert h.relay("POST", [dict(p) for p in KIMI_FIRST], run="") is None
    assert "failed_over_to" not in recs[0]
    assert len(recs) == 1


def test_403_never_drains_on_credential_fuzz(monkeypatch):
    import random
    rng = random.Random(68)
    frags = [b"You've reached your", b" monthly", b" usage limit", b"Invalid API key.",
             b" See the", b".", b"\n", b'{"error":{"message":', b"42", b'"x"', b"}}",
             b"\xff\xfe", b"limit", b"usage"]
    models = {"anthropic": "claude-sonnet-5", "kimi": "kimi/k2.8"}
    for _ in range(300):
        body = b"".join(rng.choice(frags) for _ in range(rng.randint(1, 6)))
        upstream = rng.choice(("kimi", "anthropic"))
        status = rng.choice((401, 403))
        content_length = rng.choice((True, False))
        first = dict(upstream=upstream, model=models[upstream], host="h", headers={},
                     path="/v1/messages", body=b"")
        recs = []
        h = relay_stub(monkeypatch, recs, statuses=(
            FakeResp(status, body, content_length), 200))
        h.relay("POST", [first, dict(PREPARED[0])], run="")   # must never raise
        if "failed_over_to" in recs[0]:
            low = body.lower()
            assert (upstream == "kimi" and status == 403 and content_length
                    and b"reached your" in low and b"usage limit" in low), body


# 3. meters ------------------------------------------------------------------
def test_meters_reads_fresh_files_and_ignores_stale(tmp_path):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
    (tmp_path / "rate-limits.json").write_text(json.dumps(
        {"ts": now, "five_hour_pct": 4, "seven_day_pct": 1, "spend_pct": 100}))
    (tmp_path / "kimi-limits.json").write_text(json.dumps(
        {"ts": old, "five_hour_pct": 9, "month_pct": 50}))
    m = d.meters(str(tmp_path))
    assert m["claude"] == 4
    assert "kimi" not in m
    (tmp_path / "kimi-limits.json").write_text("not json")
    assert "kimi" not in d.meters(str(tmp_path))


# 4. pick_plan ----------------------------------------------------------------
def test_pick_plan_from_meters():
    role = {"plan": "claude-sonnet-5", "kimi": "kimi/k2.8"}
    assert d.pick_plan(role, {"claude": 4, "kimi": 93}, 95) == "claude-sonnet-5"
    assert d.pick_plan(role, {"claude": 97, "kimi": 93}, 95) == "kimi/k2.8"
    assert d.pick_plan(role, {"claude": 97, "kimi": 99}, 95) is None
    assert d.pick_plan(role, {}, 95) == "claude-sonnet-5"
    assert d.pick_plan(role, {"claude": 4}, 0) is None
    # coder override: the plan model is a Kimi id, so the KIMI meter gates it.
    # Reading the Claude meter here starts the coder on k3-256k while Kimi is
    # past the line, and skips a healthy k3-256k when only Claude is hot.
    coder = {"plan": "kimi/k3-256k", "kimi": "kimi/k2.8"}
    assert d.pick_plan(coder, {"claude": 99, "kimi": 10}, 95) == "kimi/k3-256k"
    assert d.pick_plan(coder, {"claude": 8, "kimi": 96}, 95) is None


# 5. agy meter parsing ---------------------------------------------------------
QUOTA_OUT = ("Gemini Models\tWeekly Limit Remaining\t72%\t2026-10-15T08:36:51Z\n"
             "Claude Models\tWeekly Limit Remaining\t100%\t2026-10-15T08:36:51Z\n")


def test_agy_meter_and_bucket():
    assert d.agy_meter(QUOTA_OUT) == {"gemini": 28.0, "claude": 0.0}
    assert d.agy_meter("garbage") == {}
    assert d.agy_bucket("gemini-3.8-flash-high") == "gemini"
    assert d.agy_bucket("claude-opus-4-6-thinking") == "claude"
    assert d.agy_bucket("gpt-oss-120b-medium") == "claude"


# 6. folder policy gates the tiers ---------------------------------------------
def test_tier_allowed_by_folder_policy(monkeypatch):
    assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": "zdr"}) is False
    assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": "claude"}) is False
    assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": "any"}) is True
    assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {}) is True
    # dispatch imports its own router module (d.router is not the `_r` instance
    # above), so the cash gate must be patched there or the asserts run against
    # the live catalog and say nothing about this code path.
    monkeypatch.setattr(d.router, "BY_ID", {
        "xiaomi/mimo-v2.6-pro": {"id": "xiaomi/mimo-v2.6-pro",
                                 "price": [0.435, 0.87], "zdr": False},
    })
    assert d.tier_allowed("cash", "xiaomi/mimo-v2.6-pro", {"data": "zdr"}) is False
    assert d.tier_allowed("cash", "xiaomi/mimo-v2.6-pro", {"data": "any"}) is True
    assert d.tier_allowed("plan", "whatever", {"data": "zdr"}) is True


# 7. exhaustion is a capacity signal only --------------------------------------
def test_exhausted_only_on_capacity_status():
    assert d.exhausted({"is_error": True, "api_error_status": 402}, []) is True
    assert d.exhausted({"is_error": True}, [{"status": 429}]) is True
    assert d.exhausted(None, [{"status": 529}]) is True
    assert d.exhausted({"is_error": True, "api_error_status": 404},
                       [{"status": 404}]) is False
    assert d.exhausted({"is_error": True}, []) is False
    assert d.exhausted({"is_error": False},
                       [{"status": 429}, {"status": 200}]) is False


# 8. agy outcome classification -------------------------------------------------
def test_agy_outcome():
    kind, dct = d.agy_outcome(
        '{"status":"SUCCESS","response":"ok\\n",'
        '"usage":{"input_tokens":11756,"output_tokens":1}}', "")
    assert kind == "ok"
    assert dct["response"] == "ok\n"
    assert d.agy_outcome('{"status":"ERROR","error":"Out of credits"}', "")[0] == "exhausted"
    assert d.agy_outcome('{"status":"ERROR","error":"tool failed"}', "")[0] == "failed"
    assert d.agy_outcome("not json", "RESOURCE_EXHAUSTED")[0] == "exhausted"
    assert d.agy_outcome("not json", "context canceled")[0] == "failed"
    assert d.agy_outcome('{"status":"TIMEOUT"}', "")[0] == "failed"


def test_agy_outcome_timeout_and_auth_errors_are_not_exhaustion():
    # TIMEOUT/CANCELLED say nothing about quota, even if stderr mentions it
    assert d.agy_outcome('{"status":"TIMEOUT"}', "see quota details")[0] == "failed"
    assert d.agy_outcome('{"status":"CANCELLED"}', "rate limit hit")[0] == "failed"
    # an ADC/auth failure names "quota project" without meaning exhaustion
    assert d.agy_outcome('{"status":"ERROR","error":'
                         '"PERMISSION_DENIED: quota project not set for user"}',
                         "")[0] == "failed"
    # real capacity wording still counts
    assert d.agy_outcome('{"status":"ERROR","error":"Out of credits"}', "")[0] == "exhausted"
    assert d.agy_outcome('{"status":"ERROR","error":"RESOURCE_EXHAUSTED"}', "")[0] == "exhausted"


def test_agy_outcome_non_object_json():
    # valid JSON that isn't an object must classify, not crash
    assert d.agy_outcome("null", "")[0] == "failed"
    assert d.agy_outcome("[1]", "")[0] == "failed"
    assert d.agy_outcome('"x"', "")[0] == "failed"
    assert d.agy_outcome("null", "Out of credits")[0] == "exhausted"


# 9. ledger record for an antigravity run ---------------------------------------
def test_agy_record_shape():
    rec = d.agy_record("agy:abc123", "coder", "gemini-3.8-flash-high", 200, 1234,
                       {"input_tokens": 11756, "output_tokens": 1})
    assert rec["upstream"] == "antigravity"
    assert rec["run"] == "agy:abc123"
    assert rec["role"] == "coder"
    assert rec["in"] == 11756
    assert rec["out"] == 1
    assert "cost" not in rec
    assert "error" in d.agy_record("agy:x", "coder", "m", 429, 1, {}, error="boom")


# 10. ledger tail by run id ------------------------------------------------------
def test_ledger_since_filters_by_run_and_offset(tmp_path):
    p = tmp_path / "requests.log"
    # the pre-offset line carries THIS run id, so a seek(0) regression surfaces
    l1 = json.dumps({"run": "plan:me", "status": 429}) + "\n"
    rest = ("not json\n"
            + json.dumps({"run": "plan:me", "status": 200}) + "\n"
            + json.dumps({"run": "plan:other", "status": 429}) + "\n")
    p.write_text(l1 + rest)
    got = d.ledger_since(str(p), len(l1), "plan:me")
    assert got == [{"run": "plan:me", "status": 200}]


# 11. command lines ---------------------------------------------------------------
def test_cmds():
    argv = d.agy_cmd("/x/agy", "do it", "gemini-3.8-flash-high", "45m", "/tmp/f")
    assert "--dangerously-skip-permissions" in argv
    assert argv[argv.index("--print-timeout") + 1] == "45m"
    assert argv[argv.index("--add-dir") + 1] == "/tmp/f"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert not any(a.startswith("--token") or a.startswith("--key") for a in argv)
    argv = d.claude_cmd("claude-sonnet-5", ["Read", "Bash"])
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1:] == ["Read", "Bash"]


def test_run_help_exits_zero():
    p = subprocess.run([sys.executable, os.path.join(HERE, "switchboard.py"),
                        "run", "--help"], capture_output=True)
    assert p.returncode == 0


# 12. the folder is canonicalised before the policy lookup -----------------------
def test_canonical_folder_restores_on_disk_case(tmp_path, monkeypatch):
    real = tmp_path / "State Street"
    real.mkdir()
    here = os.getcwd()
    got = d.canonical_folder(os.path.join(str(tmp_path), "state street"))
    assert os.getcwd() == here                      # no cwd side effect
    assert got == d.canonical_folder(str(real))
    assert os.path.basename(got) == "State Street"  # macOS is case-preserving
    # the whole point: a lowercase --dir must hit the zdr policy root
    monkeypatch.setattr(d.router, "POLICY", {got: {"data": "zdr"}})
    assert d.router.policy_for(got)["data"] == "zdr"
    # since #40 the router folds case itself, so the raw spelling hits it too
    assert d.router.policy_for(
        os.path.join(str(tmp_path), "state street"))["data"] == "zdr"


# 13. the router advertises run-tag support on /v1/models -------------------------
def test_models_endpoint_advertises_run_tag(monkeypatch):
    h = r.Router.__new__(r.Router)
    headers = {}
    h.send_response = lambda *a: None
    h.send_header = lambda k, v: headers.__setitem__(k.lower(), v)
    h.end_headers = lambda: None
    h.wfile = io.BytesIO()
    h.headers_passthrough = lambda: {}
    h.log_message = lambda *a: None
    monkeypatch.setattr(r, "CANDIDATES", [])
    monkeypatch.setattr(r, "or_key", lambda model=None: "")
    def no_net(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(r.http.client, "HTTPSConnection", no_net)
    h.models()
    assert headers.get(d.RUN_TAG)


# 14. dispatch.run(): the walk ---------------------------------------------------
QUOTA_LOW = ("Gemini Models\tWeekly Limit Remaining\t1%\t2026-10-15T08:36:51Z\n"
             "Claude Models\tWeekly Limit Remaining\t100%\t2026-10-15T08:36:51Z\n")

TEST_DISPATCH_CFG = {
    "agy": "/bin/echo",
    "headroom": 95,
    "roles": {"coder": {"plan": "kimi/k3-256k", "kimi": "kimi/k2.8",
                        "antigravity": "gemini-3.8-flash-high",
                        "agy_timeout": "45m", "cash": "xiaomi/mimo-v2.6-pro"}},
}


def _proc(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class RunHarness:
    """dispatch.run() with every subprocess, meter and ledger stubbed. `calls`
    records each argv so tests can assert which tiers actually ran."""

    def __init__(self, monkeypatch, tmp_path):
        self.calls = []
        self.ledger = []
        prompt = tmp_path / "prompt.md"
        prompt.write_text("do the thing")
        logfile = tmp_path / "requests.log"
        logfile.write_text("")
        monkeypatch.setattr(d.router, "LOGFILE", str(logfile))
        monkeypatch.setattr(d.router, "CATALOG", {"dispatch": TEST_DISPATCH_CFG})
        monkeypatch.setattr(d.router, "POLICY", {})
        monkeypatch.setattr(d.router, "log_request", self.ledger.append)
        monkeypatch.setattr(d, "meters", lambda logdir: {"claude": 5, "kimi": 5})
        monkeypatch.setattr(d, "router_tagged", lambda port: True)
        monkeypatch.setattr(d.shutil, "which", lambda name: "/fake/" + name)
        monkeypatch.setattr(d.subprocess, "run", self._run)
        monkeypatch.setattr(d, "ledger_since", self._ledger_since)
        self.plan_stdout = json.dumps({"is_error": False, "result": "plan answer"})
        self.cash_stdout = json.dumps({"is_error": False, "result": "cash answer"})
        self.agy_stdout = json.dumps({"status": "SUCCESS", "response": "agy answer"})
        self.agy_stderr = ""
        self.quota_queue = [QUOTA_OUT]
        self.plan_recs = [{"status": 200, "upstream": "kimi",
                           "model_requested": "kimi/k3-256k"}]
        self.cash_recs = [{"status": 200, "upstream": "openrouter",
                           "model_requested": "xiaomi/mimo-v2.6-pro", "cost": 0.001}]
        self.args = types.SimpleNamespace(
            role="coder", headroom=95, prompt_file=str(prompt), dir=str(tmp_path),
            from_=None, no_cash=False, allowed_tools=["Read"])

    def _ledger_since(self, path, offset, run):
        return list(self.plan_recs if run.startswith("plan:") else self.cash_recs)

    def _run(self, argv, **kw):
        self.calls.append(list(argv))
        exe = os.path.basename(str(argv[0]))
        if exe == "nc":
            return _proc()
        if exe == "agy" or exe == "echo":
            if "/quota" in argv:
                out = (self.quota_queue.pop(0) if len(self.quota_queue) > 1
                       else self.quota_queue[0])
                return _proc(stdout=out)
            return _proc(stdout=self.agy_stdout, stderr=self.agy_stderr)
        model = argv[argv.index("--model") + 1]
        return _proc(stdout=(self.cash_stdout if model == "xiaomi/mimo-v2.6-pro"
                             else self.plan_stdout))

    def models_called(self):
        out = []
        for argv in self.calls:
            if "--model" in argv:
                out.append(argv[argv.index("--model") + 1])
        return out


def run_exhausted_plan(h):
    h.plan_stdout = json.dumps({"is_error": True, "api_error_status": 402})
    h.plan_recs = [{"status": 402, "model_requested": "kimi/k3-256k"}]


def test_run_plan_ok_stops_at_plan(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    assert d.run(h.args) == 0
    assert capsys.readouterr().out == "plan answer\n"
    assert h.models_called() == ["kimi/k3-256k"]


def test_run_plan_exhausted_walks_to_antigravity(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    assert d.run(h.args) == 0
    assert capsys.readouterr().out == "agy answer\n"
    assert h.models_called() == ["kimi/k3-256k", "gemini-3.8-flash-high"]


def test_run_all_plan_exhausted_walks_to_cash(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    h.quota_queue = [QUOTA_LOW]          # agy pre-check: 99% used
    assert d.run(h.args) == 0
    assert capsys.readouterr().out == "cash answer\n"
    assert h.models_called() == ["kimi/k3-256k", "xiaomi/mimo-v2.6-pro"]


def test_run_no_cash_stops_with_2(monkeypatch, tmp_path):
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    h.quota_queue = [QUOTA_LOW]
    h.args.no_cash = True
    assert d.run(h.args) == 2
    assert "xiaomi/mimo-v2.6-pro" not in h.models_called()


def test_run_from_cash_skips_earlier_tiers(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    h.args.from_ = "cash"
    assert d.run(h.args) == 0
    assert capsys.readouterr().out == "cash answer\n"
    assert h.models_called() == ["xiaomi/mimo-v2.6-pro"]


def test_run_plan_failure_returns_3(monkeypatch, tmp_path):
    h = RunHarness(monkeypatch, tmp_path)
    h.plan_stdout = json.dumps({"is_error": True, "api_error_status": 404})
    h.plan_recs = [{"status": 404, "model_requested": "kimi/k3-256k"}]
    assert d.run(h.args) == 3
    assert h.models_called() == ["kimi/k3-256k"]


def test_run_agy_failure_returns_3_without_cash(monkeypatch, tmp_path):
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    h.agy_stdout = json.dumps({"status": "ERROR", "error": "tool failed"})
    assert d.run(h.args) == 3
    assert "xiaomi/mimo-v2.6-pro" not in h.models_called()


def test_run_missing_agy_returns_3_not_cash(monkeypatch, tmp_path):
    # a missing agy binary means Antigravity was never tried -- cash may only
    # run when --from cash said so explicitly
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    monkeypatch.setattr(d.router, "CATALOG", {"dispatch": dict(
        TEST_DISPATCH_CFG, agy="/nonexistent/agy")})
    assert d.run(h.args) == 3
    assert "xiaomi/mimo-v2.6-pro" not in h.models_called()
    h2 = RunHarness(monkeypatch, tmp_path)
    h2.args.from_ = "cash"
    monkeypatch.setattr(d.router, "CATALOG", {"dispatch": dict(
        TEST_DISPATCH_CFG, agy="/nonexistent/agy")})
    assert d.run(h2.args) == 0
    assert h2.models_called() == ["xiaomi/mimo-v2.6-pro"]


def test_run_agy_quota_wording_confirmed_by_meter(monkeypatch, tmp_path, capsys):
    # quota wording in the error text AND a meter past headroom -> walk on
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    h.agy_stdout = json.dumps({"status": "ERROR", "error": "Out of credits"})
    h.quota_queue = [QUOTA_OUT, QUOTA_LOW]   # pre-check ok, re-check exhausted
    assert d.run(h.args) == 0
    assert capsys.readouterr().out == "cash answer\n"
    assert h.models_called() == ["kimi/k3-256k", "gemini-3.8-flash-high",
                                 "xiaomi/mimo-v2.6-pro"]


def test_run_agy_quota_wording_unconfirmed_returns_3(monkeypatch, tmp_path):
    # quota wording but the meter still has headroom -> a failure, not cash
    h = RunHarness(monkeypatch, tmp_path)
    run_exhausted_plan(h)
    h.agy_stdout = json.dumps({"status": "ERROR", "error": "Out of credits"})
    h.quota_queue = [QUOTA_OUT, QUOTA_OUT]
    assert d.run(h.args) == 3
    assert "xiaomi/mimo-v2.6-pro" not in h.models_called()


def test_run_stops_when_router_predates_46(monkeypatch, tmp_path, capsys):
    # a pre-#46 (or slow) router must stop the walk, not skip to Google or cash
    h = RunHarness(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "router_tagged", lambda port: False)
    h.quota_queue = [QUOTA_LOW]
    assert d.run(h.args) == 1
    assert "predates #46" in capsys.readouterr().err
    assert h.models_called() == []
    assert not [a for a in h.calls if os.path.basename(a[0]) == "agy" and "/quota" not in a]


def test_run_untagged_router_still_allows_explicit_from(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    monkeypatch.setattr(d, "router_tagged", lambda port: False)
    h.args.from_ = "antigravity"
    h.quota_queue = [QUOTA_OUT]
    assert d.run(h.args) == 0
    assert capsys.readouterr().out == "agy answer\n"


def test_run_canonicalises_dir_before_policy(monkeypatch, tmp_path):
    # lowercase --dir must still hit the zdr root: no Google, no cash, exit 2
    h = RunHarness(monkeypatch, tmp_path)
    real = tmp_path / "State Street"
    real.mkdir()
    monkeypatch.setattr(d.router, "POLICY", {str(real): {"data": "zdr"}})
    h.args.dir = str(tmp_path / "state street")
    h.args.from_ = "antigravity"
    h.quota_queue = [QUOTA_OUT]
    assert d.run(h.args) == 2
    assert h.models_called() == []
    assert not [a for a in h.calls if os.path.basename(a[0]) == "agy" and "/quota" not in a]


def test_run_missing_dir_is_a_message(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    h.args.dir = str(tmp_path / "nope")
    assert d.run(h.args) == 1
    assert "no such folder" in capsys.readouterr().err


class TruncatedQuotaResp(FakeResp):
    def read(self, *a):
        raise http.client.IncompleteRead(QUOTA_403[:20], len(QUOTA_403) - 20)


def test_truncated_quota_403_body_is_502_not_failover(monkeypatch):
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(TruncatedQuotaResp(403, QUOTA_403),))
    out = h.relay("POST", [dict(PREPARED[1])], run="")
    assert out[:2] == ("fail", 502)
    assert "failed_over_to" not in recs[0]


class TruncatedAccessResp(FakeResp):
    def read(self, *a):
        raise http.client.IncompleteRead(ACCESS_403[:20], len(ACCESS_403) - 20)


def test_truncated_access_403_must_not_drain_plan(monkeypatch):
    recs = []
    h = relay_stub(monkeypatch, recs, statuses=(TruncatedAccessResp(403, ACCESS_403), 200))
    out = h.relay("POST", [dict(p) for p in KIMI_FIRST], run="")
    assert out[:2] == ("fail", 502)
    assert "failed_over_to" not in recs[0]
