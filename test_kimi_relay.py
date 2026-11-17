"""/kimi/ OpenAI-wire pass-through (#105). Run: python3 -m pytest test_kimi_relay.py"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def test_kimi_path_maps_to_the_coding_endpoint():
    assert r.relay_route("/kimi/v1/chat/completions") == ("kimi", "/v1/chat/completions")


def test_gemma_paths_still_parse_as_before():
    assert r.relay_route("/gemma/or/v1/chat/completions") == ("or", "/v1/chat/completions")
    assert r.relay_route("/gemma/local/v1/chat/completions") == ("local", "/v1/chat/completions")
