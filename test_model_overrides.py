"""Per-model auto and reasoning overrides (#19). Run: python3 -m pytest test_model_overrides.py"""
import importlib.util, json, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def thinking(body):
    return json.loads(r.sanitize(json.dumps(body).encode()))["thinking"]


def test_default_budget_without_an_override(monkeypatch):
    monkeypatch.setattr(r, "BY_ID", {"a/b": {"id": "a/b"}})
    assert thinking({"model": "a/b", "max_tokens": 32000}) == {"type": "enabled", "budget_tokens": r.THINKING_BUDGET}


def test_model_thinking_budget_replaces_the_default_and_zero_is_off(monkeypatch):
    monkeypatch.setattr(r, "BY_ID", {"a/b": {"id": "a/b", "thinking_budget": 4000},
                                     "a/off": {"id": "a/off", "thinking_budget": 0}})
    assert thinking({"model": "a/b", "max_tokens": 32000}) == {"type": "enabled", "budget_tokens": 4000}
    assert thinking({"model": "a/off", "max_tokens": 32000}) == {"type": "disabled"}
    assert thinking({"model": "a/off", "max_tokens": 32000, "output_config": {"effort": "high"}}) == {"type": "disabled"}


def test_auto_false_keeps_a_model_out_of_auto(monkeypatch):
    ids = [m["id"] for m in r.CANDIDATES]
    target = next(i for i in ids if "/" in i and not i.startswith("kaggle/"))
    monkeypatch.setitem(r.BY_ID[target], "auto", False)
    seen = {}
    monkeypatch.setattr(r, "ask_router_model", lambda t, c: seen.setdefault("c", [m["id"] for m in c]) and None)
    r.resolve_auto({"messages": [{"role": "user", "content": "x" * 10}]}, {})
    assert seen["c"] and target not in seen["c"]
