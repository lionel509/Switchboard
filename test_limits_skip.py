"""Limit-aware failover (#56). Run: python3 -m pytest -q test_limits_skip.py

Every test runs against a temp LOGFILE; the live limit files and ledger are
never touched. The property test checks the invariants from the #56 plan.
"""
import importlib.util, io, json, os, random, time

import pytest

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

KIMI = "kimi/k2.8"
MIMO = "xiaomi/mimo-v2.6-flash"
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"
CHAIN = [(HAIKU, "anthropic"), (KIMI, "kimi"), (MIMO, "openrouter")]
FAILOVER = (402, 429, 529)


def reset_state():
    for name in ("SKIP_UNTIL", "LIVE"):
        if hasattr(r, name):
            getattr(r, name).clear()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "LOGFILE", str(tmp_path / "requests.log"))
    monkeypatch.setattr(r, "maybe_fetch_quota", lambda upstream: None)
    monkeypatch.setitem(r.CATALOG, "turn_caps", {})
    monkeypatch.setitem(r.CATALOG, "limits", {})
    monkeypatch.setattr(r, "BY_ID", {
        KIMI: {"id": KIMI, "billing": "subscription"},
        MIMO: {"id": MIMO, "price": [0.14, 0.28]}})
    reset_state()
    yield
    reset_state()


# --- harness ---------------------------------------------------------------

class FakeResp:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def getheaders(self):
        return list(self.headers.items())

    def read(self, *a):
        return b""

    def read1(self, n=-1):
        return b""


class FakeConn:
    def close(self):
        pass


def make_handler(outcomes, calls, fails, headers=None):
    h = r.Router.__new__(r.Router)
    h.path = "/v1/messages"
    h.wfile = io.BytesIO()
    h.send_response = lambda *a, **k: None
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.log_message = lambda *a, **k: None
    h.fail = lambda code, msg: fails.append((code, msg))

    def issue(method, att, rec):
        calls.append(att["model"])
        o = outcomes.get(att["model"], 200)
        if o == "raise":
            raise OSError("connection reset")
        return FakeConn(), FakeResp(o, (headers or {}).get(att["model"]))

    h.issue = issue
    return h


def prep(chain):
    return [{"model": m, "upstream": u, "host": "h-" + u} for m, u in chain]


def run(outcomes, chain=CHAIN, headers=None):
    calls, fails = [], []
    h = make_handler(outcomes, calls, fails, headers)
    h.relay("POST", prep(chain))
    return calls, fails


def read_rows():
    with open(r.LOGFILE) as f:
        return [json.loads(line) for line in f if line.strip()]


def now_iso(age=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age))


def today_local():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def reset_value(kind, when):
    """A reset time: 'future' or 'past' one hour out, or '' (no reset)."""
    if when == "":
        return ""
    t = time.time() + (3600 if when == "future" else -7200)
    return str(int(t)) if kind == "epoch" else time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def write_limits(upstream, body):
    name = "rate-limits.json" if upstream == "anthropic" else "%s-limits.json" % upstream
    with open(os.path.join(os.path.dirname(r.LOGFILE), name), "w") as f:
        f.write(body if isinstance(body, str) else json.dumps(body))


def claude_snapshot(seven=0, five=0, reset="future", age=0):
    """The status-line shape: epoch resets as strings, UTC ts."""
    return {"ts": now_iso(age), "five_hour_pct": five,
            "five_hour_resets_at": reset_value("epoch", "future"),
            "seven_day_pct": seven,
            "seven_day_resets_at": reset_value("epoch", reset), "spend_pct": 0}


def kimi_snapshot(five=0, month=0, code=0, reset="future", age=0):
    """The kimi-limits.json shape from flatten_quota: ISO resets."""
    return {"ts": now_iso(age),
            "five_hour_pct": five, "five_hour_resets_at": reset_value("iso", "future"),
            "month_pct": month, "month_resets_at": reset_value("iso", "future"),
            "month_code_pct": code, "month_code_resets_at": reset_value("iso", reset)}


def write_ledger(rows):
    with open(r.LOGFILE, "a") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


