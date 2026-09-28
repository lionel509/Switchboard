"""Editor: add a key (#7), choose a model's key (#9). Run: python3 -m pytest test_tui_upstreams.py"""
import importlib.util, os, pytest

spec = importlib.util.spec_from_file_location(
    "_tui", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tui.py"))
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)

CAT = {"upstreams": [
    {"name": "anthropic", "host": "api.anthropic.com", "fixed": True},
    {"name": "openrouter", "host": "openrouter.ai", "key_file": "~/.config/openrouter-key"},
    {"name": "openrouter-nonzdr", "host": "openrouter.ai", "key_file": "~/.config/openrouter-key-nonzdr"},
]}


def test_new_upstream_defaults_and_validation():
    u = tui.new_upstream(CAT, "gemma")
    assert u["name"] == "openrouter-gemma"
    assert u["key_file"] == "~/.config/openrouter-key-gemma"
    assert u["host"] == "openrouter.ai" and u["verify_url"].endswith("/api/v1/key")
    assert tui.new_upstream(CAT, "openrouter-work", key_file="~/k")["key_file"] == "~/k"
    for bad in ("", "Has Space", "../x", "nonzdr"):          # nonzdr: name already taken
        with pytest.raises(ValueError):
            tui.new_upstream(CAT, bad)


def test_key_choices_are_openrouter_key_rows_only():
    cat = dict(CAT, upstreams=CAT["upstreams"] + [tui.new_upstream(CAT, "gemma")])
    assert tui.key_choices(cat) == ["openrouter", "openrouter-nonzdr", "openrouter-gemma"]


def test_default_key_follows_zdr_and_explicit_key_wins():
    assert tui.key_of({"id": "a/b"}) == "openrouter"
    assert tui.key_of({"id": "a/b", "zdr": False}) == "openrouter-nonzdr"
    assert tui.key_of({"id": "a/b", "upstream": "openrouter-gemma"}) == "openrouter-gemma"


def test_cycle_key_walks_choices_and_default_clears_the_field():
    cat = dict(CAT, upstreams=CAT["upstreams"] + [tui.new_upstream(CAT, "gemma")])
    m = {"id": "google/gemma-4-31b-it"}
    tui.cycle_key(cat, m); assert m["upstream"] == "openrouter-nonzdr"
    tui.cycle_key(cat, m); assert m["upstream"] == "openrouter-gemma"
    tui.cycle_key(cat, m); assert "upstream" not in m       # back to the default: no field


def test_model_row_shows_a_non_default_key_only():
    cat = dict(CAT, providers={})
    plain = tui.row_text("model", {"id": "a/b", "name": "B", "price": [1, 2]}, cat, ())
    keyed = tui.row_text("model", {"id": "a/b", "name": "B", "price": [1, 2],
                                   "upstream": "openrouter-gemma"}, cat, ())
    assert "openrouter-gemma" not in plain and "openrouter-gemma" in keyed


def test_long_provider_label_does_not_run_into_its_host():
    u = {"name": "openrouter-nonzdr", "label": "OpenRouter (no ZDR)", "host": "openrouter.ai"}
    assert "OpenRouter (no ZDR) " in tui.row_text("up", u, {}, (), True)
