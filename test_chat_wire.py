"""Anthropic Messages <-> OpenAI chat completions, for wire:openai providers (#15).
Run: python3 -m pytest test_chat_wire.py"""
import importlib.util, json, os

spec = importlib.util.spec_from_file_location(
    "_r", os.path.join(os.path.dirname(os.path.abspath(__file__)), "router.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

REQ = {
    "model": "kaggle/google/gemini-3-flash-preview", "max_tokens": 512, "temperature": 0.7,
    "stream": True, "stop_sequences": ["END"],
    "system": [{"type": "text", "text": "You are terse."}, {"type": "text", "text": "Use tools."}],
    "tools": [{"name": "Read", "description": "Read a file",
               "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
    "tool_choice": {"type": "auto"},
    "messages": [
        {"role": "user", "content": "read a.txt"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "x"},
            {"type": "text", "text": "Reading."},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "42"}]},
            {"type": "text", "text": "what is it?"}]},
    ],
}


def test_to_chat_request_shape():
    out = r.to_chat(REQ, "google/gemini-3-flash-preview")
    assert out["model"] == "google/gemini-3-flash-preview"
    assert out["max_tokens"] == 512 and out["stop"] == ["END"]
    assert "temperature" not in out and not out.get("stream")      # proxy rejects temperature; never streams
    assert out["tools"] == [{"type": "function", "function": {
        "name": "Read", "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
    assert out["tool_choice"] == "auto"


def test_to_chat_messages():
    m = r.to_chat(REQ, "w")["messages"]
    assert m[0] == {"role": "system", "content": "You are terse.\nUse tools."}
    assert m[1] == {"role": "user", "content": "read a.txt"}
    assert m[2]["role"] == "assistant" and m[2]["content"] == "Reading."          # thinking dropped
    assert m[2]["tool_calls"] == [{"id": "toolu_1", "type": "function",
                                   "function": {"name": "Read", "arguments": json.dumps({"path": "a.txt"})}}]
    assert m[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "42"}   # tool results first
    assert m[4] == {"role": "user", "content": "what is it?"}
    assert len(m) == 5


def test_to_chat_tool_choice_variants():
    for given, want in (({"type": "any"}, "required"), ({"type": "none"}, "none"),
                        ({"type": "tool", "name": "Read"}, {"type": "function", "function": {"name": "Read"}})):
        assert r.to_chat(dict(REQ, tool_choice=given), "w")["tool_choice"] == want
    assert "tool_choice" not in r.to_chat({k: v for k, v in REQ.items() if k != "tool_choice"}, "w")


def test_from_chat_text_and_tool_calls():
    resp = {"id": "gen-1", "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Let me look.",
        "tool_calls": [{"id": "call_9", "type": "function",
                        "function": {"name": "Read", "arguments": "{\"path\": \"b.txt\"}"}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 7}}
    msg = r.from_chat(resp, "kaggle/google/gemini-3-flash-preview")
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["model"] == "kaggle/google/gemini-3-flash-preview"
    assert msg["content"] == [{"type": "text", "text": "Let me look."},
                              {"type": "tool_use", "id": "call_9", "name": "Read", "input": {"path": "b.txt"}}]
    assert msg["stop_reason"] == "tool_use" and msg["stop_sequence"] is None
    assert msg["usage"] == {"input_tokens": 100, "output_tokens": 7}


def test_from_chat_stop_reasons_and_empty_text():
    def one(fr, content="hi"):
        return r.from_chat({"choices": [{"finish_reason": fr, "message": {"content": content}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}, "m")
    assert one("stop")["stop_reason"] == "end_turn"
    assert one("length")["stop_reason"] == "max_tokens"
    assert one("stop", content=None)["content"] == []
    assert one("stop")["id"].startswith("msg_")


def events(raw):
    out = []
    for chunk in raw.decode().strip().split("\n\n"):
        ev, data = chunk.split("\n", 1)
        assert ev.startswith("event: ") and data.startswith("data: ")
        d = json.loads(data[len("data: "):])
        assert d["type"] == ev[len("event: "):]
        out.append(d)
    return out


def test_message_sse_replays_the_message():
    msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
           "content": [{"type": "text", "text": "Hi"},
                       {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "c"}}],
           "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 3}}
    ev = events(r.message_sse(msg))
    assert [e["type"] for e in ev] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_stop",
        "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"]
    assert ev[0]["message"]["content"] == [] and ev[0]["message"]["id"] == "msg_1"
    assert ev[1] == {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    assert ev[2]["delta"] == {"type": "text_delta", "text": "Hi"} and ev[2]["index"] == 0
    assert ev[4]["content_block"] == {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
    assert ev[5]["delta"]["type"] == "input_json_delta" and json.loads(ev[5]["delta"]["partial_json"]) == {"path": "c"}
    assert ev[7]["delta"] == {"stop_reason": "tool_use", "stop_sequence": None}
    assert ev[7]["usage"]["output_tokens"] == 3
