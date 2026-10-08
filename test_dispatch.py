"""Headless dispatch tiers (#46). Run: python3 -m pytest test_dispatch.py"""
import importlib.util, io, json, os, subprocess, sys, time

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
    def __init__(self, status):
        self.status = status

    def getheader(self, name):
        return "2" if name == "content-length" else None

    def getheaders(self):
        return [("content-length", "2")]

    def read(self, *a):
        return b"{}"


class FakeConn:
    def close(self):
        pass


def relay_stub(monkeypatch, recs):
    h = r.Router.__new__(r.Router)
    monkeypatch.setattr(r, "log_request", recs.append)
    monkeypatch.setattr(r, "take_turn", lambda model: True)
    monkeypatch.setattr(r, "maybe_fetch_quota", lambda upstream: None)
    h.issue = lambda method, att, rec: (FakeConn(), FakeResp(429))
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
    by_id(monkeypatch)
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
    l1 = json.dumps({"run": "plan:other", "status": 200}) + "\n"
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
