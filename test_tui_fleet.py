"""Fleet band, Fleet page and band toggle (#58). Run: python3 -m pytest -q test_tui_fleet.py

Nothing here touches the real ~/.claude or models.json: every path the TUI
writes is monkeypatched into tmp_path by the autouse fixture.
"""
import importlib.util, json, os, random, re, stat, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("_tui", os.path.join(HERE, "tui.py"))
tui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tui)

MS, ME = "<!-- fleet-map:start -->", "<!-- fleet-map:end -->"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(tui, "AGENTS_DIR", str(tmp_path / "agents"), raising=False)
    monkeypatch.setattr(tui, "CLAUDE_MD", str(tmp_path / "CLAUDE.md"), raising=False)
    monkeypatch.setattr(tui, "LIVE_FILE", str(tmp_path / "live-upstreams.json"), raising=False)
    monkeypatch.setattr(tui, "PREFS_FILE", str(tmp_path / "tui-prefs.json"), raising=False)


def fixture_cat():
    """Hand-built, shaped like the live models.json (no live file is read)."""
    return {
        "failover_default": [],
        "models": [
            {"id": "claude-haiku-5-5", "tier": "haiku", "billing": "subscription",
             "failover": ["kimi/k2.8", "xiaomi/mimo-v2.6-flash"]},
            {"id": "claude-opus-5", "tier": "opus", "billing": "subscription",
             "failover": ["kimi/k2.8", "xiaomi/mimo-v2.6-pro"]},
            {"id": "claude-opus-5-5", "tier": "opus", "billing": "subscription",
             "failover": ["kimi/k2.8", "xiaomi/mimo-v2.6-pro"]},
            {"id": "kimi/k2.8", "billing": "subscription", "failover": ["xiaomi/mimo-v2.6-pro"]},
            {"id": "xiaomi/mimo-v2.6-pro", "price": [0.2, 0.6]},
            {"id": "xiaomi/mimo-v2.6-flash", "price": [0.1, 0.3]},
            {"id": "anthropic/claude-haiku-5.5", "billing": "metered", "price": [1, 5]},
        ],
    }


AGENTS = [("breaker", "haiku"), ("coder", "haiku"), ("planner", "opus"),
          ("reviewer", "opus"), ("scout", "sonnet"), ("sidekick", "sonnet"),
          ("vault-reader", "sonnet")]

CODER_MD = (
    "---\n"
    "name: coder\n"
    "description: Writes code: one unit at a time\n"
    "tools: Read, Edit, Bash\n"
    "model: haiku\n"
    "color: green\n"
    "---\n"
    "Body. A rule follows:\n"
    "---\n"
    "model: opus\n"
)


def flat(lines):
    return [seg for line in lines for seg in line]


def joined(line):
    return "".join(t for t, _ in line)


# --- chains and metering ---------------------------------------------------

def test_chain_for_follows_router_rules():
    c = {"failover_default": ["kimi/k2.8"], "models": [
        {"id": "claude-opus-5"},                                   # absent -> default
        {"id": "kimi/k2.8", "failover": "xiaomi/mimo-v2.6-pro"},   # str -> [str]
        {"id": "xiaomi/mimo-v2.6-pro", "failover": None},          # None -> default
        {"id": "ghost/row", "failover": ["kimi/k2.8", "kimi/k2.8", "ghost/row",
                                         "nowhere/none"]},          # dupes/self/unknown dropped
    ]}
    assert tui.chain_for(c, "claude-opus-5") == ["kimi/k2.8"]
    assert tui.chain_for(c, "kimi/k2.8") == ["xiaomi/mimo-v2.6-pro"]
    assert tui.chain_for(c, "xiaomi/mimo-v2.6-pro") == ["kimi/k2.8"]
    assert tui.chain_for(c, "ghost/row") == ["kimi/k2.8"]


def test_hop_metered_matches_router():
    c = fixture_cat()
    assert tui.hop_metered(c, "claude-haiku-5-5") is False
    assert tui.hop_metered(c, "claude-unlisted-9") is False     # bare claude-* is the plan
    assert tui.hop_metered(c, "kimi/k2.8") is False
    assert tui.hop_metered(c, "xiaomi/mimo-v2.6-pro") is True
    assert tui.hop_metered(c, "anthropic/claude-haiku-5.5") is True
    assert tui.hop_metered(c, "unknown/thing") is True


