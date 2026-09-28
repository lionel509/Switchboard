"""Spend panel: today's ledger totals (#16). Run: python3 -m pytest test_spend.py"""
import importlib.util, json, os

spec = importlib.util.spec_from_file_location(
    "_tui", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tui.py"))
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)

LINES = [json.dumps(r) for r in [
    {"ts": "2026-09-27T23:59:59", "upstream": "openrouter", "status": 200, "cost": 5.0},
    {"ts": "2026-09-28T01:00:00", "upstream": "openrouter", "status": 200, "cost": 0.25},
    {"ts": "2026-09-28T02:00:00", "upstream": "openrouter", "key": "openrouter-gemma", "status": 200, "cost": 0.01},
    {"ts": "2026-09-28T03:00:00", "upstream": "gemma-or", "status": 200, "cost": 0.02},
    {"ts": "2026-09-28T03:30:00", "upstream": "gemma-or", "status": 429},
    {"ts": "2026-09-28T04:00:00", "upstream": "anthropic", "status": 200},
    {"ts": "2026-09-28T05:00:00", "upstream": "openrouter", "status": 200, "cost": 0.5, "failover_from": "claude-sonnet-5"},
]] + ["not json", ""]


def test_spend_today_groups_by_upstream_or_key_and_skips_other_days():
    s = tui.spend_today(LINES, "2026-09-28")
    assert s["openrouter"] == {"requests": 2, "cost": 0.75, "failover_cost": 0.5, "refused": 0}
    assert s["openrouter-gemma"] == {"requests": 1, "cost": 0.01, "failover_cost": 0.0, "refused": 0}
    assert s["gemma-or"] == {"requests": 2, "cost": 0.02, "failover_cost": 0.0, "refused": 1}
    assert s["anthropic"] == {"requests": 1, "cost": 0.0, "failover_cost": 0.0, "refused": 0}
    assert set(s) == {"openrouter", "openrouter-gemma", "gemma-or", "anthropic"}
