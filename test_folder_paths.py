#!/usr/bin/env python3
"""Folder policy reads a row the same way however the cwd is spelled (#49).

Run: python3 -m pytest test_folder_paths.py

A private folder keeps its data policy under another Unicode spelling, a symlink,
"." or "..", and a subfolder row with no data key inherits the enclosing row. A
hand-edited odd value fails closed as claude. "Private" is a made-up folder name.
"""
import importlib.util, os, random, sys, unicodedata

import pytest

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

H = os.path.expanduser("~")
THIRD_PARTY = "kimi/k2.8"
DEFAULT = {"data": "any", "tier": "pro", "effort": "medium"}
PRIV = {"~/Documents/Private": {"data": "claude", "effort": "xhigh"}}


def NFC(s):
    return unicodedata.normalize("NFC", s)


def NFD(s):
    return unicodedata.normalize("NFD", s)


def use(mp, rows, default=DEFAULT):
    mp.setattr(r, "POLICY", dict([("_default", dict(default))] + list(rows.items())))


def level(cwd):
    return r.data_level(r.policy_for(cwd).get("data"))


# 1. one reading of a data value -----------------------------------------------
def test_data_level_table():
    for v in (None, "", "any", "Any", "ANY"):
        assert r.data_level(v) == "any", v
    for v in ("zdr", "ZDR", "Zdr"):
        assert r.data_level(v) == "zdr", v
    for v in ("claude", "Claude", "CLAUDE", "clade", " claude", "zdr ", " any ",
              "nay", False, 0, [], {}, ["claude"], 1, True):
        assert r.data_level(v) == "claude", v
    for v in (False, 0, [], {}):
        assert not r.allowed(THIRD_PARTY, {"data": v}), v
    assert r.allowed(THIRD_PARTY, {"data": None})
    assert r.allowed(THIRD_PARTY, {"data": ""})
    assert r.allowed(THIRD_PARTY, {})


# 2. a subfolder with no data key inherits its enclosing row -------------------
def test_subfolder_without_data_inherits_enclosing_row(monkeypatch):
    use(monkeypatch, dict(PRIV, **{"~/Documents/Private/sub": {"effort": "max"},
                                   "~/Documents/Private/sub/deeper": {"tier": "flash"}}))
    for cwd in (H + "/Documents/Private/sub", H + "/Documents/private/SUB/x"):
        pol = r.policy_for(cwd)
        assert level(cwd) == "claude" and pol["effort"] == "max", cwd
        assert not r.allowed(THIRD_PARTY, pol), cwd
    pol = r.policy_for(H + "/Documents/Private/sub/deeper/x")
    assert level(H + "/Documents/Private/sub/deeper/x") == "claude"
    assert pol["effort"] == "max" and pol["tier"] == "flash"
    # A case twin with an explicit "any" cannot loosen the inherited claude.
    use(monkeypatch, dict(PRIV, **{"~/Documents/Private/sub": {"effort": "max"},
                                   "~/Documents/private/sub": {"data": "any"}}))
    assert level(H + "/Documents/Private/sub/x") == "claude"


# 3. a subfolder inherits the enclosing effort, not _default's -----------------
def test_subfolder_inherits_enclosing_effort(monkeypatch):
    use(monkeypatch, dict(PRIV, **{"~/Documents/Private/Public": {"data": "any"}}))
    pol = r.policy_for(H + "/Documents/Private/Public/x")
    assert r.data_level(pol.get("data")) == "any"   # an explicit looser row still applies
    assert pol["effort"] == "xhigh"                 # master gave "medium"
    assert r.allowed(THIRD_PARTY, pol)


# 4. an inherited data is never looser than _default's -------------------------
def test_inherited_data_never_looser_than_default(monkeypatch):
    use(monkeypatch, {"~/Documents": {"data": "any"}, "~/Documents/X": {"effort": "max"}},
        default={"data": "claude", "tier": "pro"})
    assert level(H + "/Documents/X/y") == "claude"
    assert level(H + "/Documents/y") == "any"


# 5. a non-object row fails closed ---------------------------------------------
def test_non_object_row_fails_closed(monkeypatch):
    for bad in ("claude", "any", None, ["any"], 0):
        use(monkeypatch, {"~/Documents/Private": bad})
        assert level(H + "/Documents/Private/x") == "claude", bad
    use(monkeypatch, {"~/Documents/Private": "any",
                      "~/Documents/Private/sub": {"effort": "max"}})
    assert level(H + "/Documents/Private/sub/x") == "claude"