def test_chain_groups_merges_identical_chains():
    groups = tui.chain_groups(fixture_cat())
    heads_for_tail = {tuple(tail): heads for heads, tail in groups}
    assert heads_for_tail[("kimi/k2.8", "xiaomi/mimo-v2.6-pro")] == ["claude-opus-5", "claude-opus-5-5"]
    assert heads_for_tail[("kimi/k2.8", "xiaomi/mimo-v2.6-flash")] == ["claude-haiku-5-5"]


# --- live file -------------------------------------------------------------

def test_read_live_tolerates_missing_and_bad_files(tmp_path):
    p = tmp_path / "live.json"
    assert tui.read_live(str(p)) == {}
    for bad in ["{not json", "[1, 2]", '{"a": 3}', '{"a": {"model": 3}}']:
        p.write_text(bad)
        assert tui.read_live(str(p)) == {}, bad
    p.write_text('{"claude-sonnet-5": {"model": "kimi/k2.8", "ts": "x"}, "b": 4}')
    assert tui.read_live(str(p)) == {"claude-sonnet-5": "kimi/k2.8"}


# --- packets ---------------------------------------------------------------

def test_packet_animates_without_live():
    tail = ["a", "b"]
    assert tui.packet_at(["h"], tail, {}, 0) == (0, False)
    assert tui.packet_at(["h"], tail, {}, 1) == (1, False)
    assert tui.packet_at(["h"], tail, {}, 2) == (2, False)
    assert tui.packet_at(["h"], tail, {}, 3) == (0, False)


def test_packet_pins_to_live_hop():
    tail = ["a", "b"]
    assert tui.packet_at(["h"], tail, {"h": "h"}, 5) == (0, True)
    assert tui.packet_at(["h"], tail, {"h": "b"}, 5) == (2, True)
    # a live model that is not in this chain is ignored: the line keeps moving
    assert tui.packet_at(["h"], tail, {"h": "zzz"}, 1) == (1, False)
    # two heads served by different hops: the deepest degradation wins
    assert tui.packet_at(["h1", "h2"], tail, {"h1": "a", "h2": "b"}, 9) == (2, True)


# --- tiers and the band ----------------------------------------------------

def test_tier_chain_uses_first_row_of_the_tier():
    c = fixture_cat()
    assert tui.tier_of(c, "haiku") == "haiku"
    assert tui.tier_of(c, "claude-opus-5-5") == "opus"
    assert tui.tier_of(c, "kimi/k2.8") is None
    assert tui.tier_chain(c, "haiku") == ["claude-haiku-5-5", "kimi/k2.8", "xiaomi/mimo-v2.6-flash"]
    assert tui.tier_chain(c, "claude-opus-5-5") == ["claude-opus-5", "kimi/k2.8", "xiaomi/mimo-v2.6-pro"]
    assert tui.tier_chain(c, "fable") == []


def test_band_lines_mark_metered_hops_and_moving_packet():
    c = fixture_cat()
    lines = tui.band_lines(c, {}, AGENTS, "opus[1m]", 0)
    mimo = [(t, r) for t, r in flat(lines) if "mimo-v2.6-pro" in t]
    assert mimo, "the metered hop should be drawn"
    assert all(r == "warn" and t.endswith(" $") for t, r in mimo)
    f0 = [i for i, (t, _) in enumerate(lines[0]) if "●" in t]
    f1 = [i for i, (t, _) in enumerate(tui.band_lines(c, {}, AGENTS, "opus[1m]", 1)[0]) if "●" in t]
    assert f0 and f1 and f0 != f1


def test_band_lines_pin_and_label_live_tier():
    c = fixture_cat()
    still = flat(tui.band_lines(c, {}, AGENTS, "opus[1m]", 0))
    assert not any("◉" in t for t, _ in still)
    assert not any(t == "  LIVE" for t, _ in still)
    live = tui.band_lines(c, {"claude-opus-5": "xiaomi/mimo-v2.6-pro"}, AGENTS, "opus[1m]", 0)
    assert any("◉" in t for t, _ in flat(live))
    assert any(t == "  LIVE" for t, _ in flat(live))


