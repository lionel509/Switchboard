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

# --- family tier preference -------------------------------------------------
fams = {m.get("family") for m in r.CANDIDATES}
if "glm" in fams:
    tiers = {m.get("tier") for m in r.family_of("glm")}
    if "flash" in tiers:
        got = r.resolve_family({"messages": []}, "glm", "flash")
        assert (r.BY_ID[got].get("tier")) == "flash", got
    if "pro" in tiers:
        got = r.resolve_family({"messages": []}, "glm", "pro")
        assert (r.BY_ID[got].get("tier")) == "pro", got

print("ok — %d models, policy rail holds" % len(r.CANDIDATES))
