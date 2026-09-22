#!/usr/bin/env python3
"""Folder data policy: the rail that keeps a private vault off a third party.

Run: python3 test_policy.py     (silent means pass)

Every case below fails loudly if the rail stops holding -- the point is that a
real Vanguard path with a real third-party model id comes back refused, not that
the functions merely run.
"""
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

HOME = os.path.expanduser("~")
VANGUARD = HOME + "/Documents/Vanguard"
STATE = HOME + "/Documents/State Street"
BLACKROCK = HOME + "/Documents/BlackRock"

# --- cwd extraction from a body shaped like Claude Code's -------------------
BODY = ('{"model":"x","messages":[{"role":"user","content":"<system-reminder>\\n'
        '# Environment\\nYou have been invoked in the following environment: \\n'
        ' - Primary working directory: %s\\n - Is a git repository: true\\n'
        '</system-reminder>"}]}')
assert r.cwd_of((BODY % VANGUARD).encode()) == VANGUARD, r.cwd_of((BODY % VANGUARD).encode())
assert r.cwd_of(b'{"model":"x"}') == "", "no Environment block must yield no cwd"
assert r.cwd_of(b"") == ""

# --- longest-prefix matching ------------------------------------------------
assert r.policy_for(VANGUARD)["data"] == "claude"
assert r.policy_for(VANGUARD + "/entities")["data"] == "claude", "subfolder inherits"
assert r.policy_for(STATE)["data"] == "zdr"
assert r.policy_for(BLACKROCK)["data"] == "any", "unlisted folder gets _default"
assert r.policy_for("")["data"] == "any", "unknown cwd gets _default"
# A sibling whose name merely starts the same must NOT match.
assert r.policy_for(HOME + "/Documents/VanguardNotes")["data"] == "any"

# --- the rail itself --------------------------------------------------------
CLAUDE_ONLY = {"data": "claude"}
ZDR = {"data": "zdr"}
ANY = {"data": "any"}

# Anthropic is allowed everywhere.
assert r.allowed("claude-sonnet-5", CLAUDE_ONLY)
assert r.allowed("claude-sonnet-5", ZDR)

# Vanguard: nothing but Claude. These are the cases that matter.
assert not r.allowed("kimi/k2.8", CLAUDE_ONLY), "vendor plan must be refused"
assert not r.allowed("qwen/qwen3.8-flash", CLAUDE_ONLY)
assert not r.allowed("x-ai/grok-4.7", CLAUDE_ONLY), "even a ZDR route is still third party"
assert not r.allowed("xiaomi/mimo-v2.6-flash", CLAUDE_ONLY)

# Finances: third party is fine, retention is not.
assert r.allowed("x-ai/grok-4.7", ZDR), "grok has a ZDR provider"
assert not r.allowed("qwen/qwen3.8-flash", ZDR), "qwen is flagged zdr:false"
assert not r.allowed("xiaomi/mimo-v2.6-flash", ZDR), "mimo has no ZDR endpoint"
assert not r.allowed("kimi/k2.8", ZDR), \
    "a vendor plan retains under its own terms; must not pass as ZDR"

# Homework: anything goes.
for m in ("kimi/k2.8", "qwen/qwen3.8-flash", "xiaomi/mimo-v2.6-flash", "claude-sonnet-5"):
    assert r.allowed(m, ANY), m

# --- the negative control: the rail must be able to go red ------------------
# If `allowed` ever degenerates to "return True", the asserts above catch it.
# This proves the inverse too -- a policy that permits nothing refuses Claude.
assert not r.allowed("kimi/k2.8", {"data": "claude"})
assert r.allowed("kimi/k2.8", {"data": "any"})

# --- which OpenRouter key gets sent -----------------------------------------
# The ZDR guardrail lives on the workspace, so the key decides reachability.
# Only a zdr:false model may reach the permissive workspace.
strict = r.read_key(r.KEYFILE)
loose = r.read_key(r.NONZDR_KEYFILE)
if strict and loose:
    assert strict != loose, "the two key files must not hold the same key"
    assert r.or_key("qwen/qwen3.8-flash") == loose, "zdr:false model needs the loose key"
    assert r.or_key("xiaomi/mimo-v2.6-flash") == loose
    assert r.or_key("x-ai/grok-4.7") == strict, "a ZDR route must stay on the strict key"
    assert r.or_key("deepseek/deepseek-v4-pro") == strict
    assert r.or_key(None) == strict, "no model named -> strict by default"
    assert r.or_key("claude-sonnet-5") == strict

    # The two rails must compose: a model that reaches the permissive workspace
    # must still be refused outright in a folder that forbids it.
    for m in ("qwen/qwen3.8-flash", "xiaomi/mimo-v2.6-flash"):
        assert r.or_key(m) == loose and not r.allowed(m, {"data": "zdr"}), m
        assert not r.allowed(m, {"data": "claude"}), m

