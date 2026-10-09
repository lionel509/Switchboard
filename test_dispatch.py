"""Headless dispatch tiers (#46). Run: python3 -m pytest test_dispatch.py"""
import importlib.util, io, json, os, subprocess, sys, time, types

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


def relay_stub(monkeypatch, recs, statuses=(429, 429)):
    h = r.Router.__new__(r.Router)
    monkeypatch.setattr(r, "log_request", recs.append)
    monkeypatch.setattr(r, "take_turn", lambda model: True)
    monkeypatch.setattr(r, "maybe_fetch_quota", lambda upstream: None)
    it = iter(statuses)
    h.issue = lambda method, att, rec: (FakeConn(), FakeResp(next(it)))
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
    assert not [a for a in h.calls if a[0] == TEST_DISPATCH_CFG["agy"] and "/quota" not in a]


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
    assert not [a for a in h.calls if a[0] == TEST_DISPATCH_CFG["agy"] and "/quota" not in a]


def test_run_missing_dir_is_a_message(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    h.args.dir = str(tmp_path / "nope")
    assert d.run(h.args) == 1
    assert "no such folder" in capsys.readouterr().err


# 16. dispatch reads data through router.data_level (#49) ----------------------
def test_tier_allowed_reads_data_through_router(monkeypatch):
    monkeypatch.setattr(d.router, "data_level", lambda v: "claude")
    assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": "any"}) is False
    monkeypatch.setattr(d.router, "data_level", lambda v: "any")
    assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": "zdr"}) is True


# 17. falsy and non-string data fail closed in dispatch (#49) -------------------
def test_tier_allowed_falsy_data_fails_closed(monkeypatch):
    monkeypatch.setattr(d.router, "BY_ID", {
        "kimi/k2.8": {"id": "kimi/k2.8", "billing": "subscription"}})
    for v in (False, 0, [], {}):
        assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": v}) is False, v
        assert d.tier_allowed("cash", "kimi/k2.8", {"data": v}) is False, v
    for v in (None, ""):
        assert d.tier_allowed("antigravity", "gemini-3.8-flash-high", {"data": v}) is True, v


# 18. the skip message names the level it read (#49) ---------------------------
def test_run_skip_message_names_the_level(monkeypatch, tmp_path, capsys):
    h = RunHarness(monkeypatch, tmp_path)
    real = tmp_path / "State Street"
    real.mkdir()
    monkeypatch.setattr(d.router, "POLICY", {str(real): {"data": False}})
    h.args.dir = str(real)
    h.args.from_ = "antigravity"
    h.quota_queue = [QUOTA_OUT]
    assert d.run(h.args) == 2
    err = capsys.readouterr().err
    assert "antigravity: skipped (data policy claude)" in err
    assert "cash: skipped (data policy claude)" in err


# 19. a --dir through a symlink keeps the policy of the folder it names (#49) ---
def test_run_dir_through_symlink_keeps_folder_policy(monkeypatch, tmp_path):
    h = RunHarness(monkeypatch, tmp_path)
    real = tmp_path / "Private"
    real.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(out, real / "link")
    monkeypatch.setattr(d.router, "POLICY", {str(real): {"data": "zdr"}})
    h.args.dir = str(real / "link")
    h.args.from_ = "antigravity"
    h.quota_queue = [QUOTA_OUT]
    assert d.run(h.args) == 2
    assert h.models_called() == []
    assert not [a for a in h.calls if a[0] == TEST_DISPATCH_CFG["agy"] and "/quota" not in a]


# 20. a --dir with ".." after a symlink keeps the folder policy it reaches (#49) ---
def test_run_dir_dotdot_after_symlink_keeps_folder_policy(monkeypatch, tmp_path):
    h = RunHarness(monkeypatch, tmp_path)
    real = tmp_path / "Private"
    (real / "sub").mkdir(parents=True)
    (real / "x").mkdir()
    other = tmp_path / "Other"
    other.mkdir()
    (other / "x").mkdir()
    os.symlink(real / "sub", other / "link")
    monkeypatch.setattr(d.router, "POLICY", {str(real): {"data": "zdr"}})
    h.args.dir = str(other / "link" / ".." / "x")
    h.args.from_ = "antigravity"
    h.quota_queue = [QUOTA_OUT]
    assert d.run(h.args) == 2
    assert h.models_called() == []

def test_run_dir_link_dotdot_never_reaches_private(monkeypatch, tmp_path):
    h = RunHarness(monkeypatch, tmp_path)
    (tmp_path / "Private" / "x").mkdir(parents=True)
    (tmp_path / "Elsewhere" / "deep").mkdir(parents=True)
    os.symlink(tmp_path / "Elsewhere" / "deep", tmp_path / "Link")
    monkeypatch.setattr(d.router, "POLICY", {str(tmp_path / "Private"): {"data": "claude"}})
    h.args.dir = str(tmp_path / "Link" / ".." / "Private" / "x")
    h.args.from_ = "antigravity"
    h.quota_queue = [QUOTA_OUT]
    assert d.run(h.args) == 1
    assert h.calls == []


def test_run_reads_policy_for_the_folder_it_runs_in(monkeypatch, tmp_path):
    spells = ["Other/link/../x", "Link/../Private/x", "Other/link",
              "Private/out/../x", "Private/out"]
    for i, spell in enumerate(spells):
        base = tmp_path / str(i)
        for sub in ("Private/sub", "Private/x", "Other/x", "Elsewhere/deep",
                    "Elsewhere/Private/x", "outdir", "x"):
            (base / sub).mkdir(parents=True)
        os.symlink(base / "Private" / "sub", base / "Other" / "link")
        os.symlink(base / "Elsewhere" / "deep", base / "Link")
        os.symlink(base / "outdir", base / "Private" / "out")
        h = RunHarness(monkeypatch, base)
        looked = []
        real_policy_for = d.router.policy_for
        monkeypatch.setattr(d.router, "policy_for",
                            lambda c, f=real_policy_for: (looked.append(c), f(c))[1])
        h.args.dir = str(base / spell)
        h.args.from_ = "antigravity"
        assert d.run(h.args) == 0, spell
        agy = [a for a in h.calls if a[0] == TEST_DISPATCH_CFG["agy"] and "/quota" not in a]
        assert os.path.realpath(looked[0]) == agy[0][agy[0].index("--add-dir") + 1], spell
        monkeypatch.setattr(d.router, "policy_for", real_policy_for)


