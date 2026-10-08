#!/usr/bin/env python3
"""switchboard run <role>: a headless prompt walks plan -> antigravity -> cash
and stops at the first tier with headroom.

  plan        one `claude -p` through the router, tagged x-switchboard-run:
              plan:<id>. The router keeps its mid-run Claude->Kimi failover but
              strips metered models from the chain, and answers 402 instead of
              relaying a final 429 -- so Claude Code fails fast and the walk
              moves on instead of retrying for minutes or sliding onto cash.
  antigravity Google's own `agy` CLI in print mode, on ONE Google account,
              pre-checked with the free `agy -p /quota`. Never its OAuth token,
              never a googleapis endpoint called by us, never account rotation:
              Google's reinstatement terms call circumventing usage limits a
              ban offence. It is neither Claude nor ZDR, so only folders with
              data policy "any" may reach it.
  cash        `claude -p --model xiaomi/mimo-v2.6-pro` through the router --
              the only step that can spend money.

A tier is only skipped on a positive capacity signal (402/429/529 in the run's
own ledger records or api_error_status, a meter past the headroom line, or agy
error text matching its own quota wording). Anything else stops the walk with
exit 3, so a broken prompt can never buy MiMo tokens.
"""
import calendar, http.client, json, os, re, shutil, subprocess, sys, time, uuid

import router

CAPACITY = router.FAILOVER_STATUSES              # 402, 429, 529
STALE = 30 * 60                                   # a meter file older than this says nothing
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
RUN_TAG = "x-switchboard-run"
# Wording lifted from the agy binary's own strings ("Out of credits", RESOURCE_EXHAUSTED,
# "rate limit") -- not yet seen live; the ledger keeps the raw text so the first real
# exhaustion can correct this. The bare word "quota" is NOT here: an ADC/auth failure
# ("quota project not set") names it without meaning exhaustion.
AGY_QUOTA_RE = re.compile(r"out of credits|resource_exhausted|rate.?limit|too many requests|\b429\b", re.I)


