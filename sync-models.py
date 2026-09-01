#!/usr/bin/env python3
"""Publish the router's gateway models into Claude Code's /model picker.

Claude Code *will* show gateway models in the picker, but its own discovery
never reaches us, for two independent reasons (both read out of the 2.1.252
binary):

  1. Before fetching, it requires ANTHROPIC_AUTH_TOKEN, an API key, or an
     apiKeyHelper. On a claude.ai subscription there is none of those, so it
     logs "skipped: no credential" and never issues GET /v1/models.
  2. If it did fetch, it filters the response to ids matching /claude|anthropic/i
     and bails with "0 usable models after filter". Every OpenRouter id we
     surface fails that test.

The picker itself reads a *cache file* and applies neither check: it only
requires CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY, a first-party provider,
and cache.baseUrl == ANTHROPIC_BASE_URL. So we write that cache ourselves.

Schema (from the binary's validator):
    {"baseUrl": str, "fetchedAt": int_ms, "models": [{"id": str,
                                                      "display_name": str?}]}
written 0600. Claude Code reads it at startup, so a new entry needs a restart.
"""
import json, os, sys, time, urllib.request

BASE = os.environ.get("ANTHROPIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
CACHE = os.path.expanduser("~/.claude/cache/gateway-models.json")


def fetch():
    req = urllib.request.Request(BASE + "/v1/models",
                                 headers={"anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("data", [])


def main():
    try:
        data = fetch()
    except Exception as e:
        # Router down or slow: leave any existing cache alone rather than
        # clobbering a good picker with an empty one.
        print("sync-models: router unreachable (%s); cache left as-is" % e,
              file=sys.stderr)
        return 1

    # Only the provider-qualified ids are ours. Bare claude-* ids are already
    # in the picker natively; re-adding them would duplicate every entry.
    models = [{"id": m["id"], "display_name": m.get("display_name") or m["id"]}
              for m in data if "/" in m.get("id", "")]
    if not models:
        print("sync-models: no gateway models surfaced; cache left as-is",
              file=sys.stderr)
        return 1

    payload = json.dumps({"baseUrl": BASE, "fetchedAt": int(time.time() * 1000),
                          "models": models})

    try:
        with open(CACHE, "r") as f:
            old = json.load(f)
        if old.get("baseUrl") == BASE and old.get("models") == models:
            return 0                      # unchanged; don't churn the mtime
    except Exception:
        pass

    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    fd = os.open(CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    print("sync-models: published %d models -> %s"
          % (len(models), ", ".join(m["id"] for m in models)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
