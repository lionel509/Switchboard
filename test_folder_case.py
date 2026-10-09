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