# 6. Unicode spellings of one name match -----------------------------------------
def test_unicode_spellings_match(monkeypatch):
    pairs = [("ΟΔΟΣ", "οδος"), ("οδοσ", "ΟΔΟΣ"), ("Straße", "STRASSE"),
             ("STRASSE", "straße"), (NFC("Résumé"), NFD("Résumé")),
             (NFD("Résumé"), NFC("RÉSUMÉ"))]
    for row, cwd in pairs:
        assert row != cwd, (row, cwd)
        use(monkeypatch, {"~/Documents/" + row: {"data": "claude"}})
        assert level(H + "/Documents/" + row) == "claude", (row, cwd)
        assert level(H + "/Documents/" + cwd + "/notes") == "claude", (row, cwd)


# 7. the fold does not equate dotted capital I with i --------------------------
def test_fold_does_not_equate_dotted_capital_i(monkeypatch):
    use(monkeypatch, {"~/Documents/İstanbul": {"data": "claude"}})
    assert level(H + "/Documents/İstanbul") == "claude"
    # APFS keeps these apart (PR #48 breaker round 1, "Tried, no break"), so a fold
    # that also strips combining marks over-matches here.
    assert level(H + "/Documents/istanbul") == "any"


# 8. "." and ".." and "//" resolve before the match ----------------------------
def test_dot_segments_resolve(monkeypatch):
    use(monkeypatch, PRIV)
    for cwd in (H + "/Documents/Other/../Private/notes", H + "/Documents/./Private",
                H + "/Documents//Private/sub"):
        assert level(cwd) == "claude", cwd
    # The as-sent spelling matched Private on master; the stricter reading wins.
    assert level(H + "/Documents/Private/../Other") == "claude"


# 9. a symlink into a private folder reaches its row ---------------------------
def test_symlink_alias_reaches_row(tmp_path, monkeypatch):
    real = tmp_path / "Private"
    real.mkdir()
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    use(monkeypatch, {str(real): {"data": "claude", "effort": "xhigh"}})
    pol = r.policy_for(str(alias / "notes"))
    assert r.data_level(pol.get("data")) == "claude" and pol["effort"] == "xhigh"


# 10. a symlink out of a private folder keeps that folder's data ---------------
def test_symlink_out_of_private_keeps_its_data(tmp_path, monkeypatch):
    real = tmp_path / "Private"
    real.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(out, real / "link")
    use(monkeypatch, {str(real): {"data": "claude"}})
    assert level(str(real / "link" / "x")) == "claude"


# 11. a symlinked root matches the physical cwd --------------------------------
def test_symlinked_root_matches_physical_cwd(tmp_path, monkeypatch):
    real = tmp_path / "Private"
    real.mkdir()
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    use(monkeypatch, {str(alias): {"data": "claude"}})
    assert level(str(real / "notes")) == "claude"


# 12. an empty or relative cwd never reads the disk ----------------------------
def test_empty_and_relative_cwd_never_read_the_disk(tmp_path, monkeypatch):
    real = tmp_path / "Private"
    real.mkdir()
    use(monkeypatch, {str(real): {"data": "claude"}})
    monkeypatch.chdir(real)
    assert r.policy_for(None) == DEFAULT
    assert r.policy_for("") == DEFAULT
    assert level("notes") == "any"
    assert level(".") == "any"


# 13. a NUL byte in a cwd does not raise ---------------------------------------
def test_nul_in_cwd_does_not_raise(monkeypatch):
    use(monkeypatch, PRIV)
    assert level(H + "/Documents/Private/a\x00b") == "claude"
    assert level(H + "/Documents/Other\x00") == "any"


# 14. the real catalog keeps its claude-only row (#48 round-1 finding #7) ------
def test_real_catalog_keeps_a_claude_only_row(monkeypatch):
    rows = r.CATALOG.get("folder_policy", {})
    keys = [k for k, p in rows.items()
            if not k.startswith("_") and isinstance(p, dict)
            and r.data_level(p.get("data")) == "claude"]
    monkeypatch.setattr(r, "POLICY", rows)
    ok = bool(keys) and all(
        r.data_level(r.policy_for(os.path.expanduser(k) + "/x").get("data")) == "claude"
        for k in keys)
    # Never put a key or a path in this assert: the repo is public.
    assert ok, "the catalog lost its claude-only folder row"


