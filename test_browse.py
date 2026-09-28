"""Model browser filter (#8). Run: python3 -m pytest test_browse.py"""
import importlib.util, os

spec = importlib.util.spec_from_file_location(
    "_sb", os.path.join(os.path.dirname(os.path.abspath(__file__)), "switchboard.py"))
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)

# Shaped like OpenRouter's GET /api/v1/models "data" rows: prices are USD per token, as strings.
OR = [
    {"id": "google/gemma-4-31b-it", "name": "Google: Gemma 4 31B", "context_length": 262144,
     "pricing": {"prompt": "0.00000009", "completion": "0.00000034"}},
    {"id": "google/gemini-pro-latest", "name": "Google: Gemini Pro", "context_length": 1048576,
     "pricing": {"prompt": "0.000002", "completion": "0.000012"}},
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek: V4 Flash", "context_length": 131072,
     "pricing": {"prompt": "0.00000005", "completion": "0.00000016"}},
    {"id": "openrouter/auto", "name": "Auto Router", "context_length": 2000000,
     "pricing": {"prompt": "-1", "completion": "-1"}},
    {"id": "qwen/qwen3-coder:free", "name": "Qwen: Qwen3 Coder (free)", "context_length": 262144,
     "pricing": {"prompt": "0", "completion": "0"}},
]


def ids(rows):
    return [row["id"] for row in rows]


def test_empty_query_lists_all_cheapest_first_variable_price_last():
    assert ids(sb.browse_rows(OR, "", set())) == [
        "qwen/qwen3-coder:free", "deepseek/deepseek-v4-flash", "google/gemma-4-31b-it",
        "google/gemini-pro-latest", "openrouter/auto"]


def test_every_term_must_match_id_or_name_case_insensitively():
    assert ids(sb.browse_rows(OR, "GOOGLE gemma", set())) == ["google/gemma-4-31b-it"]
    assert ids(sb.browse_rows(OR, "google", set())) == ["google/gemma-4-31b-it", "google/gemini-pro-latest"]
    assert ids(sb.browse_rows(OR, "V4 flash", set())) == ["deepseek/deepseek-v4-flash"]
    assert sb.browse_rows(OR, "google nothing", set()) == []


def test_row_carries_price_per_million_context_and_added_flag():
    row = sb.browse_rows(OR, "gemma", {"google/gemma-4-31b-it"})[0]
    assert row["name"] == "Google: Gemma 4 31B"
    assert abs(row["price"][0] - 0.09) < 1e-9 and abs(row["price"][1] - 0.34) < 1e-9
    assert row["context"] == 262144
    assert row["added"] is True
    assert sb.browse_rows(OR, "gemini", {"google/gemma-4-31b-it"})[0]["added"] is False


def test_variable_price_is_none():
    assert sb.browse_rows(OR, "auto", set())[0]["price"] is None
