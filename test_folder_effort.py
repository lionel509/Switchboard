#!/usr/bin/env python3
"""Per-folder effort: a folder's policy pins the effort level on the wire.

Run: python3 test_folder_effort.py     (silent means pass)
"""
import importlib.util, json, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def eff(req):
    return (req.get("output_config") or {}).get("effort")


# A Claude request that already carries an effort gets the folder's level.
req = {"model": "claude-opus-5-5", "output_config": {"effort": "medium"}}
r.pin_effort(req, "claude-opus-5-5", {"effort": "max"})
assert eff(req) == "max", req

# Other output_config fields survive the rewrite.
req = {"model": "claude-opus-5-5", "output_config": {"effort": "low", "format": {"x": 1}}}
r.pin_effort(req, "claude-opus-5-5", {"effort": "xhigh"})
assert req["output_config"] == {"effort": "xhigh", "format": {"x": 1}}, req

# A Claude request with no effort is left alone: a model that takes none
# (Haiku-class) would 400 on an injected one.
req = {"model": "claude-haiku-4-5-20251001", "max_tokens": 100}
r.pin_effort(req, "claude-haiku-4-5-20251001", {"effort": "max"})
assert "output_config" not in req, req

# A gateway model gets the level even with none sent; sanitize() turns it
# into a thinking budget, so the pin must reach the budget too.
req = {"model": "kimi/k2.8", "max_tokens": 64000}
r.pin_effort(req, "kimi/k2.8", {"effort": "high"})
assert eff(req) == "high", req
req = {"model": "x-ai/grok-4.7", "max_tokens": 64000, "output_config": {"effort": "low"}}
r.pin_effort(req, "x-ai/grok-4.7", {"effort": "max"})
out = json.loads(r.sanitize(json.dumps(req).encode()))
assert out["thinking"]["budget_tokens"] == r.EFFORT_BUDGETS["max"], out

# No pin, or an unknown level, changes nothing -- the slider rules.
for pol in ({}, {"effort": None}, {"effort": "turbo"}, {"effort": "slider"}):
    req = {"model": "claude-opus-5-5", "output_config": {"effort": "low"}}
    r.pin_effort(req, "claude-opus-5-5", pol)
    assert eff(req) == "low", (pol, req)

# Negative control: the same Claude request DOES change when pinned, so the
# no-op cases above can't pass vacuously.
req = {"model": "claude-opus-5-5", "output_config": {"effort": "low"}}
r.pin_effort(req, "claude-opus-5-5", {"effort": "medium"})
assert eff(req) == "medium"

# The level is inherited by longest prefix like data/tier.
H = os.path.expanduser("~")
r.POLICY = {"_default": {"data": "any", "effort": "medium"},
            "~/Documents/Private": {"data": "claude", "effort": "max"}}
assert r.policy_for(H + "/Documents/Private/notes")["effort"] == "max"
assert r.policy_for(H + "/Documents/Citadel")["effort"] == "medium"
r.POLICY["~/Documents/Citadel"] = {"effort": "slider"}
assert r.policy_for(H + "/Documents/Citadel/x")["effort"] == "slider", "a folder can opt out of _default's pin"