def meters(logdir, now=None):
    """Percent USED per plan, from the meter files the router writes beside
    requests.log. A missing, unparseable or stale file leaves its key absent --
    unknown means try it, never exhausted."""
    now = time.time() if now is None else now
    out = {}
    for fname, key in (("rate-limits.json", "claude"), ("kimi-limits.json", "kimi")):
        try:
            with open(os.path.join(logdir, fname)) as f:
                d = json.load(f)
            ts = calendar.timegm(time.strptime(d["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
            if now - ts > STALE:
                continue
            pcts = [float(v) for k, v in d.items()
                    if k.endswith("_pct") and k != "spend_pct"
                    and isinstance(v, (int, float))]
            if pcts:
                out[key] = max(pcts)
        except Exception:
            continue
    return out


def pick_plan(role, used, headroom):
    """The plan-tier start model: the role's plan model while its meter has
    headroom, else the Kimi fallback, else None. An unknown meter reads as
    headroom -- the router's 402 is the backstop. The meter follows the model
    id: the coder's plan override is kimi/k3-256k, so the Claude meter says
    nothing about it."""
    for key in ("plan", "kimi"):
        model = role.get(key)
        if not model:
            continue
        meter = "kimi" if model.startswith("kimi/") else "claude"
        if used.get(meter, 0) < headroom:
            return model
    return None


def agy_meter(text):
    """Percent USED per bucket from `agy -p /quota` TSV lines
    (name <tab> label <tab> 72% <tab> reset-iso), same scale as meters()."""
    out = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 3 or not cols[2].strip().endswith("%"):
            continue
        try:
            pct = float(cols[2].strip().rstrip("%"))
        except ValueError:
            continue
        key = "claude" if cols[0].strip().lower().startswith("claude") else "gemini"
        out[key] = 100 - pct
    return out


def agy_bucket(model):
    """Which /quota bucket an antigravity model draws from."""
    return "claude" if model.startswith(("claude", "gpt")) else "gemini"


def tier_allowed(name, model, pol):
    """Whether the folder's data policy lets this tier run here. Antigravity is
    neither Claude nor ZDR, so only 'any' folders may reach it."""
    if name == "plan":
        return True
    if name == "antigravity":
        return (pol.get("data") or "any") == "any"
    return router.allowed(model, pol)


def exhausted(result, records):
    """True only on a positive capacity signal: the run's own ledger records or
    Claude Code's api_error_status carry a 402/429/529. Any other failure is
    not exhaustion and must stop the walk rather than spend the next tier."""
    if result and not result.get("is_error"):
        return False
    return ((records[-1].get("status") if records else None) in CAPACITY
            or (result or {}).get("api_error_status") in CAPACITY)


def agy_outcome(stdout, stderr):
    """Classify an agy print run: "ok", "exhausted" (its own quota wording --
    walk on) or "failed" (anything else, including TIMEOUT -- stop)."""
    try:
        d = json.loads(stdout)
    except ValueError:
        d = {}
    if not isinstance(d, dict):          # valid JSON like null / [1] / "x"
        d = {}
    if d.get("status") == "SUCCESS":
        return "ok", d
    msg = " ".join(str(x) for x in (d.get("error") or d.get("error_message") or "",
                                    d.get("status") or "", stderr[-2000:]))
    # TIMEOUT/CANCELLED say nothing about quota, whatever stderr carries.
    if d.get("status") in ("TIMEOUT", "CANCELLED"):
        return "failed", {"error": msg[:300], "status": d.get("status")}
    return ("exhausted" if AGY_QUOTA_RE.search(msg) else "failed",
            {"error": msg[:300], "status": d.get("status")})


def ledger_since(path, offset, run):
    """This run's ledger records appended after `offset`."""
    out = []
    try:
        with open(path) as f:
            f.seek(offset)
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("run") == run:
                    out.append(rec)
    except OSError:
        pass
    return out


def agy_record(run, role, model, status, ms, usage, error=None):
    """A ledger record for an antigravity call, same shape as the router's.
    Plan-billed, so no `cost` key -- like anthropic records."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "upstream": "antigravity",
           "model_requested": model, "model_used": model, "run": run,
           "role": role, "status": status, "ms": ms}
    for src, dst in (("input_tokens", "in"), ("output_tokens", "out"),
                     ("thinking_tokens", "thinking"),
                     ("cache_read_tokens", "cache_read")):
        if usage.get(src) is not None:
            rec[dst] = usage[src]
    if error is not None:
        rec["error"] = error
    return rec


def claude_cmd(model, tools):
    return [shutil.which("claude"), "-p", "--model", model, "--output-format", "json",
            "--strict-mcp-config", "--allowedTools", *tools]


def canonical_folder(path):
    """The folder with its on-disk casing. macOS lookups are case-insensitive
    but the policy roots are stored (and matched) in real case, so a lowercase
    --dir would slip past a zdr root -- and Antigravity has no router behind
    it to re-check. chdir+getcwd is how the OS reports the real spelling."""
    here = os.getcwd()
    try:
        os.chdir(path)
        return os.getcwd()
    finally:
        os.chdir(here)


def router_tagged(port):
    """Whether the live router understands run tags: post-#46 it advertises
    x-switchboard-run on GET /v1/models. A pre-#46 router would let a plan run
    fail over onto metered models, so the plan tier must not run against it."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
        c.request("GET", "/v1/models")
        resp = c.getresponse()
        tagged = resp.getheader(RUN_TAG) is not None
        resp.read()
        c.close()
        return tagged
    except Exception:
        return False


def agy_cmd(agy, prompt, model, timeout, folder):
    return [agy, "-p", prompt, "--model", model, "--output-format", "json",
            "--print-timeout", timeout, "--dangerously-skip-permissions",
            "--add-dir", folder]


def run(args):
    """The walk. Prints one decision line per tier to stderr, the answer to
    stdout. 0 answered, 2 every tier exhausted or skipped, 3 a real failure."""
    cfg = router.CATALOG["dispatch"]
    role = cfg["roles"].get(args.role)
    if role is None:
        print("unknown role %r — known: %s" % (args.role, ", ".join(sorted(cfg["roles"]))),
              file=sys.stderr)
        return 1
    if args.headroom is None:
        args.headroom = cfg.get("headroom", 95)
    if args.prompt_file:
        with open(args.prompt_file) as f:
            prompt = f.read()
    else:
        prompt = sys.stdin.read()
    if not prompt.strip():
        print("empty prompt — pass --prompt-file or pipe one on stdin", file=sys.stderr)
        return 1
    folder = canonical_folder(os.path.abspath(args.dir or os.getcwd()))
    pol = router.policy_for(folder)
    rid = uuid.uuid4().hex[:8]
    logdir = os.path.dirname(router.LOGFILE)
    port = os.environ.get("CLAUDE_ROUTER_PORT", "8787")

    tiers = ["plan", "antigravity", "cash"]
    if args.from_:
        tiers = tiers[tiers.index(args.from_):]
    if args.no_cash and "cash" in tiers:
        tiers.remove("cash")

    if subprocess.run(["nc", "-z", "127.0.0.1", port],
                      capture_output=True).returncode:
        print("router not listening on :%s — run `switchboard restart`" % port,
              file=sys.stderr)
        return 1
    if "plan" in tiers and not router_tagged(port):
        # A pre-#46 router keeps metered models in the failover chain, so a
        # plan run that 429s everywhere lands on MiMo while billed as plan.
        print("run %s: plan: skipped (the live router predates #46 — it would "
              "fail over onto metered models; merge, update the live tree, "
              "restart)" % rid, file=sys.stderr)
        tiers.remove("plan")
    env = dict(os.environ, ANTHROPIC_BASE_URL="http://127.0.0.1:%s" % port)

    def summary(recs):
        counts = {}
        for rec in recs:
            if rec.get("status") == 200:
                counts[rec["upstream"]] = counts.get(rec["upstream"], 0) + 1
        return ", ".join("%s %d" % kv for kv in sorted(counts.items())) or "no 200s"

    for tier in tiers:
        if tier == "plan":
            used = meters(logdir)
            model = pick_plan(role, used, args.headroom)
            if model is None:
                print("run %s: plan: skipped (claude %s%%, kimi %s%% used ≥ headroom %s)"
                      % (rid, used.get("claude", "?"), used.get("kimi", "?"),
                         args.headroom), file=sys.stderr)
                continue
            print("run %s: plan %s (claude %s%% kimi %s%% used)"
                  % (rid, model, used.get("claude", "?"), used.get("kimi", "?")),
                  file=sys.stderr)
            try:
                offset = os.path.getsize(router.LOGFILE)
            except OSError:
                offset = 0
            p = subprocess.run(claude_cmd(model, args.allowed_tools), input=prompt,
                               text=True, capture_output=True, cwd=folder,
                               env=dict(env, ANTHROPIC_CUSTOM_HEADERS=
                                        "x-switchboard-run: plan:%s" % rid))
            try:
                result = json.loads(p.stdout)
            except ValueError:
                result = None
            recs = ledger_since(router.LOGFILE, offset, "plan:%s" % rid)
            if not recs:
                print("warning: no ledger record carries this run id — the running "
                      "router predates #46; `switchboard restart`", file=sys.stderr)
            if result and not result.get("is_error"):
                print("run %s: plan: ok (%s)" % (rid, summary(recs)), file=sys.stderr)
                print(result.get("result", ""))
                return 0
            if exhausted(result, recs):
                print("run %s: plan: exhausted (402 after %s; %s answered first — "
                      "the next tier restarts from the prompt)"
                      % (rid, ", ".join(r["model_requested"] for r in recs) or model,
                         summary(recs)), file=sys.stderr)
                continue
            print("run %s: plan: failed — %s"
                  % (rid, p.stderr[-500:] or (result or {}).get("result") or p.stdout[-500:]),
                  file=sys.stderr)
            return 3

        if tier == "antigravity":
            model = role["antigravity"]
            if not tier_allowed("antigravity", model, pol):
                print("run %s: antigravity: skipped (data policy %s)"
                      % (rid, pol.get("data") or "any"), file=sys.stderr)
                continue
            agy = os.path.expanduser(cfg["agy"])
            if not os.path.exists(agy):
                # Antigravity was never tried, so cash has not been earned --
                # only an explicit `--from cash` (which skips this tier) may
                # buy MiMo without it.
                print("run %s: antigravity: failed — no agy at %s (fix the "
                      "path, or rerun with --from cash)" % (rid, agy),
                      file=sys.stderr)
                return 3
            q = subprocess.run([agy, "-p", "/quota", "--output-format", "text",
                                "--print-timeout", "60s"],
                               capture_output=True, text=True)
            am = agy_meter(q.stdout)
            u = am.get(agy_bucket(model))
            if u is not None and u >= args.headroom:
                print("run %s: antigravity: skipped (%s weekly %s%% used)"
                      % (rid, agy_bucket(model), u), file=sys.stderr)
                continue
            print("run %s: antigravity %s (%s)" % (rid, model,
                  ", ".join("%s %s%% used" % kv for kv in sorted(am.items()))
                  or "meter unread — trying anyway"), file=sys.stderr)
            t0 = time.time()
            p = subprocess.run(agy_cmd(agy, prompt, model, role["agy_timeout"], folder),
                               capture_output=True, text=True, cwd=folder)
            kind, d = agy_outcome(p.stdout, p.stderr)
            ms = int((time.time() - t0) * 1000)
            if kind == "exhausted":
                # Quota wording in error text is an unverified signal (an ADC
                # auth failure can mention "quota"), so confirm against the
                # free meter: only measured exhaustion walks on to cash.
                q = subprocess.run([agy, "-p", "/quota", "--output-format", "text",
                                    "--print-timeout", "60s"],
                                   capture_output=True, text=True)
                u = agy_meter(q.stdout).get(agy_bucket(model))
                if u is None or u < args.headroom:
                    kind = "failed"
                    d = dict(d, error="%s (/quota shows %s%% used, below "
                             "headroom %s — not exhaustion)"
                             % (d.get("error") or "",
                                "?" if u is None else u, args.headroom))
            if kind == "ok":
                router.log_request(agy_record("agy:%s" % rid, args.role, model, 200,
                                              ms, d.get("usage") or {}))
                print("run %s: antigravity: ok (%dms)" % (rid, ms), file=sys.stderr)
                print(d.get("response", ""))
                return 0
            if kind == "exhausted":
                router.log_request(agy_record("agy:%s" % rid, args.role, model, 429,
                                              ms, d.get("usage") or {},
                                              error=d.get("error")))
                print("run %s: antigravity: exhausted — %s" % (rid, d.get("error")),
                      file=sys.stderr)
                continue
            router.log_request(agy_record("agy:%s" % rid, args.role, model, 500,
                                          ms, d.get("usage") or {},
                                          error=d.get("error")))
            print("run %s: antigravity: failed — %s" % (rid, d.get("error")),
                  file=sys.stderr)
            return 3

        if tier == "cash":
            model = role["cash"]
            if not tier_allowed("cash", model, pol):
                print("run %s: cash: skipped (data policy %s)"
                      % (rid, pol.get("data") or "any"), file=sys.stderr)
                continue
            print("run %s: cash %s (metered — this spends money)"
                  % (rid, model), file=sys.stderr)
            try:
                offset = os.path.getsize(router.LOGFILE)
            except OSError:
                offset = 0
            p = subprocess.run(claude_cmd(model, args.allowed_tools), input=prompt,
                               text=True, capture_output=True, cwd=folder,
                               env=dict(env, ANTHROPIC_CUSTOM_HEADERS=
                                        "x-switchboard-run: cash:%s" % rid))
            try:
                result = json.loads(p.stdout)
            except ValueError:
                result = None
            recs = ledger_since(router.LOGFILE, offset, "cash:%s" % rid)
            if result and not result.get("is_error"):
                spent = sum(r.get("cost") or 0 for r in recs)
                print("run %s: cash: ok (%s; cash spent: $%.4f)"
                      % (rid, summary(recs), spent), file=sys.stderr)
                print(result.get("result", ""))
                return 0
            print("run %s: cash: failed — %s"
                  % (rid, p.stderr[-500:] or (result or {}).get("result") or p.stdout[-500:]),
                  file=sys.stderr)
            return 3

    print("run %s: every tier exhausted or skipped" % rid, file=sys.stderr)
    return 2