# --- the resolvers must stay inside the policy -------------------------------
# Auto, the arrow row and a family row must CHOOSE within the policy rather than
# pick something forbidden and get 403'd afterwards.
CLAUDE_POL = {"data": "claude", "tier": "pro"}
REQ = {"messages": [{"role": "user", "content": "rename a variable in one file"}]}

got = r.resolve_arrow({"output_config": {"effort": "low"}}, CLAUDE_POL)
assert r.allowed(got, CLAUDE_POL), "arrow row escaped the policy: %s" % got

for fam in sorted({m.get("family") for m in r.CANDIDATES} - {None}):
    got = r.resolve_family(REQ, fam, None, CLAUDE_POL)
    assert r.allowed(got, CLAUDE_POL), "family %s escaped the policy: %s" % (fam, got)

# A pinned Auto choice that a policy forbids must not be reused. Pin a forbidden
# model for this conversation, then resolve under the strict policy.
key = r.conv_key(REQ)
r._auto_pins[key] = "kimi/k2.8"
got = r.resolve_auto(REQ, CLAUDE_POL)
assert r.allowed(got, CLAUDE_POL), "a stale pin escaped the policy: %s" % got
r._auto_pins.pop(key, None)

# --- family tier preference -------------------------------------------------
# The MiMo row in the /model menu is a family row, so "pro by default, flash per
# folder" is a user-facing promise. Assert on it unconditionally rather than
# behind an `if`: a family that loses a tier must break this, not skip it.
MENU_FAM = "mimo"
tiers = {m.get("tier") for m in r.family_of(MENU_FAM)}
assert {"flash", "pro"} <= tiers, \
    "%s is a family row in the menu but no longer offers flash+pro: %s" % (MENU_FAM, tiers)
for want in ("flash", "pro"):
    got = r.resolve_family(REQ, MENU_FAM, want, {"data": "any"})
    assert r.BY_ID[got].get("tier") == want, "%s -> %s" % (want, got)

# Default policy is pro, and that is what the menu row promises.
assert r.policy_for("")["tier"] == "pro"
got = r.resolve_family(REQ, MENU_FAM, r.policy_for("")["tier"], {"data": "any"})
assert r.BY_ID[got].get("tier") == "pro", got

# A family row must not leak a forbidden model: under a strict policy every
# MiMo is non-ZDR, so it has to fall back off the family entirely.
got = r.resolve_family(REQ, MENU_FAM, "pro", {"data": "zdr"})
assert r.BY_ID.get(got, {}).get("family") != MENU_FAM, "family row leaked: %s" % got
assert r.allowed(got, {"data": "zdr"}), got


# --- the effort dial --------------------------------------------------------
# The dial is a picker row whose five stops are five different models. A typo in
# arrow_models would silently fall through to the fallback and look deliberate,
# so every stop must name a real catalog entry.
import json as _json
_cat = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")))
assert r.ARROW_ORDER, "no effort levels configured"
for lvl in r.ARROW_ORDER:
    tgt = r.ARROW_MAP.get(lvl)
    assert tgt, "effort level %r has no model" % lvl
    assert tgt in r.BY_ID, "effort %s -> %r is not in the catalog" % (lvl, tgt)

# Every stop must stay inside the folder policy, and in Vanguard all five
# collapse onto Claude rather than refusing the turn.
for pol in ({"data": "any"}, {"data": "zdr"}, {"data": "claude"}):
    for lvl in r.ARROW_ORDER:
        got = r.resolve_arrow({"output_config": {"effort": lvl}}, pol)
        assert r.allowed(got, pol), "effort %s under %s -> %s" % (lvl, pol["data"], got)
assert all(r.resolve_arrow({"output_config": {"effort": l}}, {"data": "claude"})
           == r.FALLBACK for l in r.ARROW_ORDER), "claude-only must collapse to the fallback"

# The row has to be in the lineup AND carry behavesAs, or Claude Code will not
# offer it: it is an id this version does not know.
dial = [row for row in _cat["picker_lineup"] if row["model"] == r.ARROW_MODEL]
assert len(dial) == 1, "the effort dial is not in picker_lineup"
assert dial[0].get("behavesAs"), "the dial needs behavesAs or it is never offered"
assert len(_cat["picker_lineup"]) <= 10, \
    "%d rows — over ten collapses behind '... +N models'" % len(_cat["picker_lineup"])

print("ok — %d models, policy rail holds" % len(r.CANDIDATES))
