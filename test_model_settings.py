"""Per-model settings screen (#19). Run: python3 -m pytest test_model_settings.py"""
import importlib.util, os, pytest

spec = importlib.util.spec_from_file_location(
    "_tui", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tui.py"))
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)

CAT = {"models": [
    {"id": "google/gemma-4-31b-it", "name": "Gemma 4 31B", "tier": "flash", "price": [0.09, 0.34], "context": 262144},
    {"id": "kimi/k3", "name": "Kimi K3", "tier": "pro", "billing": "subscription", "price": [0, 0], "context": 1048576},
    {"id": "claude-sonnet-5", "name": "Claude Sonnet", "tier": "sonnet", "billing": "subscription", "price": [0, 0]}],
    "upstreams": [{"name": "openrouter", "host": "openrouter.ai", "key_file": "k"}],
    "turn_caps": {"per_hour": [[0.5, 600], [None, 40]]},
    "picker_lineup": [{"model": "kimi/k3"}]}


def keys(m):
    return [r[1] for r in tui.settings_rows(CAT, m, primary="kimi/k3") if r[0] == "field"]


def test_openrouter_models_get_every_routing_and_limit_row():
    k = keys(CAT["models"][0])
    for want in ("name", "blurb", "tier", "menu", "primary", "upstream", "zdr", "auto", "failover",
                 "turn_cap", "thinking_budget", "context", "price", "good_at", "specialties", "source"):
        assert want in k, want


def test_plan_models_hide_key_cap_and_reasoning():
    k = keys(CAT["models"][1])
    assert "menu" in k and "auto" in k and "failover" in k
    for gone in ("upstream", "turn_cap", "thinking_budget", "zdr"):
        assert gone not in k, gone


def test_values_read_plainly():
    rows = {r[1]: r[4] for r in tui.settings_rows(CAT, CAT["models"][0], primary="kimi/k3") if r[0] == "field"}
    assert rows["menu"] == "no" and rows["primary"] == "no"
    assert rows["turn_cap"] == "600/h (price-tier default)"
    assert rows["thinking_budget"].startswith("12,000 tokens (default)")
    assert rows["zdr"].startswith("yes")
    rows = {r[1]: r[4] for r in tui.settings_rows(CAT, CAT["models"][1], primary="kimi/k3") if r[0] == "field"}
    assert rows["menu"] == "yes" and rows["primary"].startswith("yes")


@pytest.mark.parametrize("typed,want", [("", None), ("0", 0), ("250", 250), (" 40 ", 40)])
def test_parse_number_setting(typed, want):
    assert tui.parse_setting(typed) == want


@pytest.mark.parametrize("typed", ["-3", "lots", "1.5"])
def test_parse_number_setting_rejects_nonsense(typed):
    with pytest.raises(ValueError):
        tui.parse_setting(typed)


def test_an_existing_rule_breaking_fallback_is_flagged_not_hidden():
    cat = dict(CAT, models=CAT["models"] + [{"id": "moonshotai/kimi-k3", "price": [3.0, 15.0]}])
    m = dict(CAT["models"][1], failover=["moonshotai/kimi-k3"])
    val = {r[1]: r[4] for r in tui.settings_rows(cat, m) if r[0] == "field"}["failover"]
    assert val.startswith("moonshotai/kimi-k3") and "⚠ 1 over" in val
    ok = dict(CAT["models"][1], failover=["google/gemma-4-31b-it"])
    assert "⚠" not in {r[1]: r[4] for r in tui.settings_rows(cat, ok) if r[0] == "field"}["failover"]
