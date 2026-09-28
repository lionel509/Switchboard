"""Editor pages (#21). Run: python3 -m pytest test_tui_pages.py"""
import importlib.util, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import switchboard as sb
spec = importlib.util.spec_from_file_location("_tui", os.path.join(HERE, "tui.py"))
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)


def ui(page):
    u = tui.UI.__new__(tui.UI)
    u.sb, u.scr, u.cat = sb, None, sb.load()
    u.sel = u.top = 0
    u.dirty, u.status, u.rows = False, "", []
    u.primary, u.new_primary, u.keyuse, u.spend = "opus[1m]", None, {"openrouter": None}, {"openrouter": {"requests": 1, "cost": 0.1, "failover_cost": 0, "refused": 0}}
    u.page = page
    u.rebuild()
    return {k for k, _ in u.rows} - {"head", "blank", "note"}


def test_each_page_shows_only_its_own_rows():
    assert tui.PAGES == ["Providers", "Menu", "Models", "Policy", "Spend"]
    assert ui(0) == {"up", "prov"}
    assert ui(1) == {"menu"}
    assert ui(2) == {"model"}
    assert ui(3) == {"pol"}
    assert ui(4) == {"spend", "keyuse"}


def test_settings_pages_cover_every_field_once():
    m = {"id": "google/gemma-4-31b-it", "price": [0.09, 0.34]}
    rows = tui.settings_rows({"models": [m]}, m)
    everything = [r[1] for r in rows if r[0] == "field"]
    paged = [r[1] for p in range(len(tui.SETTINGS_PAGES))
             for r in tui.settings_page(rows, p) if r[0] == "field"]
    assert sorted(paged) == sorted(everything)
    assert [r[1] for r in tui.settings_page(rows, 0) if r[0] == "head"] == ["IDENTITY", "MENU"]
