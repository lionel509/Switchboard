#!/usr/bin/env python3
"""Folder policy matches the cwd case-insensitively, as macOS paths are (#40).

Run: python3 test_folder_case.py     (silent means pass)

`cd private` reaches ~/Documents/Private on APFS, and Claude Code may report the
cwd as typed. A case-sensitive match drops that session to _default: no effort
pin, and data "any", so failover could carry private content off Claude.
"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

H = os.path.expanduser("~")
r.POLICY = {"_default": {"data": "any", "tier": "pro", "effort": "medium"},
            "~/Documents/Private": {"data": "claude", "effort": "xhigh"},
            "~/Documents/State Street": {"data": "zdr"}}
THIRD_PARTY = "kimi/k2.8"

# Control: the row's own spelling matches, so the cases below can't pass vacuously.
pol = r.policy_for(H + "/Documents/Private")
assert pol["data"] == "claude" and pol["effort"] == "xhigh", pol

# The bug: lower, upper and mixed case, and a subfolder, all get the row.
for cwd in (H + "/Documents/private",
            H + "/Documents/PRIVATE",
            H + "/documents/pRiVaTe",
            H.lower() + "/documents/private/notes",
            H.upper() + "/DOCUMENTS/PRIVATE"):
    pol = r.policy_for(cwd)
    assert pol["data"] == "claude", (cwd, pol)            # the data filter
    assert not r.allowed(THIRD_PARTY, pol), cwd
    assert pol["effort"] == "xhigh", (cwd, pol)           # the effort pin
    req = {"model": "claude-opus-5-5", "output_config": {"effort": "low"}}
    r.pin_effort(req, "claude-opus-5-5", pol)
    assert req["output_config"]["effort"] == "xhigh", (cwd, req)

# A row with a space, in another case.
assert r.policy_for(H + "/documents/state street")["data"] == "zdr"

# Prefix collision: a sibling that only starts with the name must not match, in any case.
for cwd in (H + "/Documents/privatenotes", H + "/Documents/PrivateNotes",
            H + "/documents/PRIVATE-old", H + "/Documents/Private2"):
    pol = r.policy_for(cwd)
    assert pol["data"] == "any" and pol["effort"] == "medium", (cwd, pol)
    assert r.allowed(THIRD_PARTY, pol), cwd

# End to end from the wire: the cwd Claude Code reports, as typed.
BODY = ('{"model":"x","messages":[{"role":"user","content":"<system-reminder>\\n'
        '# Environment\\nYou have been invoked in the following environment: \\n'
        ' - Primary working directory: %s\\n - Is a git repository: true\\n'
        '</system-reminder>"}]}')
typed = (BODY % (H + "/Documents/private")).encode()
assert r.cwd_of(typed) == H + "/Documents/private", "cwd_of keeps the path as sent"
pol = r.policy_for(r.cwd_of(typed))
assert pol["data"] == "claude" and pol["effort"] == "xhigh", pol

# Rows that differ only in case fold to one root. `switchboard policy` and the TUI key
# a row by its typed spelling, so `policy ~/Documents/private --effort max` writes a
# twin beside Private. The twin must never loosen the vault, in either dict order.
BASE = {"_default": {"data": "any", "tier": "pro", "effort": "medium"},
        "~/Documents/Private": {"data": "claude", "effort": "xhigh"}}
for twin in ({"data": "zdr"}, {"effort": "max"}, {"data": "any"}):
    for twin_first in (False, True):
        rows = [("~/Documents/Private", BASE["~/Documents/Private"]),
                ("~/Documents/private", twin)]
        if twin_first:
            rows.reverse()
        r.POLICY = dict([("_default", BASE["_default"])] + rows)
        for cwd in (H + "/Documents/Private/notes", H + "/Documents/private"):
            pol = r.policy_for(cwd)
            assert pol["data"] == "claude", (twin, twin_first, cwd, pol)
            assert not r.allowed(THIRD_PARTY, pol), (twin, twin_first, cwd)

# Longest prefix still wins, whatever the dict order: a looser subfolder row applies
# inside it, and only inside it.
for sub_first in (False, True):
    rows = [("~/Documents/Private", {"data": "claude"}),
            ("~/Documents/Private/Public", {"data": "any"})]
    if sub_first:
        rows.reverse()
    r.POLICY = dict([("_default", {"data": "any"})] + rows)
    assert r.policy_for(H + "/Documents/private/public/x")["data"] == "any", sub_first
    assert r.policy_for(H + "/Documents/Private/x")["data"] == "claude", sub_first

# No cwd at all (None) is "no cwd": the default, not a crash.
r.POLICY = {"_default": {"data": "any", "tier": "pro"},
            "~/Documents/Private": {"data": "claude"}}
assert r.policy_for(None) == {"data": "any", "tier": "pro"}
assert r.policy_for("") == {"data": "any", "tier": "pro"}

# Review 2: twins are merged by what each row means, read the way allowed() reads
# it, and JSON key order never changes the result. Oracle: the merged policy allows
# a model exactly when every twin alone (over the default) allows it.
MODELS = sorted(set(r.BY_ID) | {THIRD_PARTY, "claude-opus-5-5"})
# Controls: the catalog has models that tell the three levels apart, so the oracle
# below can't pass vacuously.
assert any(r.allowed(m, {"data": "zdr"}) and not r.allowed(m, {"data": "claude"}) for m in MODELS)
assert any(r.allowed(m, {"data": "any"}) and not r.allowed(m, {"data": "zdr"}) for m in MODELS)
# allowed() reads data lowercased; a value that is not claude/zdr/any reads as claude (#49).
for v, means in (("Claude", "claude"), ("ZDR", "zdr"), ("clade", "claude"), ("", "any"), (None, "any")):
    assert ([r.allowed(m, {"data": v}) for m in MODELS]
            == [r.allowed(m, {"data": means}) for m in MODELS]), v


def twins(default, a, b):
    got = []
    for rows in ([("~/Documents/Private", a), ("~/Documents/private", b)],
                 [("~/Documents/private", b), ("~/Documents/Private", a)]):
        r.POLICY = dict([("_default", default)] + rows)
        pol = r.policy_for(H + "/Documents/Private/notes")
        got.append(pol)
        for m in MODELS:
            alone = all(r.allowed(m, dict(default, **row)) for _, row in rows)
            assert r.allowed(m, pol) == alone, (default, a, b, m, pol)
    assert got[0] == got[1], ("JSON key order changed the policy", got)


ANY = {"data": "any", "tier": "pro", "effort": "medium"}
twins({"data": "claude"}, {"effort": "xhigh"}, {"data": "zdr"})   # no data key inherits claude
twins({"data": "claude"}, {"data": None}, {"effort": "max"})
twins(ANY, {"data": "Claude"}, {"data": "zdr"})                     # allowed() lowercases
for u in ("ZDR", "Zdr", "CLAUDE", "clade", "zdr ", " claude"):      # unknown means claude
    twins(ANY, {"data": u}, {"data": "any"})
twins(ANY, {"data": "claude", "effort": "max"}, {"data": "claude", "effort": "low"})

# A hand-edited non-string data fails closed, in policy_for and in allowed().
r.POLICY = {"_default": ANY, "~/Documents/Private": {"data": ["claude"]},
            "~/Documents/private": {"data": "any"}}
pol = r.policy_for(H + "/Documents/Private/x")
assert not r.allowed(THIRD_PARTY, pol), pol