# 15. never looser than master 53d8b84 ----------------------------------------
RANK = {"claude": 0, "zdr": 1, "any": 2}


def master_level(v):
    if not v:
        return "any"
    if not isinstance(v, str):
        return "claude"
    v = v.lower()
    return v if v in ("claude", "any") else "zdr"


def master_policy_for(policy, cwd):
    pol = dict(policy.get("_default") or r.POLICY_DEFAULT)
    cwd = (cwd or "").lower()
    best, hits = None, []
    for path, p in policy.items():
        if path.startswith("_") or not isinstance(p, dict):
            continue
        root = os.path.expanduser(path).rstrip("/").lower()
        if cwd == root or cwd.startswith(root + "/"):
            if best is None or len(root) > len(best):
                best, hits = root, [(path, p)]
            elif len(root) == len(best):
                hits.append((path, p))
    dflt = pol.get("data")
    hits.sort(key=lambda h: h[0])
    for _, p in hits:
        pol.update(p)
    if len(hits) > 1:
        pol["data"] = min((p["data"] if "data" in p else dflt for _, p in hits),
                          key=lambda d: RANK[master_level(d)])
    return pol


def test_never_looser_than_master(monkeypatch):
    KEYS = ["~/Documents", "~/Documents/Private", "~/Documents/private",
            "~/Documents/Private/sub", "~/Documents/PRIVATE/Sub"]
    VALUES = ["claude", "zdr", "any", None, "", "Claude", "ZDR", "Any", "clade",
              " claude", "zdr ", False, 0, [], {}, ["claude"], 1]
    CWDS = [H + s for s in ("/Documents", "/Documents/Private", "/documents/private/notes",
                            "/Documents/Private/sub", "/Documents/private/SUB/x",
                            "/Documents/Other", "/Documents/Private/../Other",
                            "/Documents/Other/../Private/x", "/Documents/./Private",
                            "/Documents//Private/sub")]
    rng = random.Random(49)
    stricter = 0
    for _ in range(3000):
        policy = {"_default": {"data": rng.choice(["any", "zdr", "claude"]),
                               "tier": "pro", "effort": "medium"}}
        for k in rng.sample(KEYS, rng.randint(1, len(KEYS))):
            if rng.random() < 0.1:
                policy[k] = rng.choice(["claude", None, ["any"]])
                continue
            row = {}
            if rng.random() < 0.7:
                row["data"] = rng.choice(VALUES)
            if rng.random() < 0.5:
                row["effort"] = rng.choice(["low", "max"])
            policy[k] = row
        monkeypatch.setattr(r, "POLICY", policy)
        for cwd in CWDS:
            new = RANK[r.data_level(r.policy_for(cwd).get("data"))]
            old = RANK[master_level(master_policy_for(policy, cwd).get("data"))]
            assert new <= old, (policy, cwd)
            stricter += new < old
    # Control: the generator reaches the cases the fix changes.
    assert stricter > 0


# 12. a symlink out of a private folder keeps its effort and tier --------------
def test_symlink_out_keeps_as_sent_effort_and_tier(tmp_path, monkeypatch):
    real = tmp_path / "Private"
    real.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    os.symlink(out, real / "link")
    use(monkeypatch, {str(real): {"data": "claude", "effort": "xhigh", "tier": "plan"}})
    pol = r.policy_for(str(real / "link" / "x"))
    assert (r.data_level(pol["data"]), pol["effort"], pol["tier"]) == ("claude", "xhigh", "plan")


# 13. a nested row with null or "" data inherits the enclosing row's data ------
def test_nested_null_or_empty_data_inherits(monkeypatch):
    for v in (None, ""):
        use(monkeypatch, {"~/Documents/Private": {"data": "claude"},
                          "~/Documents/Private/sub": {"data": v}})
        assert level(H + "/Documents/Private/sub/x") == "claude", v
        use(monkeypatch, {"~/Documents/Private": {"data": v}})
        assert level(H + "/Documents/Private/x") == "any", v


if __name__ == "__main__":
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", __file__]))
