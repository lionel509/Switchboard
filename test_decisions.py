"""Auto's decision step: an OpenRouter decision model (System One API) asked first (#79).

Run: python3 -m pytest -q test_decisions.py
Nothing here touches the network: openrouter_post is replaced by a fake.
"""
import importlib.util, json, os, random

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

CANDS = [
    {"id": "a/b", "name": "Bee", "price": [0.1, 0.2], "good_at": "x"},
    {"id": "~c/d-latest", "name": "Dee", "billing": "subscription", "good_at": "y"},
    {"id": "openrouter/auto", "price": [-1, -1]},
]


def decision_reply(choice, confidence=0.91, cost=0.0000378):
    return {"model": "typesafe/jev-1.14-20261001",
            "answers": {"model": {"type": "choice", "choice": choice,
                                  "confidence": confidence,
                                  "probabilities": {choice: confidence}}},
            "usage": {"input_tokens": 900, "output_tokens": 0, "cost": cost}}


def chat_reply(text):
    return {"content": [{"type": "text", "text": text}]}


class Net:
    """Fake openrouter_post: one queue of canned replies per path, recording calls."""

    def __init__(self, decisions=(), chats=()):
        self.decisions = list(decisions)
        self.chats = list(chats)
        self.calls = []

    def __call__(self, path, body, key, headers=None):
        self.calls.append((path, body, headers))
        q = self.decisions if path == r.DECISION_PATH else self.chats
        return q.pop(0) if q else None

    def paths(self):
        return [p for p, _, _ in self.calls]


def setup_env(monkeypatch, tmp_path, decision="typesafe/jev-1.14", router="x/y", net=None):
    monkeypatch.setattr(r, "LOGFILE", str(tmp_path / "log"))
    monkeypatch.setattr(r, "or_key", lambda model=None: "k")
    monkeypatch.setattr(r, "DECISION_MODEL", decision)
    monkeypatch.setattr(r, "ROUTER_MODEL", router)
    net = net or Net()
    monkeypatch.setattr(r, "openrouter_post", net)
    return net


def read_log(tmp_path):
    p = tmp_path / "log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_decision_choice_wins_and_is_shaped_right(monkeypatch, tmp_path):
    net = setup_env(monkeypatch, tmp_path, net=Net(decisions=[decision_reply("a/b")]))
    assert r.ask_router_model("rename a var", CANDS) == "a/b"
    assert len(net.calls) == 1
    path, body, _ = net.calls[0]
    assert path == "/api/alpha/decisions"
    assert body["model"] == "typesafe/jev-1.14"
    assert body["state"] == "rename a var"
    assert body["provider"]["zdr"] is True
    allowed = {"zdr", "sort", "order", "only", "ignore", "allow_fallbacks",
               "data_collection", "require_parameters", "quantizations", "max_price"}
    assert set(body["provider"]) <= allowed
    assert set(body["questions"]["model"]["criteria"]) == {"a/b", "~c/d-latest", "openrouter/auto", "none"}
    assert body["questions"]["model"]["type"] == "choice"


def test_none_or_unknown_choice_falls_through_to_chat_router(monkeypatch, tmp_path):
    for choice in ("none", "zz/not-listed"):
        net = setup_env(monkeypatch, tmp_path, router="x/y",
                        net=Net(decisions=[decision_reply(choice)],
                                chats=[chat_reply("~c/d-latest")]))
        assert r.ask_router_model("t", CANDS) == "~c/d-latest"
        assert net.paths() == ["/api/alpha/decisions", "/api/v1/messages"]
        assert net.calls[1][2] == {"anthropic-version": "2023-06-01"}


def test_none_with_no_chat_router_returns_none(monkeypatch, tmp_path):
    net = setup_env(monkeypatch, tmp_path, router="",
                    net=Net(decisions=[decision_reply("none")]))
    assert r.ask_router_model("t", CANDS) is None
    assert net.paths() == ["/api/alpha/decisions"]


def test_tilde_dropped_choice_still_maps_back(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path, net=Net(decisions=[decision_reply("c/d-latest")]))
    assert r.ask_router_model("t", CANDS) == "~c/d-latest"


def test_decision_failure_falls_through(monkeypatch, tmp_path):
    net = setup_env(monkeypatch, tmp_path, net=Net(
        decisions=[None, {"error": {"code": 429, "message": "rate"}}, {"answers": {}}],
        chats=[chat_reply("a/b"), chat_reply("a/b"), chat_reply("a/b")]))
    for _ in range(3):
        assert r.ask_router_model("t", CANDS) == "a/b"
    assert net.paths() == ["/api/alpha/decisions", "/api/v1/messages"] * 3
    rows = read_log(tmp_path)
    errs = [x for x in rows if x.get("kind") == "auto_decision" and x.get("status") == 429]
    assert len(errs) == 1


def test_decision_is_a_ledger_row(monkeypatch, tmp_path):
    setup_env(monkeypatch, tmp_path, net=Net(decisions=[decision_reply("a/b")]))
    assert r.ask_router_model("t", CANDS) == "a/b"
    rows = read_log(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["upstream"] == "openrouter"
    assert row["model_requested"] == "typesafe/jev-1.14"
    assert row["model_used"] == "typesafe/jev-1.14-20261001"
    assert row["status"] == 200
    assert row["kind"] == "auto_decision"
    assert row["cost"] == 0.0000378
    assert row["in"] == 900
    assert row["out"] == 0
    assert row["choice"] == "a/b"
    assert row["confidence"] == 0.91


def test_off_when_unset(monkeypatch, tmp_path):
    net = setup_env(monkeypatch, tmp_path, decision="", router="x/y",
                    net=Net(chats=[chat_reply("a/b")]))
    assert r.ask_router_model("t", CANDS) == "a/b"
    assert net.paths() == ["/api/v1/messages"]


def test_no_call_without_candidates_or_task(monkeypatch, tmp_path):
    net = setup_env(monkeypatch, tmp_path, net=Net(decisions=[decision_reply("a/b")]))
    assert r.ask_router_model("", CANDS) is None
    assert r.ask_router_model("x", []) is None
    assert net.calls == []


def test_invariant_result_is_always_in_cands(monkeypatch, tmp_path):
    rng = random.Random(79)
    ids = [m["id"] for m in r.CANDIDATES]
    for _ in range(300):
        subset = rng.sample(r.CANDIDATES, rng.randint(1, min(8, len(r.CANDIDATES))))
        in_ids = [m["id"] for m in subset]
        out_ids = [i for i in ids if i not in in_ids] or ["zz/none-of-these"]
        inside = rng.choice(in_ids)
        outside = rng.choice(out_ids)
        reply = rng.choice([
            inside, outside, inside.replace("~", ""), inside.split("/")[-1],
            "none", "", "missing", "error", "nondict", "chat-outside",
        ])
        if reply == "missing":
            dec = {"usage": {}}
        elif reply == "error":
            dec = {"error": {"code": 500, "message": "x"}}
        elif reply == "nondict":
            dec = None
        elif reply == "chat-outside":
            dec = {"answers": {}}
        else:
            dec = decision_reply(reply)
        chat_text = rng.choice([outside, "nothing useful", ""])
        net = setup_env(monkeypatch, tmp_path, net=Net(decisions=[dec], chats=[chat_reply(chat_text)]))
        got = r.ask_router_model("t", subset)
        assert got in ({None} | set(in_ids)), (reply, got, in_ids)
