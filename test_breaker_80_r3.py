"""Breaker round 3 must-fix tests for PR #80 (claude-folder gate).

Run: python3 -m pytest -q test_breaker_80_r3.py
Nothing here touches the network: openrouter_post is replaced by a fake and
log_request is captured in memory. Non-ASCII inputs are written as escapes so
the lookalike characters survive copy-paste.
"""
import importlib.util, json, os, unicodedata

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

import pytest


def answer(choice, conf=0.9, cost=0.0001):
    return {"model": "typesafe/jev-1.14", "answers": {"model": {
                "type": "choice", "choice": choice, "confidence": conf}},
            "usage": {"input_tokens": 10, "output_tokens": 0, "cost": cost}}


class Fake:
    def __init__(self):
        self.decision = lambda model, body: None
        self.calls = []

    def __call__(self, path, body, key, headers=None):
        self.calls.append((path, body.get("model") if isinstance(body, dict) else None))
        if path == r.DECISION_PATH:
            return self.decision(body["model"], body)
        return None

    def decision_calls(self):
        return [m for p, m in self.calls if p == r.DECISION_PATH]


@pytest.fixture
def env(monkeypatch):
    rows = []
    monkeypatch.setattr(r, "log_request", lambda rec: rows.append(rec))
    monkeypatch.setattr(r, "or_key", lambda model=None: "test-key")
    monkeypatch.setattr(r, "_auto_pins", {})
    fake = Fake()
    monkeypatch.setattr(r, "openrouter_post", fake)
    return {"rows": rows, "fake": fake, "mp": monkeypatch}


# --- must-fix 1: NFD cwd must still find the NFC claude row ---------------

def test_break_policy_for_nfd_cwd_misses_claude_folder(env):
    home = os.path.expanduser("~")
    key = "~/Documents/Privée"                  # NFC in the policy key
    cwd = home + unicodedata.normalize("NFD", "/Documents/Privée")
    assert cwd != home + "/Documents/Privée"    # really NFD, not NFC
    env["mp"].setattr(r, "POLICY", {"_default": {"data": "any", "tier": "pro"},
                                    key: {"data": "claude"}})
    pol = r.policy_for(cwd)
    assert r.data_level(pol.get("data")) == "claude"


# --- must-fix 2: JSON-escaped non-ASCII cwd must not be cut short ---------

def test_break_cwd_json_escaped_unicode_misses_claude_folder(env):
    path = "/Users/x/Documents/Privée"
    env["mp"].setattr(r, "POLICY", {"_default": {"data": "any", "tier": "pro"},
                                    path: {"data": "claude"}})
    body = json.dumps({"system": "Primary working directory: " + path}).encode()
    pol = r.policy_for(r.cwd_of(body))
    assert r.data_level(pol.get("data")) == "claude"


def test_ascii_cwd_followed_by_git_line_in_json_body_is_exact(env):
    body = json.dumps({"system": "Primary working directory: /Users/x/Documents/Private\n"
                                 "Is a git repository: true"}).encode()
    assert r.cwd_of(body) == "/Users/x/Documents/Private"


# --- must-fix 3: invisible and fullwidth lookalikes of "claude" fail closed -

def test_break_fullwidth_claude_is_not_fail_closed(env):
    assert r.may_send_task({"data": "ｃｌａｕｄｅ"}) is False


def test_break_zero_width_space_before_claude_is_not_fail_closed(env):
    assert r.may_send_task({"data": "​claude"}) is False


def test_break_bom_before_claude_is_not_fail_closed(env):
    assert r.may_send_task({"data": "﻿claude"}) is False


# --- must-fix 4: dispatch must read falsy claude data as claude ----------

def test_break_dispatch_tier_allowed_treats_falsy_claude_data_as_any(env):
    import dispatch
    for bad in (0, False, []):
        assert dispatch.tier_allowed("antigravity", "gemini-x", {"data": bad}) is False, bad


# --- must-fix 5: a timed-out decision attempt still leaves a ledger row ---

def test_break_timeout_on_first_decision_model_leaves_no_ledger_row(env):
    cands = [{"id": "vendor/alpha", "name": "Alpha", "price": [1, 1]}]
    env["mp"].setattr(r, "DECISION_MODEL", ["m1", "m2"])
    env["mp"].setattr(r, "ROUTER_MODEL", "")
    replies = iter([None, answer("vendor/alpha")])
    env["fake"].decision = lambda model, body: next(replies)
    assert r.ask_router_model("task", cands) == "vendor/alpha"
    dec_rows = [x for x in env["rows"] if x.get("kind") == "auto_decision"]
    assert len(dec_rows) == 2, dec_rows