def fallback_row(cost, ts=None):
    return {"ts": ts or today_local(), "upstream": "openrouter", "status": 200,
            "cost": cost, "failover_from": KIMI}


# --- skip on a fresh exhausted subscription --------------------------------

def test_exhausted_claude_week_skips_anthropic():
    write_limits("anthropic", claude_snapshot(seven=100))
    calls, _ = run({HAIKU: 429})
    assert calls == [KIMI]
    row = read_rows()[-1]
    assert row["failover_from"] == HAIKU
    assert HAIKU in row["skipped"]


def test_stale_file_is_ignored():
    write_limits("anthropic", claude_snapshot(seven=100, age=16 * 60))
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, KIMI]


@pytest.mark.parametrize("reset", ["past", ""])
def test_reset_in_past_or_missing_does_not_skip(reset):
    write_limits("anthropic", claude_snapshot(seven=100, reset=reset))
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, KIMI]


def test_unparseable_file_does_not_skip():
    write_limits("anthropic", "{not json")
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, KIMI]


def test_real_kimi_snapshot_does_not_skip_kimi():
    # The live reading on 2026-10-12: the month total is full, but Kimi kept
    # answering 200 on k2.8, so the total is not a skip gate.
    write_limits("kimi", kimi_snapshot(five=0, month=100, code=55.2))
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, KIMI]


def test_kimi_code_pool_full_skips_kimi():
    write_limits("kimi", kimi_snapshot(code=100))
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, MIMO]
    assert KIMI in read_rows()[-1]["skipped"]


def test_last_attempt_never_skipped():
    write_limits("kimi", kimi_snapshot(code=100))
    calls, fails = run({HAIKU: 429}, chain=[(HAIKU, "anthropic"), (KIMI, "kimi")])
    assert calls == [HAIKU, KIMI]
    assert fails == []


# --- remembered 429s -------------------------------------------------------

def test_429_retry_after_marks_model():
    run({HAIKU: 429}, headers={HAIKU: {"retry-after": "120"}})
    assert abs(r.SKIP_UNTIL[HAIKU] - (time.time() + 120)) < 5
    calls, _ = run({HAIKU: 200})
    assert calls == [KIMI]


def test_mark_in_the_past_is_tried_again():
    r.SKIP_UNTIL[HAIKU] = time.time() - 1
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, KIMI]


def test_429_without_header_uses_cooloff():
    run({HAIKU: 429})
    assert abs(r.SKIP_UNTIL[HAIKU] - (time.time() + 900)) < 5


@pytest.mark.parametrize("status", [402, 529])
def test_402_and_529_do_not_mark(status):
    run({HAIKU: status})
    assert HAIKU not in r.SKIP_UNTIL


def test_metered_429_not_marked():
    r.mark_429(MIMO, FakeResp(429, {"retry-after": "60"}))
    assert MIMO not in r.SKIP_UNTIL


# --- daily cash cap --------------------------------------------------------

def test_cash_cap_stops_before_metered_fallback():
    write_ledger([fallback_row(5.0)])
    calls, fails = run({HAIKU: 429, KIMI: 429})
    assert calls == [HAIKU, KIMI]
    assert fails[-1][0] == 403
    assert "fallback cash cap" in read_rows()[-1]["error"]


def test_spent_counts_only_today_fallback_numeric():
    yesterday = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 86400))
    write_ledger([
        fallback_row(3.0, ts=yesterday),
        {"ts": today_local(), "upstream": "openrouter", "status": 200, "cost": 2.0},
        {"ts": today_local(), "failover_from": KIMI, "cost": {"input_tokens_cost_nanodollars": 1500}},
        {"ts": today_local(), "failover_from": KIMI, "cost": True},
        fallback_row(0.5),
        "not json",
    ])
    assert r.fallback_spent_today() == 0.5


def test_cap_ignores_direct_metered_pick():
    write_ledger([fallback_row(9.0)])
    calls, fails = run({MIMO: 200}, chain=[(MIMO, "openrouter")])
    assert calls == [MIMO]
    assert fails == []


