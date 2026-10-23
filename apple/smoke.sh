#!/bin/sh
# Smoke test for the Apple shim against this Mac's real models (#98). Exits non-zero on the first failure.
# Run: apple/smoke.sh   (PORT=18788 by default, so a running shim on 8788 is left alone)
set -u
cd "$(dirname "$0")"
PORT="${PORT:-18788}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/apple-smoke.XXXXXX")"
BASE="http://127.0.0.1:$PORT"
SHIM=""
cleanup() {
  if [ -n "$SHIM" ]; then kill "$SHIM" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*"; exit 1; }
ok()   { echo "ok:   $*"; }

APPLE_SHIM_PORT="$PORT" ./run.sh >"$TMP/shim.log" 2>&1 &
SHIM=$!
i=0
until curl -s -m 2 "$BASE/health" >"$TMP/health.json" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -gt 120 ] && { cat "$TMP/shim.log"; fail "shim did not answer /health within 60 s"; }
  sleep 0.5
done
ok "health: $(cat "$TMP/health.json")"

# (a) loopback only
ADDRS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print $9}' | sort -u)
[ -n "$ADDRS" ] || fail "lsof shows no listener on $PORT"
for a in $ADDRS; do
  case "$a" in 127.0.0.1:"$PORT") ;; *) fail "listener on $a, expected 127.0.0.1 only" ;; esac
done
ok "listener: $ADDRS"

# post BODY_FILE URL OUT_FILE -> prints HTTP status
post() {
  curl -s -H 'Expect:' -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
    -o "$3" -w '%{http_code}' --data-binary "@$1" "$2"
}
mk() {  # mk FILE JSON
  printf '%s' "$2" >"$1"
}

# (b) non-stream on-device
mk "$TMP/b.json" '{"model":"apple/on-device","max_tokens":50,"messages":[{"role":"user","content":"Reply with one word: pong"}]}'
S=$(post "$TMP/b.json" "$BASE/v1/messages" "$TMP/b.out")
[ "$S" = 200 ] || { cat "$TMP/b.out"; echo; fail "(b) on-device non-stream status $S"; }
python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); t=d["content"][0]["text"]; assert t.strip(), "empty"; assert d.get("stop_reason"), "no stop_reason"; print("   text=%r stop=%s usage=%s" % (t.strip()[:80], d["stop_reason"], d["usage"]))' "$TMP/b.out" || fail "(b) body"
ok "(b) on-device non-stream -> 200"

# (c) stream:true
mk "$TMP/c.json" '{"model":"apple/on-device","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"Reply with one word: pong"}]}'
S=$(post "$TMP/c.json" "$BASE/v1/messages" "$TMP/c.out")
[ "$S" = 200 ] || fail "(c) stream status $S"
grep -q 'event: message_stop' "$TMP/c.out" || fail "(c) no message_stop event"
ok "(c) on-device stream -> 200, message_stop present"

# (d) over-window prompt is refused, not truncated
# Varied text: a repeated token trips the guardrail before the window check, which proves nothing.
python3 -I -c 'import json; s=" ".join("Sentence %d describes a small harbour town where fishing boats return at dusk." % i for i in range(1500)); print(json.dumps({"model":"apple/on-device","max_tokens":50,"messages":[{"role":"user","content":s}]}))' >"$TMP/d.json"
S=$(post "$TMP/d.json" "$BASE/v1/messages" "$TMP/d.out")
[ "$S" = 400 ] || { cat "$TMP/d.out"; echo; fail "(d) over-window status $S, want 400"; }
python3 -I -c 'import json,sys; e=json.load(open(sys.argv[1]))["error"]; assert e["type"]=="invalid_request_error", e; assert "Nothing was truncated" in e["message"], e; print("   " + e["message"][:160])' "$TMP/d.out" || fail "(d) body"
ok "(d) over-window -> 400 invalid_request_error, nothing truncated"

# (e) tools rejected, not ignored
mk "$TMP/e.json" '{"model":"apple/on-device","max_tokens":50,"tools":[{"name":"Bash","input_schema":{"type":"object"}}],"messages":[{"role":"user","content":"hi"}]}'
S=$(post "$TMP/e.json" "$BASE/v1/messages" "$TMP/e.out")
[ "$S" = 400 ] || fail "(e) tools status $S, want 400"
ok "(e) tools -> 400"

# (f) unknown model
mk "$TMP/f.json" '{"model":"nope","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'
S=$(post "$TMP/f.json" "$BASE/v1/messages" "$TMP/f.out")
[ "$S" = 404 ] || fail "(f) unknown model status $S, want 404"
ok "(f) unknown model -> 404"

# (g) two messages rejected
mk "$TMP/g.json" '{"model":"apple/on-device","max_tokens":50,"messages":[{"role":"user","content":"hi"},{"role":"assistant","content":"yo"},{"role":"user","content":"again"}]}'
S=$(post "$TMP/g.json" "$BASE/v1/messages" "$TMP/g.out")
[ "$S" = 400 ] || fail "(g) two messages status $S, want 400"
ok "(g) multi-message -> 400"

# (h) query string from Claude Code (?beta=true) is accepted
mk "$TMP/h.json" '{"model":"apple/on-device","max_tokens":50,"messages":[{"role":"user","content":"Reply with one word: pong"}]}'
S=$(post "$TMP/h.json" "$BASE/v1/messages?beta=true" "$TMP/h.out")
[ "$S" = 200 ] || fail "(h) ?beta=true status $S"
ok "(h) /v1/messages?beta=true -> 200"

# (i) PCC: 200, or 403 permission_error while the binary is unsigned. Anything else fails.
mk "$TMP/i.json" '{"model":"apple-pcc/pcc","max_tokens":50,"messages":[{"role":"user","content":"Reply with one word: pong"}]}'
S=$(post "$TMP/i.json" "$BASE/v1/messages" "$TMP/i.out")
case "$S" in
  200) ok "(i) pcc -> 200" ;;
  403) python3 -I -c 'import json,sys; e=json.load(open(sys.argv[1]))["error"]; assert e["type"]=="permission_error", e; print("   " + e["message"][:160])' "$TMP/i.out" || fail "(i) 403 body"
       ok "(i) pcc -> 403 permission_error (unsigned binary, expected until signed)" ;;
  *) cat "$TMP/i.out"; echo; fail "(i) pcc status $S" ;;
esac

# (j) a full id carries its own prefix: apple/pcc and apple-pcc/on-device are not models (#98)
for m in apple/pcc apple-pcc/on-device; do
  mk "$TMP/j.json" "{\"model\":\"$m\",\"max_tokens\":50,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
  S=$(post "$TMP/j.json" "$BASE/v1/messages" "$TMP/j.out")
  [ "$S" = 404 ] || fail "(j) $m status $S, want 404"
done
ok "(j) apple/pcc and apple-pcc/on-device -> 404"

echo "smoke: all checks passed"
