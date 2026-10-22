"""Breaker tests for PR #57 (issue #56). Run: python3 -m pytest -q test_breaker_limits.py

Every file write goes under a pytest tmp_path; LOGFILE is monkeypatched, so the
live ~/.local/share/claude-router limit and log files are never touched.
"""
import calendar, importlib.util, json, math, os, time
import pytest

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

NOW = float(calendar.timegm((2026, 10, 12, 12, 0, 0)))
HOUR, DAY = 3600, 86400
SUB = "claude-sonnet-5"          # bare claude-* id: subscription, not metered
KIMI = "kimi/k2.8"               # billing subscription
MIMO = "xiaomi/mimo-v2.6-flash"  # no billing key: metered


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def kimi_full(**over):
    d = {"ts": iso(time.time()), "five_hour_pct": 10.0,
         "five_hour_resets_at": iso(time.time() + HOUR),
         "month_pct": 10.0, "month_resets_at": iso(time.time() + 9 * DAY),
         "month_code_pct": 10.0, "month_code_resets_at": iso(time.time() + 9 * DAY)}
    d.update(over)
    return d


def limits_path(name):
    return os.path.join(os.path.dirname(r.LOGFILE), name)


def write_json(name, obj):
    with open(limits_path(name), "w") as f:
        f.write(json.dumps(obj) + "\n")


def write_raw(name, text):
    with open(limits_path(name), "w") as f:
        f.write(text)


def add_fallback_row(**over):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "upstream": "openrouter",
           "model_requested": MIMO, "failover_from": KIMI, "status": 200}
    rec.update(over)
    r.log_request(rec)


def add_raw_ledger_line(line):
    with open(r.LOGFILE, "a") as f:
        f.write(line + "\n")


class FakeResp:
    def __init__(self, status, headers=None):
        self.status = status
        self._h = {k.lower(): v for k, v in (headers or {}).items()}

    def getheader(self, name, default=None):
        return self._h.get(name.lower(), default)

    def getheaders(self):
        return list(self._h.items())

    def read(self, *a):
        return b""

    def close(self):
        pass


class FakeConn:
    def close(self):
        pass


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "LOGFILE", str(tmp_path / "requests.log"))
    monkeypatch.setattr(r, "maybe_fetch_quota", lambda u: None)
    monkeypatch.setattr(r, "BY_ID", {
        KIMI: {"id": KIMI, "billing": "subscription"},
        MIMO: {"id": MIMO, "price": [0.14, 0.28]},
    })
    monkeypatch.setitem(r.CATALOG, "limits", {})
    monkeypatch.setitem(r.CATALOG, "turn_caps", {})
    r.SKIP_UNTIL.clear()
    r.LIVE.clear()
    yield tmp_path


# ---------- limit files: corrupt, future ts, resets ------------------------

def test_corrupt_or_partial_limit_json_is_ignored():
    write_raw("kimi-limits.json",
              '{"ts": "%s", "five_hour_pct": 100, "five_hour_resets_at":' % iso(time.time()))
    assert r.skip_reason(KIMI, "kimi") is None


def test_break_ts_a_day_in_future_is_trusted_forever():
    write_json("kimi-limits.json", kimi_full(ts=iso(time.time() + DAY),
                                             five_hour_pct=100.0,
                                             five_hour_resets_at=iso(time.time() + HOUR)))
    assert r.skip_reason(KIMI, "kimi") is None


def test_break_non_ascii_digit_ts_raises():
    write_json("kimi-limits.json", kimi_full(ts="²"))
    assert r.skip_reason(KIMI, "kimi") is None


def test_break_non_ascii_digit_resets_at_raises():
    write_json("kimi-limits.json", kimi_full(five_hour_pct=100.0,
                                             five_hour_resets_at="²"))
    assert r.skip_reason(KIMI, "kimi") is None


def test_resets_at_in_past_with_pct_100_does_not_skip():
    write_json("kimi-limits.json", kimi_full(five_hour_pct=100.0,
                                             five_hour_resets_at=iso(time.time() - HOUR)))
    assert r.skip_reason(KIMI, "kimi") is None


def test_epoch_string_and_iso_resets_both_skip_when_future():
    write_json("rate-limits.json", {"ts": iso(time.time()), "five_hour_pct": 100,
                                    "five_hour_resets_at": str(int(time.time() + HOUR))})
    assert r.skip_reason(SUB, "anthropic")
    write_json("kimi-limits.json", kimi_full(five_hour_pct=100.0,
                                             five_hour_resets_at=iso(time.time() + HOUR)))
    assert r.skip_reason(KIMI, "kimi")




def test_kimi_month_total_100_with_code_below_100_does_not_skip():
    write_json("kimi-limits.json", kimi_full(month_pct=100.0, month_code_pct=55.2))
    assert r.skip_reason(KIMI, "kimi") is None


def test_stale_file_is_ignored_past_15_minutes():
    write_json("kimi-limits.json", kimi_full(ts=iso(time.time() - 16 * 60),
                                             five_hour_pct=100.0,
                                             five_hour_resets_at=iso(time.time() + HOUR)))
    assert r.skip_reason(KIMI, "kimi") is None


