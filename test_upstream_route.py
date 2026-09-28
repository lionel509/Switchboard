"""Per-model OpenRouter key (#6). Run: python3 -m pytest test_upstream_route.py"""
import importlib.util, os, tempfile

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def keyfile(text):
    fd, path = tempfile.mkstemp()
    os.write(fd, text.encode()); os.close(fd)
    return path


def setup(monkeypatch, gemma_key_file):
    main, nonzdr = keyfile("MAIN"), keyfile("NONZDR")
    monkeypatch.setattr(r, "KEYFILE", main)
    monkeypatch.setattr(r, "NONZDR_KEYFILE", nonzdr)
    monkeypatch.setitem(r.CATALOG, "upstreams", [
        {"name": "openrouter", "key_file": main},
        {"name": "openrouter-gemma", "key_file": gemma_key_file},
    ])
    monkeypatch.setattr(r, "BY_ID", {
        "plain/model": {"id": "plain/model"},
        "loose/model": {"id": "loose/model", "zdr": False},
        "google/gemma-4-31b-it": {"id": "google/gemma-4-31b-it", "upstream": "openrouter-gemma"},
        "typo/model": {"id": "typo/model", "upstream": "no-such-upstream"},
    })


def test_model_with_upstream_uses_that_key(monkeypatch):
    setup(monkeypatch, keyfile("GEMMA"))
    assert r.or_key("google/gemma-4-31b-it") == "GEMMA"


def test_missing_upstream_key_never_falls_back_to_main(monkeypatch):
    setup(monkeypatch, "/nonexistent/openrouter-key-gemma")
    assert r.or_key("google/gemma-4-31b-it") is None


def test_unknown_upstream_name_never_falls_back_to_main(monkeypatch):
    setup(monkeypatch, keyfile("GEMMA"))
    assert r.or_key("typo/model") is None


def test_models_without_upstream_are_unchanged(monkeypatch):
    setup(monkeypatch, keyfile("GEMMA"))
    assert r.or_key("plain/model") == "MAIN"
    assert r.or_key("loose/model") == "NONZDR"
    assert r.or_key() == "MAIN"
