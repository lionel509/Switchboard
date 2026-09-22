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


# --- the effort slider -------------------------------------------------------
# The slider must change the CHOSEN model's effort, not swap the model. Claude
# Code sends the level as output_config.effort, which the PORTABLE allowlist
# drops -- so if sanitize stops reading it first, every level silently collapses
# onto one budget and the dial goes dead. That is the failure this catches.
import json as _json
_CAT = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")))

def _sent(effort, max_tokens=64000, model="xiaomi/mimo-v2.6-pro"):
    b = {"model": model, "max_tokens": max_tokens,
         "messages": [{"role": "user", "content": "hi"}]}
    if effort:
        b["output_config"] = {"effort": effort}
    return _json.loads(r.sanitize(_json.dumps(b).encode()))

assert r.EFFORT_BUDGETS, "no effort_budgets in the catalog"
budgets = [_sent(l)["thinking"]["budget_tokens"] for l in r.ARROW_ORDER]
assert budgets == sorted(budgets), "budgets must rise with effort: %s" % budgets
assert len(set(budgets)) == len(budgets), \
    "every level must differ, or the slider does nothing: %s" % budgets
for lvl in r.ARROW_ORDER:
    assert _sent(lvl)["thinking"]["budget_tokens"] == r.EFFORT_BUDGETS[lvl], lvl
    assert _sent(lvl).get("reasoning"), "OpenRouter's own lever must go too: %s" % lvl

# No level -> the flat default, exactly as before this existed.
assert _sent(None)["thinking"]["budget_tokens"] == min(r.THINKING_BUDGET, 64000 - 1024)
# output_config itself must never reach a non-Claude model: it 400s on it.
assert "output_config" not in _sent("max")

# Anthropic rejects a budget under 1024, so a small max_tokens must turn thinking
# OFF rather than send an invalid budget.
tiny = _sent("max", max_tokens=1200)
assert tiny["thinking"] == {"type": "disabled"}, tiny["thinking"]
# And a level must never exceed what max_tokens leaves room for.
clamped = _sent("max", max_tokens=8000)
assert clamped["thinking"]["budget_tokens"] <= 8000 - 1024, clamped

# Every gateway row needs behavesAs or the slider never appears on it. The
# catalog default supplies it at apply time, so assert the default exists.
assert _CAT.get("picker_behaves_as"), \
    "without picker_behaves_as no gateway row gets an effort slider"
assert len(_CAT["picker_lineup"]) <= 10, \
    "%d rows — over ten collapses behind '... +N models'" % len(_CAT["picker_lineup"])

print("ok — %d models, policy rail holds" % len(r.CANDIDATES))
