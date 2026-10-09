"""Editor does everything (#16): menu, fallback rule, policy, spend. Run: python3 -m pytest test_tui_menu.py"""
import importlib.util, json, os, pytest

spec = importlib.util.spec_from_file_location(
    "_tui", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tui.py"))
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)


def cat():
    return {"models": [
        {"id": "claude-sonnet-5", "billing": "subscription", "price": [0, 0]},
        {"id": "kimi/k2.8", "billing": "subscription", "price": [0, 0]},
        {"id": "~deepseek/deepseek-v4-flash-latest", "price": [0.05, 0.16]},
        {"id": "google/gemma-4-31b-it", "price": [0.09, 0.34]},
        {"id": "~google/gemini-pro-latest", "price": [2.0, 12.0]}],
        "picker": {"models": []},
        "picker_lineup": [{"model": "opus[1m]", "label": "Opus (1M)"}, {"model": "kimi/k2.8"}],
        "folder_policy": {"_default": {"data": "any", "tier": "pro"}, "~/Documents/State Street": {"data": "zdr"}}}


def test_menu_toggle_adds_to_the_menu_and_the_picker_then_removes():
    c = cat()
    tui.menu_toggle(c, "google/gemma-4-31b-it")
    assert [r["model"] for r in c["picker_lineup"]][-1] == "google/gemma-4-31b-it"
    assert "google/gemma-4-31b-it" in c["picker"]["models"]
    tui.menu_toggle(c, "google/gemma-4-31b-it")
    assert "google/gemma-4-31b-it" not in [r["model"] for r in c["picker_lineup"]]
    assert "google/gemma-4-31b-it" not in c["picker"]["models"]
    assert tui.in_menu(c, "kimi/k2.8") and not tui.in_menu(c, "google/gemma-4-31b-it")


def test_menu_move_reorders_and_stops_at_the_ends():
    c = cat()
    tui.menu_move(c, "kimi/k2.8", -1)
    assert [r["model"] for r in c["picker_lineup"]] == ["kimi/k2.8", "opus[1m]"]
    tui.menu_move(c, "kimi/k2.8", -1)
    assert [r["model"] for r in c["picker_lineup"]] == ["kimi/k2.8", "opus[1m]"]


def test_fallback_rule_names_every_metered_pro_tier_fallback():
    c = cat()
    assert tui.fallback_problems(c, ["kimi/k2.8", "~deepseek/deepseek-v4-flash-latest"]) == []
    probs = tui.fallback_problems(c, ["~google/gemini-pro-latest", "nobody/unknown"])
    assert len(probs) == 2
    assert "gemini-pro" in probs[0] and "$2" in probs[0]
    assert "unknown" in probs[1]


def test_policy_cycles_data_and_tier():
    c = cat()
    tui.cycle_policy(c, "~/Documents/State Street", "data")
    assert c["folder_policy"]["~/Documents/State Street"]["data"] == "claude"
    tui.cycle_policy(c, "~/Documents/State Street", "data")
    assert c["folder_policy"]["~/Documents/State Street"]["data"] == "any"
    tui.cycle_policy(c, "_default", "tier")
    assert c["folder_policy"]["_default"]["tier"] == "flash"


def test_loosening_is_detected_both_ways():
    assert tui.loosens("claude", "any") and tui.loosens("zdr", "any") and tui.loosens("claude", "zdr")
    assert not tui.loosens("any", "zdr") and not tui.loosens("zdr", "claude") and not tui.loosens("zdr", "zdr")


def test_cycle_unknown_data_lands_on_claude():
    # A hand-edited value the router reads as claude (#49): the first enter lands there.
    for bad in ("clade", "ZDR", "Any", " claude", [], 0, False):
        c = cat()
        c["folder_policy"]["~/Documents/X"] = {"data": bad}
        tui.cycle_policy(c, "~/Documents/X", "data")
        assert c["folder_policy"]["~/Documents/X"]["data"] == "claude", bad


def test_cycle_row_without_data_lands_on_claude():
    # A row with no data inherits a level the editor can't see (#49); a new row too.
    c = cat()
    c["folder_policy"]["~/Documents/State Street/sub"] = {"effort": "max"}
    tui.cycle_policy(c, "~/Documents/State Street/sub", "data")
    row = c["folder_policy"]["~/Documents/State Street/sub"]
    assert row["data"] == "claude" and row["effort"] == "max"
    tui.cycle_policy(c, "~/Documents/New", "data")
    assert c["folder_policy"]["~/Documents/New"]["data"] == "claude"


def test_cycle_unknown_tier_still_resets_to_first():
    c = cat()
    c["folder_policy"]["~/Documents/X"] = {"tier": "ultra"}
    tui.cycle_policy(c, "~/Documents/X", "tier")
    assert c["folder_policy"]["~/Documents/X"]["tier"] == "pro"
    c["folder_policy"]["~/Documents/Y"] = {}
    tui.cycle_policy(c, "~/Documents/Y", "tier")
    assert c["folder_policy"]["~/Documents/Y"]["tier"] == "flash"
