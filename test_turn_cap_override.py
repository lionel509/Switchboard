"""Per-model hourly cap override (#19). Run: python3 -m pytest test_turn_cap_override.py"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def setup(monkeypatch, **extra):
    m = dict({"id": "x/cheap", "price": [0.1, 0.2]}, **extra)
    monkeypatch.setattr(r, "BY_ID", {"x/cheap": m, "x/plan": {"id": "x/plan", "billing": "subscription", "turn_cap": 5}})
    monkeypatch.setitem(r.CATALOG, "turn_caps", {"per_hour": [[0.5, 600], [2.0, 120], [None, 40]]})


def test_tier_cap_applies_without_an_override(monkeypatch):
    setup(monkeypatch)
    assert r.turn_cap("x/cheap") == 600


def test_a_model_turn_cap_overrides_the_tier(monkeypatch):
    setup(monkeypatch, turn_cap=50)
    assert r.turn_cap("x/cheap") == 50


def test_turn_cap_zero_means_no_cap(monkeypatch):
    setup(monkeypatch, turn_cap=0)
    assert r.turn_cap("x/cheap") is None


def test_plan_models_stay_uncapped_even_with_a_turn_cap(monkeypatch):
    setup(monkeypatch)
    assert r.turn_cap("x/plan") is None


def test_unlisted_bare_claude_ids_are_plan_not_metered(monkeypatch):
    # #41: claude-sonnet-5-5 / claude-fable-5-1 aren't in models.json but go to the plan
    setup(monkeypatch)
    assert r.turn_cap("claude-sonnet-5-5") is None
    assert r.turn_cap("claude-fable-5-1") is None
    assert r.turn_cap("x/unknown") == 40  # unknown metered ids still capped
