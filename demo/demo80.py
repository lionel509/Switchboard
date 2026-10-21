#!/usr/bin/env python3
"""Demo for Switchboard #80 (decision model, #79), at head dc679d2.

Imports the real router.py from this checkout and prints plain text. Four runs:
  1. LIVE               two real tasks through the decision chain (cash, strict ZDR key)
  2. CHAIN FALLTHROUGH  model 1 is an invalid id, so it errors live; classify-1 answers (cash)
  3. CLAUDE-ONLY        a made-up claude-only folder named Private: openrouter_post is
                        stubbed to record calls; 0 calls and 0 auto_decision rows
  4. OFF                decision_model "": no decisions call, no row (HTTP stubbed)

Every requests.log row goes to a scratch ledger in a fresh temp dir, never to
~/.local/share/claude-router. Stdlib only. Usage: python3 demo/demo80.py
"""
import json, os, sys, tempfile, time

# Point the router's ledger at a scratch file BEFORE router.py reads LOGFILE.
SCRATCH = tempfile.mkdtemp(prefix="demo80-")
LEDGER = os.path.join(SCRATCH, "requests.log")
os.environ["CLAUDE_ROUTER_LOG"] = LEDGER
os.environ.pop("CLAUDE_ROUTER_AUTO_DECIDER", None)   # use the models.json chain

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # the real router.py

REAL_POST = router.openrouter_post
TASK_LIVE = [
    "fix the typo in README line 4",
    "design a multi-file refactor of the auth layer with tests",
]
TASK_FALLTHROUGH = "rename colour to color in every file under docs/"
TASK_PRIVATE = "summarise the notes in this folder"
TASK_OFF = "list the TODO lines in the repo"
INVALID_MODEL = "typesafe/no-such-model-demo"


def out(s=""):
    print(s, flush=True)


def ledger_rows():
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER) as f:
        return [json.loads(line) for line in f if line.strip()]


def num(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace("$", ""))
        except ValueError:
            return None
    return None


def money(x):
    v = num(x)
    return "n/a" if v is None else "$%.7f" % v


def new_run():
    router._auto_pins.clear()   # each run decides fresh, not from a pinned earlier turn


def live_call_log(calls):
    """Print each decision call a live run made."""
    for c in calls:
        body, p = c["body"], c["payload"]
        crit = body["questions"]["model"]["criteria"]
        ids = [k for k in crit if k != "none"]
        out("  chain model asked : %s" % body["model"])
        out("  candidates sent   : %d ids: %s" % (len(ids), ", ".join(ids)))
        if p is None or "error" in p:
            err = (p or {}).get("error")
            msg = err.get("message") if isinstance(err, dict) else err
            out("  answered          : NO (error: %s)" % (msg or "no response (timeout or transport)"))
        else:
            ans = (p.get("answers") or {}).get("model") or {}
            usage = p.get("usage") or {}
            out("  answered by       : %s (response model)" % p.get("model"))
            out("  pick              : %s" % ans.get("choice"))
            out("  confidence        : %s" % ans.get("confidence"))
            out("  latency           : %d ms" % c["ms"])
            out("  cost              : %s  (in %s, out %s)" % (
                money(usage.get("cost")), usage.get("input_tokens"), usage.get("output_tokens")))


def live_post(path, body, key, headers=None):
    t0 = time.perf_counter()
    payload = REAL_POST(path, body, key, headers)
    CALLS.append({"path": path, "body": body, "payload": payload,
                  "ms": round((time.perf_counter() - t0) * 1000)})
    return payload


def stub_post(path, body, key, headers=None):
    STUBBED.append({"path": path, "body": body})
    return None   # no network: the call is recorded and nothing is sent


CALLS, STUBBED = [], []
failures = []


def check(cond, label):
    out("  [%s] %s" % ("PASS" if cond else "FAIL", label))
    if not cond:
        failures.append(label)


def auto_rows(rows):
    return [r for r in rows if r.get("kind") == "auto_decision"]


