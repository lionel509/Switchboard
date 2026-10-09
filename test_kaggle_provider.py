"""Kaggle Model Proxy provider guardrails and token minting (#15). Run: python3 -m pytest test_kaggle_provider.py"""
import importlib.util, os, sys, time, datetime

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
H = os.path.expanduser("~")
KAGGLE = [m["id"] for m in r.CANDIDATES if m["id"].startswith("kaggle/")]


def test_private_folders_refuse_every_kaggle_model(monkeypatch):
    monkeypatch.setattr(r, "POLICY", {"_default": {"data": "any"},
                                      "~/Documents/State Street": {"data": "zdr"},
                                      "~/Documents/Private": {"data": "claude"}})
    assert KAGGLE
    for folder in ("State Street", "Private"):
        pol = r.policy_for(H + "/Documents/" + folder)
        assert not any(r.allowed(m, pol) for m in KAGGLE), folder


def test_never_a_failover_target_and_never_auto():
    assert not any(f.startswith("kaggle/") for m in r.CANDIDATES for f in r.failover_chain(m["id"]))
    assert r.PROVIDERS["kaggle"].get("auto") is False
    seen = {}
    r.ask_router_model = lambda task, cands: seen.setdefault("c", [m["id"] for m in cands]) and None
    r.resolve_auto({"messages": [{"role": "user", "content": "x" * 10}]}, {})
    assert seen["c"] and not any(c.startswith("kaggle/") for c in seen["c"])


def test_minted_token_mints_when_missing_and_reuses_while_fresh(tmp_path, monkeypatch):
    f = tmp_path / "kmp.env"
    exp = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    calls = tmp_path / "calls"
    script = ("import sys,pathlib; pathlib.Path(%r).open('a').write('x');"
              "open(sys.argv[1],'w').write('MODEL_PROXY_URL=https://h/models\\nMODEL_PROXY_API_KEY=K1\\nMODEL_PROXY_EXPIRY_TIME=%s\\n')"
              % (str(calls), exp))
    prov = {"token_env": {"file": str(f), "url_var": "MODEL_PROXY_URL", "key_var": "MODEL_PROXY_API_KEY",
                          "expiry_var": "MODEL_PROXY_EXPIRY_TIME", "mint": [sys.executable, "-c", script, "{file}"]}}
    assert r.minted_token("t", prov) == ("https://h/models", "K1")
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    assert r.minted_token("t", prov) == ("https://h/models", "K1")
    assert calls.read_text() == "x"                      # second call reused the fresh token


def test_minted_token_renews_a_nearly_spent_one_and_fails_closed(tmp_path):
    f = tmp_path / "kmp.env"
    soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)).isoformat()
    f.write_text("MODEL_PROXY_URL=https://h/models\nMODEL_PROXY_API_KEY=OLD\nMODEL_PROXY_EXPIRY_TIME=%s\n" % soon)
    prov = {"token_env": {"file": str(f), "url_var": "MODEL_PROXY_URL", "key_var": "MODEL_PROXY_API_KEY",
                          "expiry_var": "MODEL_PROXY_EXPIRY_TIME", "mint": [sys.executable, "-c", "raise SystemExit(3)"]}}
    assert r.minted_token("t2", prov) is None           # a failed mint never hands back the spent key
