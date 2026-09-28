"""/gemma/kaggle stand-in route (#28). Run: python3 -m pytest test_gemma_kaggle.py"""
import importlib.util, os, pytest

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def req(chars, max_tokens=4096, **kw):
    return dict({"model": "gemma-4-31b-it-qat-w4a16-ct", "max_tokens": max_tokens, "temperature": 1.0,
                 "messages": [{"role": "user", "content": "x" * chars}], "tools": [{"type": "function"}]}, **kw)


def test_renames_to_flash_lite_and_drops_temperature():
    out = r.gemma_kaggle_body(req(1000))
    assert out["model"] == "google/gemini-3.1-flash-lite-preview"
    assert "temperature" not in out and out["tools"] == [{"type": "function"}]


def test_a_request_over_gemmas_32k_context_is_refused_like_vllm():
    assert r.gemma_kaggle_body(req(4 * 20000, max_tokens=4096))          # ~24K: fits
    with pytest.raises(ValueError, match="32768"):
        r.gemma_kaggle_body(req(4 * 30000, max_tokens=4096))             # ~34K with output: doesn't