# --- header ---------------------------------------------------------------
out("Switchboard #80 demo, decision model (#79)")
out("router.py from: %s" % os.path.relpath(router.__file__, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
out("scratch ledger: <fresh temp dir>/requests.log (never ~/.local/share/claude-router)")
out("decision chain (models.json): %s" % router.decision_chain())
out("decision path: %s" % router.DECISION_PATH)
out("candidates in catalog: %d" % len(router.CANDIDATES))

# --- 1. LIVE ----------------------------------------------------------------
out("")
out("=== 1. LIVE: two real tasks, strict-ZDR key, cash ===")
router.openrouter_post = live_post
live_totals = []
for task in TASK_LIVE:
    new_run()
    CALLS.clear()
    out("")
    out("task: %r" % task)
    chosen = router.resolve_auto({"messages": [{"role": "user", "content": task}]})
    live_call_log(CALLS)
    dec = [c for c in CALLS if c["path"] == router.DECISION_PATH]
    other = [c["path"] for c in CALLS if c["path"] != router.DECISION_PATH]
    out("  auto resolved to  : %s" % chosen)
    if other:
        out("  NOTE: non-decision calls made: %s" % other)
    check(len(dec) >= 1, "at least one decision call for %r" % task[:30])
    check(bool(dec) and dec[-1]["payload"] is not None and "error" not in dec[-1]["payload"],
          "decision call answered for %r" % task[:30])
    live_totals.extend(c["payload"] for c in dec if c["payload"])

# --- 2. CHAIN FALLTHROUGH ----------------------------------------------------
out("")
out("=== 2. CHAIN FALLTHROUGH: model 1 invalid, classify-1 answers, live ===")
router.DECISION_MODEL = [INVALID_MODEL, "scaledown/classify-1"]
out("chain set to: %s" % router.decision_chain())
new_run()
CALLS.clear()
before = len(ledger_rows())
out("task: %r" % TASK_FALLTHROUGH)
chosen = router.resolve_auto({"messages": [{"role": "user", "content": TASK_FALLTHROUGH}]})
live_call_log(CALLS)
out("  auto resolved to  : %s" % chosen)
rows2 = ledger_rows()[before:]
out("  ledger rows this run: %d (auto_decision: %d)" % (len(rows2), len(auto_rows(rows2))))
dec2 = [c for c in CALLS if c["path"] == router.DECISION_PATH]
check(len(dec2) == 2, "two decision calls: invalid id, then classify-1")
check(len(dec2) == 2 and dec2[1]["payload"] is not None and "error" not in dec2[1]["payload"],
      "classify-1 answered after the invalid id errored")
check(len(dec2) == 2 and (dec2[0]["payload"] is None or "error" in dec2[0]["payload"]),
      "invalid id errored live")
live_totals.extend(c["payload"] for c in dec2 if c["payload"])
router.DECISION_MODEL = router.CATALOG.get("decision_model")

# --- 3. CLAUDE-ONLY ----------------------------------------------------------
out("")
out("=== 3. CLAUDE-ONLY: made-up folder /demo/Private, data=claude ===")
router.POLICY["/demo/Private"] = {"data": "claude"}
body = b"Primary working directory: /demo/Private\n"
pol = router.policy_for(router.cwd_of(body))
out("cwd: %s -> data level: %s (may_send_task: %s)" % (
    router.cwd_of(body), pol.get("data"), router.may_send_task(pol)))
new_run()
rows_before = len(ledger_rows())
STUBBED.clear()
router.openrouter_post = stub_post
chosen3 = router.resolve_auto({"messages": [{"role": "user", "content": TASK_PRIVATE}]}, pol)
router.openrouter_post = live_post
rows3 = ledger_rows()[rows_before:]
out("openrouter_post calls recorded: %d %s" % (len(STUBBED), [s["path"] for s in STUBBED]))
out("auto resolved to : %s (Anthropic pick, no router call)" % chosen3)
out("auto_decision rows this run: %d" % len(auto_rows(rows3)))
check(len(STUBBED) == 0, "openrouter_post never called under claude folder")
check(len(auto_rows(rows3)) == 0, "0 auto_decision rows written")

# --- 4. OFF ------------------------------------------------------------------
out("")
out("=== 4. OFF: decision_model \"\" (HTTP stubbed, nothing sent) ===")
router.DECISION_MODEL = ""
out("decision chain: %s" % router.decision_chain())
new_run()
STUBBED.clear()
rows_before = len(ledger_rows())
router.openrouter_post = stub_post
chosen4 = router.resolve_auto({"messages": [{"role": "user", "content": TASK_OFF}]})
router.openrouter_post = live_post
rows4 = ledger_rows()[rows_before:]
paths4 = [s["path"] for s in STUBBED]
out("openrouter_post calls recorded: %s" % paths4)
out("auto resolved to: %s" % chosen4)
out("auto_decision rows this run: %d" % len(auto_rows(rows4)))
check(router.DECISION_PATH not in paths4, "no /api/alpha/decisions call when off")
check(len(auto_rows(rows4)) == 0, "0 auto_decision rows when off")
router.DECISION_MODEL = router.CATALOG.get("decision_model")

# --- ledger totals ------------------------------------------------------------
out("")
out("=== scratch ledger, all rows of this demo ===")
rows = ledger_rows()
by_kind = {}
for r in rows:
    k = r.get("kind") or "request"
    n, c = by_kind.get(k, (0, 0.0))
    by_kind[k] = (n + 1, c + (num(r.get("cost")) or 0.0))
for k, (n, c) in sorted(by_kind.items()):
    out("  %-14s rows %2d  cost %s" % (k, n, "$%.7f" % c))
total = sum(num(r.get("cost")) or 0.0 for r in rows)
out("  %-14s rows %2d  cost %s" % ("TOTAL", len(rows), "$%.7f" % total))
out("  auto_decision rows: %d" % len(auto_rows(rows)))
live_cost = sum(num((p.get("usage") or {}).get("cost")) or 0.0 for p in live_totals if p)
out("  live decision cost (from responses): %s" % ("$%.7f" % live_cost))

out("")
if failures:
    out("RESULT: FAIL (%d): %s" % (len(failures), "; ".join(failures)))
    sys.exit(1)
out("RESULT: all checks passed")