def test_cap_null_disables():
    r.CATALOG["limits"]["fallback_cash_per_day"] = None
    write_ledger([fallback_row(9.0)])
    calls, _ = run({HAIKU: 429}, chain=[(HAIKU, "anthropic"), (MIMO, "openrouter")])
    assert calls == [HAIKU, MIMO]


# --- worker shift (off by default) -----------------------------------------

def test_worker_shift_off_by_default():
    write_limits("anthropic", claude_snapshot(seven=90))
    calls, _ = run({HAIKU: 429})
    assert calls == [HAIKU, KIMI]


def test_worker_shift_on_skips_workers_not_managers():
    r.CATALOG["limits"]["worker_shift_pct"] = 85
    write_limits("anthropic", claude_snapshot(seven=90))
    calls, _ = run({HAIKU: 200})
    assert calls == [KIMI]
    assert "worker shift" in read_rows()[-1]["skipped"][HAIKU]
    calls, _ = run({OPUS: 429}, chain=[(OPUS, "anthropic"), (KIMI, "kimi")])
    assert calls == [OPUS, KIMI]


# --- transport errors keep the #1 cash rule -------------------------------

def test_transport_error_never_reaches_cash_via_skip():
    write_limits("kimi", kimi_snapshot(code=100))
    calls, fails = run({HAIKU: "raise"})
    assert calls == [HAIKU]
    assert MIMO not in calls
    assert fails[-1][0] == 502


def test_transport_error_then_plan_429_may_still_reach_cash():
    # Master behaviour, kept: once a plan hop has answered, the cash rule is
    # the ordinary per-hop one.
    calls, _ = run({HAIKU: "raise", KIMI: 429})
    assert calls == [HAIKU, KIMI, MIMO]


# --- live-upstreams.json ---------------------------------------------------

def test_live_upstreams_written_and_recovers():
    write_limits("anthropic", claude_snapshot(seven=100))
    calls, _ = run({HAIKU: 200})
    assert calls == [KIMI]
    path = os.path.join(os.path.dirname(r.LOGFILE), "live-upstreams.json")
    with open(path) as f:
        assert json.load(f)[HAIKU]["model"] == KIMI
    write_limits("anthropic", claude_snapshot(seven=100, age=16 * 60))
    calls, _ = run({HAIKU: 200})
    assert calls == [HAIKU]
    with open(path) as f:
        assert json.load(f)[HAIKU]["model"] == HAIKU


# --- property test ---------------------------------------------------------

def master_walk(chain, outcomes):
    """relay() at master (53d8b84): the sequence this PR must match when no
    limit state exists."""
    sent = []
    for i, (m, _) in enumerate(chain):
        last = i == len(chain) - 1
        sent.append(m)
        o = outcomes[m]
        if o == "raise":
            if last or r.metered(chain[i + 1][0]):
                return sent
            continue
        if o in FAILOVER and not last:
            continue
        return sent
    return sent


def random_snapshot(rng, upstream, state):
    if state == "absent":
        return None
    age = 0 if state == "fresh" else 3600

    def pct():
        return rng.choice([0, 55.2, 85, 99.9, 100, 150])

    def reset(kind):
        return reset_value(kind, rng.choice(["future", "past", ""]))

    if upstream == "anthropic":
        return {"ts": now_iso(age), "five_hour_pct": pct(),
                "five_hour_resets_at": reset("epoch"), "seven_day_pct": pct(),
                "seven_day_resets_at": reset("epoch")}
    return {"ts": now_iso(age), "five_hour_pct": pct(),
            "five_hour_resets_at": reset("iso"), "month_pct": pct(),
            "month_resets_at": reset("iso"), "month_code_pct": pct(),
            "month_code_resets_at": reset("iso")}