def test_band_lines_tree_is_always_last_and_groups_by_model():
    c = fixture_cat()
    lines = tui.band_lines(c, {}, AGENTS, "opus[1m]", 0)
    tree = joined(lines[-1])
    assert tree.startswith("main (opus[1m])")
    assert "planner·reviewer (opus)" in tree
    assert "scout·sidekick·vault-reader (sonnet)" in tree
    assert "breaker·coder (haiku)" in tree
    assert tree.index("(opus)") < tree.index("(sonnet)") < tree.index("(haiku)")
    assert tree.endswith("subagents can't spawn")


def test_band_lines_without_chains_says_so():
    lines = tui.band_lines({"models": [{"id": "x/y"}]}, {}, AGENTS, "", 0)
    assert "no fallback chains in models.json: every model fails instead" in joined(lines[0])
    assert "subagents can't spawn" in joined(lines[-1])
    assert "main (main)" in joined(lines[-1])


def test_band_height_hides_on_short_terminals_and_when_toggled_off():
    assert tui.band_height(40, 3, True) == 5
    assert tui.band_height(40, 3, False) == 0
    assert tui.band_height(12, 3, True) == 0


# --- agents on disk --------------------------------------------------------

def test_read_agents_reads_first_frontmatter_only(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    (d / "coder.md").write_text(CODER_MD)
    (d / "planner.md").write_text("no frontmatter here\nmodel: opus\n")
    (d / "quoted.md").write_text('---\nmodel: "sonnet"\n---\n')
    (d / "bad name.md").write_text("---\nmodel: haiku\n---\n")
    (d / "notes.txt").write_text("---\nmodel: haiku\n---\n")
    assert tui.read_agents(str(d)) == [("coder", "haiku"), ("planner", ""), ("quoted", "sonnet")]
    assert tui.read_agents(str(tmp_path / "missing")) == []


def test_next_alias_cycles():
    assert [tui.next_alias(m) for m in ["haiku", "sonnet", "opus", "fable"]] == \
        ["sonnet", "opus", "fable", "haiku"]
    assert tui.next_alias("something-else") == "haiku"


def test_set_model_line_changes_only_the_frontmatter_model():
    out = tui.set_model_line(CODER_MD, "sonnet")
    assert out == CODER_MD.replace("model: haiku\n", "model: sonnet\n", 1)


def test_set_model_line_inserts_when_missing_and_keeps_crlf():
    text = "---\r\nname: x\r\n---\r\nbody\r\n"
    assert tui.set_model_line(text, "opus") == "---\r\nname: x\r\nmodel: opus\r\n---\r\nbody\r\n"


def test_set_model_line_needs_frontmatter_and_a_real_alias():
    with pytest.raises(ValueError):
        tui.set_model_line("# no frontmatter\n", "opus")
    with pytest.raises(ValueError):
        tui.set_model_line("---\nname: x\n", "opus")          # never closed
    with pytest.raises(ValueError):
        tui.set_model_line(CODER_MD, "claude-opus-5")         # full ids are not aliases


# --- CLAUDE.md block -------------------------------------------------------

def test_fleet_block_is_deterministic_and_flags_metered_hops():
    c = fixture_cat()
    a = tui.fleet_block(c, AGENTS, "opus[1m]")
    assert a == tui.fleet_block(c, AGENTS, "opus[1m]")
    assert a.startswith(MS) and a.endswith(ME)
    assert "xiaomi/mimo-v2.6-flash (metered)" in a
    assert "kimi/k2.8 (metered)" not in a
    assert "claude-haiku-5-5 (metered)" not in a
    assert "none -- fails instead" in a


def test_splice_block_appends_when_no_markers():
    b = tui.fleet_block(fixture_cat(), AGENTS, "opus")
    assert tui.splice_block("", b) == b + "\n"
    assert tui.splice_block("hello\n", b) == "hello\n\n" + b + "\n"
    assert tui.splice_block("hello", b) == "hello\n\n" + b + "\n"
    assert tui.splice_block("hello\n\n", b) == "hello\n\n" + b + "\n"


@pytest.mark.parametrize("text", [
    MS + "\nx", "x\n" + ME, ME + "\nx" + MS, MS + MS + ME, MS + ME + ME,
])
def test_splice_block_refuses_malformed_markers(text):
    with pytest.raises(ValueError):
        tui.splice_block(text, "BLOCK")


TOKENS = ["a", " ", "\n", "\r\n", "é", "# h", "<!--", "-->", "fleet-map", "---", "model: x"]


def test_splice_property():
    rng = random.Random(58)
    b1 = tui.fleet_block(fixture_cat(), AGENTS, "opus[1m]")
    b2 = tui.fleet_block(fixture_cat(), AGENTS, "sonnet")
    for _ in range(500):
        text = "".join(rng.choice(TOKENS) for _ in range(rng.randint(0, 25)))
        has_block = rng.random() < 0.5
        if has_block:
            cut = rng.randint(0, len(text))
            text = text[:cut] + b1 + text[cut:]
        out = tui.splice_block(text, b2)
        nl = "\r\n" if "\r\n" in text else "\n"     # the block takes the file's line ending
        if has_block:
            s = text.index(MS)
            e = text.index(ME) + len(ME)
            assert out == text[:s] + b2.replace("\n", nl) + text[e:]
            assert out.startswith(text[:s]) and out.endswith(text[e:])
        else:
            assert out.startswith(text)
        assert tui.splice_block(out, b2) == out


FM_LINES = ["name: coder", "description: Writes code: one unit", "tools: Read, Edit", "color: green"]
BODY_LINES = ["# Body", "model: opus", "", "---", "tools: nope"]


def test_set_model_line_property():
    rng = random.Random(58)
    for _ in range(300):
        eol = rng.choice(["\n", "\r\n"])
        fm = rng.sample(FM_LINES, rng.randint(0, len(FM_LINES)))
        if rng.random() < 0.7:
            fm.insert(rng.randint(0, len(fm)), "model: " + rng.choice(["haiku", "opus", "x"]))
        body = rng.sample(BODY_LINES, rng.randint(0, len(BODY_LINES)))
        text = "---" + eol + "".join(l + eol for l in fm) + "---" + eol + "".join(l + eol for l in body)
        alias = rng.choice(tui.ALIASES)
        lines = text.splitlines(keepends=True)
        close = next(i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---")
        model_at = next((i for i in range(1, close) if lines[i].startswith("model:")), None)
        if model_at is None:
            expected = lines[:close] + ["model: " + alias + eol] + lines[close:]
        else:
            expected = list(lines)
            expected[model_at] = "model: " + alias + eol
        assert "".join(expected) == tui.set_model_line(text, alias)


# --- writing ---------------------------------------------------------------

def write_agents(tmp_path, files):
    d = tmp_path / "agents"
    d.mkdir(exist_ok=True)
    for name, text in files.items():
        (d / (name + ".md")).write_text(text, encoding="utf-8", newline="")
    return str(d)


def test_write_fleet_updates_agent_and_block(tmp_path):
    agents = write_agents(tmp_path, {"coder": CODER_MD})
    claude = str(tmp_path / "CLAUDE.md")
    tui.write_fleet(agents, claude, fixture_cat(), "opus[1m]", "coder", "opus")
    assert open(os.path.join(agents, "coder.md"), encoding="utf-8").read() == \
        CODER_MD.replace("model: haiku\n", "model: opus\n", 1)
    block = open(claude, encoding="utf-8").read()
    assert MS in block and ME in block and "| coder | opus |" in block


def test_write_fleet_creates_missing_claude_md(tmp_path):
    agents = write_agents(tmp_path, {"coder": CODER_MD})
    claude = str(tmp_path / "CLAUDE.md")
    tui.write_fleet(agents, claude, fixture_cat(), "opus[1m]")
    text = open(claude, encoding="utf-8").read()
    assert text.startswith(MS) and text.rstrip().endswith(ME)


def test_write_fleet_keeps_text_outside_markers_byte_identical(tmp_path):
    before = "# My rules\r\nkeep me\n\n"
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(before + "old\n" + MS + "\nstale\n" + ME + "\ntail é\n",
                      encoding="utf-8", newline="")
    agents = write_agents(tmp_path, {"coder": CODER_MD})
    tui.write_fleet(agents, str(claude), fixture_cat(), "opus[1m]")
    text = claude.read_text(encoding="utf-8", newline="")
    assert text.startswith(before + "old\n" + MS)
    assert text.endswith(ME + "\ntail é\n")


def test_malformed_markers_leave_both_files_alone(tmp_path):
    agents = write_agents(tmp_path, {"coder": CODER_MD})
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("half a block " + MS + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        tui.write_fleet(agents, str(claude), fixture_cat(), "opus[1m]", "coder", "opus")
    assert claude.read_text(encoding="utf-8") == "half a block " + MS + "\n"
    assert open(os.path.join(agents, "coder.md"), encoding="utf-8").read() == CODER_MD


def test_unknown_agent_writes_nothing(tmp_path):
    agents = write_agents(tmp_path, {"coder": CODER_MD})
    claude = str(tmp_path / "CLAUDE.md")
    with pytest.raises(ValueError):
        tui.write_fleet(agents, claude, fixture_cat(), "opus[1m]", "ghost", "opus")
    assert not os.path.exists(claude)
    assert open(os.path.join(agents, "coder.md"), encoding="utf-8").read() == CODER_MD


# --- the Fleet page and the band in the UI --------------------------------

class StubSB:
    def load(self):
        return fixture_cat()

    def save(self, cat):
        raise AssertionError("the Fleet page must never write models.json")

    START_MODEL = os.path.join(HERE, "no-such-start-model")
    SETTINGS = os.path.join(HERE, "no-such-settings.json")

    def read_keyfile(self, path):
        return ""


def fleet_ui(page=5):
    u = tui.UI.__new__(tui.UI)
    u.sb, u.scr, u.cat = StubSB(), None, fixture_cat()
    u.sel = u.top = 0
    u.dirty, u.status, u.rows = False, "", []
    u.primary, u.new_primary, u.keyuse, u.spend = "opus[1m]", None, {}, {}
    u.page = page
    u.agents = AGENTS
    u.live = {}
    u.rebuild()
    return u


def test_set_agent_never_touches_models_json(tmp_path):
    agents = write_agents(tmp_path, {"coder": CODER_MD})
    u = fleet_ui()
    u.agents = tui.read_agents(agents)
    u.set_agent(("coder", "haiku"))
    assert u.dirty is False
    assert "model: sonnet\n" in open(os.path.join(agents, "coder.md"), encoding="utf-8").read()
    assert MS in open(tui.CLAUDE_MD, encoding="utf-8").read()
    assert ("coder", "sonnet") in u.agents


def test_band_toggle_hides_and_survives_restart():
    u = tui.UI(StubSB(), None)
    assert u.band_on is True                       # starts shown
    u.toggle_band()
    assert u.band_on is False
    assert tui.band_height(40, 3, u.band_on) == 0  # hidden in the layout
    again = tui.UI(StubSB(), None)                 # a restart reads the saved choice
    assert again.band_on is False
    again.toggle_band()
    assert tui.UI(StubSB(), None).band_on is True


# --- review round 1 (PR #59): adopted breaker tests and their siblings ---------

cat = fixture_cat()


def test_crlf_file_stays_crlf_after_append():
    text = "# Notes\r\nkeep me\r\n"
    out = tui.splice_block(text, tui.fleet_block(cat, [("coder", "haiku")], "opus"))
    assert not re.findall(r"(?<!\r)\n", out)


def test_crlf_file_stays_crlf_inside_markers():
    text = "# Notes\r\n%s\r\nold\r\n%s\r\nafter\r\n" % (MS, ME)
    out = tui.splice_block(text, tui.fleet_block(cat, [("coder", "haiku")], "opus"))
    assert not re.findall(r"(?<!\r)\n", out)
    assert out.startswith("# Notes\r\n" + MS + "\r\n") and out.endswith(ME + "\r\nafter\r\n")


def test_bom_agent_file_model_is_read():
    assert tui._model_of("﻿---\nname: x\nmodel: sonnet\n---\nbody\n") == "sonnet"


def test_bom_agent_file_can_be_set():
    out = tui.set_model_line("﻿---\nname: x\nmodel: sonnet\n---\nbody\n", "opus")
    assert out.startswith("﻿---\n") and "model: opus\n" in out and "model: sonnet" not in out


def test_unreadable_agent_row_not_silently_dropped(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.md").write_text("---\nname: a\nmodel: haiku\n---\n")
    b = agents / "b.md"
    b.write_text("---\nname: b\nmodel: opus\n---\n")
    md = tmp_path / "CLAUDE.md"
    os.chmod(b, 0)
    try:
        tui.write_fleet(str(agents), str(md), cat, "opus", "a", "sonnet")
    finally:
        os.chmod(b, 0o644)
    assert "| b |" in md.read_text(encoding="utf-8")


def test_unreadable_agent_cannot_be_set_and_writes_nothing(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    b = agents / "b.md"
    b.write_text("---\nmodel: opus\n---\n")
    md = tmp_path / "CLAUDE.md"
    os.chmod(b, 0)
    try:
        with pytest.raises(OSError):
            tui.write_fleet(str(agents), str(md), cat, "opus", "b", "haiku")
    finally:
        os.chmod(b, 0o644)
    assert not md.exists()


def test_failed_claude_md_write_leaves_agent_file_unchanged(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    agent = agents / "coder.md"
    agent.write_text("---\nname: coder\nmodel: haiku\n---\nbody\n")
    md_dir = tmp_path / "ro"
    md_dir.mkdir()
    md = md_dir / "CLAUDE.md"
    md.write_text("# top\n%s\nold\n%s\n" % (MS, ME))
    os.chmod(md_dir, 0o555)
    try:
        with pytest.raises(OSError):
            tui.write_fleet(str(agents), str(md), cat, "opus", "coder", "sonnet")
    finally:
        os.chmod(md_dir, 0o755)
    assert agent.read_text() == "---\nname: coder\nmodel: haiku\n---\nbody\n"


def test_failover_not_a_list_does_not_crash_band():
    tui.band_lines({"models": [{"id": "claude-haiku-5-5", "failover": 5}]}, {}, [], "opus", 0)


def test_failover_null_entry_in_models_list_does_not_crash():
    tui.band_lines({"models": [None, {"id": "claude-haiku-5-5", "failover": ["kimi/k2.8"]},
                               {"id": "kimi/k2.8"}]}, {}, [], "opus", 0)


def test_failover_list_with_junk_entries_keeps_the_good_ones():
    c = {"models": [{"id": "claude-haiku-5-5", "failover": [["x"], 3, "kimi/k2.8"]},
                    {"id": "kimi/k2.8"}]}
    lines = tui.band_lines(c, {}, [], "opus", 0)
    assert "kimi/k2.8" in "".join(t for t, _ in lines[0])


def test_both_files_untouched_when_claude_md_write_fails(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    agent = agents / "coder.md"
    agent.write_text("---\nname: coder\nmodel: haiku\n---\nbody\n")
    md_dir = tmp_path / "ro"
    md_dir.mkdir()
    md = md_dir / "CLAUDE.md"
    md.write_text("# top\n%s\nold\n%s\n" % (MS, ME))
    os.chmod(md_dir, 0o555)
    try:
        with pytest.raises(OSError):
            tui.write_fleet(str(agents), str(md), cat, "opus", "coder", "sonnet")
    finally:
        os.chmod(md_dir, 0o755)
    assert agent.read_text() == "---\nname: coder\nmodel: haiku\n---\nbody\n"


def test_agent_model_with_a_marker_refuses_and_leaves_claude_md_alone(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "a.md").write_text("---\nmodel: haiku\n---\n")
    (agents / "b.md").write_text("---\nmodel: " + ME + "\n---\n")
    md = tmp_path / "CLAUDE.md"
    md.write_text("# top\n")
    with pytest.raises(ValueError):
        tui.write_fleet(str(agents), str(md), cat, "opus", "a", "sonnet")
    assert md.read_text() == "# top\n"


def test_write_keeps_file_modes(tmp_path):
    agents = tmp_path / "agents"
    agents.mkdir()
    agent = agents / "coder.md"
    agent.write_text(CODER_MD)
    md = tmp_path / "CLAUDE.md"
    md.write_text("# top\n")
    os.chmod(agent, 0o600)
    os.chmod(md, 0o600)
    tui.write_fleet(str(agents), str(md), cat, "opus", "coder", "sonnet")
    assert stat.S_IMODE(os.stat(agent).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(md).st_mode) == 0o600
