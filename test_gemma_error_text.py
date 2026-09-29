"""/gemma/* error logging (#30). Run: python3 -m pytest test_gemma_error_text.py"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def test_reads_kaggle_openai_and_plain_error_bodies():
    assert r.gemma_error_text(b'{"code": "", "message": "Function call is missing a thought_signature"}') \
        == "Function call is missing a thought_signature"
    assert r.gemma_error_text(b'{"error": {"message": "rate limited", "code": 429}}') == "rate limited"
    assert r.gemma_error_text(b"upstream exploded") == "upstream exploded"
    assert len(r.gemma_error_text(b"x" * 1000)) == 300