# ---------- 429 memory -----------------------------------------------------

def test_429_without_reset_header_cools_off_15_minutes():
    r.mark_429(SUB, FakeResp(429), now=NOW)
    assert r.SKIP_UNTIL[SUB] == NOW + 900


def test_429_with_numeric_retry_after_uses_it():
    r.mark_429(SUB, FakeResp(429, {"Retry-After": "3600"}), now=NOW)
    assert r.SKIP_UNTIL[SUB] == NOW + 3600


def test_metered_429_is_never_remembered():
    r.mark_429(MIMO, FakeResp(429), now=NOW)
    assert MIMO not in r.SKIP_UNTIL




def test_break_superscript_retry_after_crashes_marking():
    r.mark_429(SUB, FakeResp(429, {"Retry-After": "²"}), now=NOW)
    assert r.SKIP_UNTIL[SUB] == NOW + 900


# ---------- cash cap --------------------------------------------------------

def test_break_spend_cache_hides_spend_made_inside_60_seconds():
    add_fallback_row(cost=4.99)
    assert r.fallback_spent_today() == pytest.approx(4.99)
    add_fallback_row(cost=0.50)     # a second fallback goes out and is logged
    assert r.fallback_spent_today() == pytest.approx(5.49)


def test_break_nan_cost_row_disables_the_cap():
    add_fallback_row(cost=4.0)
    add_raw_ledger_line('{"ts": "%s", "failover_from": "%s", "model_requested": "%s", '
                        '"status": 200, "cost": NaN}' % (time.strftime("%Y-%m-%dT%H:%M:%S"), KIMI, MIMO))
    total = r.fallback_spent_today()
    assert math.isfinite(total) and total >= 4.0


def test_break_negative_cost_row_cancels_spend():
    add_fallback_row(cost=4.99)
    add_fallback_row(cost=-100)
    assert r.fallback_spent_today() >= 4.99




def test_rows_from_other_days_and_non_fallback_rows_are_not_counted():
    add_raw_ledger_line('{"ts": "2001-01-01T00:00:00", "failover_from": "x", "cost": 9}')
    add_raw_ledger_line('{"ts": "%s", "cost": 9}' % time.strftime("%Y-%m-%dT%H:%M:%S"))
    assert r.fallback_spent_today() == 0.0


# ---------- relay walk ------------------------------------------------------

def make_relay(outcomes, calls):
    h = r.Router.__new__(r.Router)
    fails = []

    def issue(method, att, rec):
        calls.append(att["model"])
        o = outcomes[att["model"]]
        if o == "raise":
            raise OSError("dropped")
        return FakeConn(), FakeResp(o)

    def fail(code, msg):
        fails.append(code)
        return ("FAIL", code)

    h.issue, h.fail = issue, fail
    return h, fails


def route(att_models):
    up = {SUB: "anthropic", KIMI: "kimi", MIMO: "openrouter"}
    return [{"model": m, "upstream": up[m], "host": up[m]} for m in att_models]


def test_last_entry_still_tried_when_every_earlier_entry_is_exhausted():
    r.SKIP_UNTIL[SUB] = time.time() + HOUR
    write_json("kimi-limits.json", kimi_full(five_hour_pct=100.0,
                                             five_hour_resets_at=iso(time.time() + HOUR)))
    calls = []
    h, fails = make_relay({SUB: 429, KIMI: 429, MIMO: "raise"}, calls)
    h.relay("POST", route([SUB, KIMI, MIMO]))
    assert calls == [MIMO]


def test_last_subscription_entry_is_tried_even_when_exhausted():
    r.SKIP_UNTIL[SUB] = time.time() + HOUR
    write_json("kimi-limits.json", kimi_full(five_hour_pct=100.0,
                                             five_hour_resets_at=iso(time.time() + HOUR)))
    calls = []
    h, fails = make_relay({SUB: 429, KIMI: "raise"}, calls)
    h.relay("POST", route([SUB, KIMI]))
    assert calls == [KIMI]


def test_cash_cap_exactly_5_refuses_metered_fallback():
    add_fallback_row(cost=5.0)
    calls = []
    h, fails = make_relay({KIMI: 429, MIMO: "raise"}, calls)
    h.relay("POST", route([KIMI, MIMO]))
    assert calls == [KIMI] and fails == [403]


def test_cash_cap_over_5_refuses_metered_fallback():
    add_fallback_row(cost=5.25)
    calls = []
    h, fails = make_relay({KIMI: 429, MIMO: "raise"}, calls)
    h.relay("POST", route([KIMI, MIMO]))
    assert calls == [KIMI] and fails == [403]


def test_cash_cap_under_5_lets_metered_fallback_through():
    add_fallback_row(cost=4.99)
    calls = []
    h, fails = make_relay({KIMI: 429, MIMO: "raise"}, calls)
    h.relay("POST", route([KIMI, MIMO]))
    assert calls == [KIMI, MIMO]