def test_invariants_random(tmp_path, monkeypatch):
    rng = random.Random(56)
    for n in range(400):
        d = tmp_path / str(n)
        d.mkdir()
        monkeypatch.setattr(r, "LOGFILE", str(d / "requests.log"))
        reset_state()

        chain = []
        if rng.random() < 0.5:
            chain.append((OPUS, "anthropic"))
        chain += [c for c in CHAIN if rng.random() < 0.6]
        if not chain:
            chain = [CHAIN[rng.randrange(len(CHAIN))]]
        models = [m for m, _ in chain]

        outcomes = {m: rng.choice([200, 429, 402, 529, 403, "raise"]) for m in models}

        states = {}
        for up in ("anthropic", "kimi"):
            states[up] = rng.choice(["absent", "stale", "fresh"])
            body = random_snapshot(rng, up, states[up])
            if body is not None:
                write_limits(up, body)

        worker_shift = rng.choice([None, 85])
        cap = rng.choice([None, 0.0, 5.0])
        spent = rng.choice([0, 4.99, 5.0])
        r.CATALOG["limits"] = {"worker_shift_pct": worker_shift,
                               "fallback_cash_per_day": cap}
        if spent:
            write_ledger([fallback_row(spent)])
        pre = rng.choice([None, "past", "future"])
        if pre:
            r.SKIP_UNTIL[models[0]] = time.time() + (900 if pre == "future" else -900)

        calls, fails = run(outcomes, chain)
        ctx = (n, chain, outcomes, calls, fails)

        # I1: sends are an in-order subsequence of the chain
        it = iter(models)
        assert all(c in it for c in calls), ctx

        # I2: no limit state and a cap that isn't hit means master's walk
        neutral = (all(s == "absent" for s in states.values())
                   and worker_shift is None and pre != "future"
                   and (cap is None or spent < cap))
        if neutral:
            assert calls == master_walk(chain, outcomes), ctx

        # I3: a metered fallback is never sent once today's spend reaches the cap
        if cap is not None and spent >= cap:
            ci = {m: i for i, m in enumerate(models)}
            assert not any(ci[m] > 0 and r.metered(m) for m in calls), ctx

        # I4: a metered model never follows a hop whose transport failed
        for p, m in enumerate(calls):
            if p and r.metered(m):
                assert outcomes[calls[p - 1]] != "raise", ctx

        # I5: the last attempt is never pre-emptively skipped; every request
        # either sends something or fails
        assert calls or fails, ctx
        assert (fails or calls[-1] == models[-1]
                or outcomes[calls[-1]] not in FAILOVER), ctx


# --- round 1 review (PR #57) ----------------------------------------------

def test_pct_just_under_100_does_not_skip():
    write_limits("anthropic", claude_snapshot(seven=99.9))
    calls, _ = run({HAIKU: 200}, chain=[(HAIKU, "anthropic"), (KIMI, "kimi")])
    assert calls == [HAIKU]


def test_file_skip_refreshes_quota_but_429_skip_does_not(monkeypatch):
    fetched = []
    monkeypatch.setattr(r, "maybe_fetch_quota", fetched.append)
    r.SKIP_UNTIL[HAIKU] = time.time() + 3600
    write_limits("kimi", kimi_snapshot(code=100))
    calls, _ = run({MIMO: 200}, chain=[(HAIKU, "anthropic"), (KIMI, "kimi"), (MIMO, "openrouter")])
    assert calls == [MIMO]
    assert "kimi" in fetched
    assert "anthropic" not in fetched


def test_skipped_hop_does_not_take_a_turn(monkeypatch):
    taken = []
    monkeypatch.setattr(r, "take_turn", lambda m: taken.append(m) or True)
    r.SKIP_UNTIL[HAIKU] = time.time() + 3600
    calls, _ = run({KIMI: 200}, chain=[(HAIKU, "anthropic"), (KIMI, "kimi")])
    assert calls == [KIMI]
    assert taken == [KIMI]


@pytest.mark.parametrize("v", [float("inf"), float("nan"), "1792080000000"])
def test_to_epoch_rejects_non_finite_and_ms(v):
    assert r.to_epoch(v) is None


def test_infinite_reset_in_file_does_not_crash():
    write_limits("kimi", '{"ts": "%s", "five_hour_pct": 100, '
                         '"five_hour_resets_at": Infinity}' % now_iso())
    assert r.skip_reason(KIMI, "kimi") is None
