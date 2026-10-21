"""Breaker tests for PR #80 polish commit 36ab1b2 (claude-folder gate)."""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def _no_network(monkeypatch):
    calls = []
    def fake(path, body, key, headers=None):
        calls.append(path)
        return None
    monkeypatch.setattr(r, "openrouter_post", fake, raising=False)
    return calls


def test_falsy_non_string_data_fails_closed():
    # Docstring: "A non-string ... fails closed as claude." But data_level tests
    # `not v` first, so a hand-edited 0 / false / [] reads as "any" and may send.
    assert r.may_send_task({"data": 0}) is False


def test_claude_with_trailing_space_never_sends_task():
    # A typo'd "claude " is not "claude", so data_level says zdr and the task
    # text goes to the OpenRouter decision call under a folder that said claude.
    assert r.may_send_task({"data": "claude "}) is False


def test_claude_hyphen_variant_never_sends_task():
    assert r.may_send_task({"data": "claude-only"}) is False


def test_auto_claude_folder_makes_no_router_call(monkeypatch):
    calls = _no_network(monkeypatch)
    req = {"model": "auto", "messages": [{"role": "user", "content": "secret plan"}]}
    chosen = r.resolve_auto(req, {"data": "claude"})
    assert calls == []
    assert r.allowed(chosen, {"data": "claude"})


def test_family_stale_pin_not_rechecked_under_claude_folder(monkeypatch):
    # resolve_family returns a pinned id without checking allowed(), so a pin set
    # by an earlier "any" turn is served under a claude folder.
    fam = "breakerfam"
    cands = [
        {"id": "claude-sonnet-5", "family": fam, "tier": "pro", "price": [3, 15]},
        {"id": "openrouter/cheap-x", "family": fam, "tier": "lite", "price": [0.1, 0.2]},
    ]
    monkeypatch.setattr(r, "CANDIDATES", cands + list(r.CANDIDATES))
    monkeypatch.setattr(r, "BY_ID", {m["id"]: m for m in r.CANDIDATES})
    req = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
    key = r.conv_key(req) + ":" + fam
    monkeypatch.setitem(r._auto_pins, key, "openrouter/cheap-x")
    chosen = r.resolve_family(req, fam, None, {"data": "claude"})
    assert chosen != "openrouter/cheap-x", chosen
