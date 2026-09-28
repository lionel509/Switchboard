"""/gemma/or request shaping (#23). Run: python3 -m pytest test_gemma_or_body.py"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def test_gemma_or_body_renames_the_model_and_pins_reka_with_no_fallback():
    req = {"model": "gemma-4-31b-it-qat-w4a16-ct", "messages": [{"role": "user", "content": "x"}], "tools": []}
    out = r.gemma_or_body(req)
    assert out["model"] == "google/gemma-4-31b-it"
    assert out["provider"] == {"only": ["reka"], "allow_fallbacks": False, "zdr": True}
    assert out["messages"] == req["messages"] and out["tools"] == []
    assert req["model"] == "gemma-4-31b-it-qat-w4a16-ct"          # caller's dict untouched
