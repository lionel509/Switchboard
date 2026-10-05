"""A Claude release the catalog hasn't heard of is still a plan model. Run: python3 -m pytest test_new_claude_ids.py"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def setup(monkeypatch):
    monkeypatch.setattr(r, "BY_ID", {})
    monkeypatch.setitem(r.CATALOG, "turn_caps", {"per_hour": [[0.5, 600], [2.0, 120], [None, 40]]})


def test_an_unlisted_claude_id_is_a_plan_model(monkeypatch):
    # claude-sonnet-5-5 and claude-fable-5-1 were capped at 40/h for a week
    # because nobody added them to models.json (2026-10-04).
    setup(monkeypatch)
    for m in ("claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-6"):
        assert not r.metered(m)
        assert r.turn_cap(m) is None


def test_unlisted_openrouter_ids_stay_metered(monkeypatch):
    setup(monkeypatch)
    for m in ("anthropic/claude-sonnet-5-5", "x/unknown", "kaggle/claude-sonnet-5"):
        assert r.metered(m)
        assert r.turn_cap(m) == 40
