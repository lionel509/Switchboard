#!/usr/bin/env python3
"""Manage the Switchboard model catalog.

    switchboard.py list
    switchboard.py add ~google/gemini-pro-latest
    switchboard.py add qwen/qwen3.8-flash --no-zdr --good-at "cheap agentic coding"
    switchboard.py remove qwen/qwen3.8-flash
    switchboard.py sync
    switchboard.py login            # pick from a list, open the key page
    switchboard.py login --all      # is every provider actually signed in?

`add` looks the id up on OpenRouter and fills in name, price and context itself,
so the catalog cannot drift into models that do not exist. Restart the router and
re-run `sync` for a change to reach the picker.
"""
import argparse, getpass, json, os, platform, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid

HERE    = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "models.json")
KEYFILE = os.path.expanduser("~/.config/openrouter-key")

TIERS = ("flash", "mini", "lite", "air", "pro", "max", "ultra")
OAUTH_POLL_MAX = 300        # seconds to wait for browser approval


def load():
    with open(CATALOG) as f:
        return json.load(f)


def save(cat):
    with open(CATALOG, "w") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
        f.write("\n")


def or_models():
    key = open(KEYFILE).read().strip()
    req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                 headers={"Authorization": "Bearer " + key})
    return {m["id"]: m for m in json.load(urllib.request.urlopen(req, timeout=60))["data"]}


def guess(mid):
    """family and tier from the id — 'google/gemini-pro-latest' -> gemini, pro."""
    slug = mid.split("/")[-1].lower()
    family = re.split(r"[-.\d]", slug)[0] or slug
    tier = next((t for t in TIERS if t in slug), "pro")
    return family, tier


def cmd_list(args):
    cat = load()
    print("router model : %s" % cat.get("router_model", "(none)"))
    print("fallback     : %s" % cat.get("fallback", "(none)"))
    for name, p in sorted(cat.get("providers", {}).items()):
        kf = p.get("key_file", "")
        mark = "✓" if read_keyfile(kf) else "✗ no key — run: switchboard login " + name
        print("subscription : %s -> %s%s  (key: %s %s)"
              % (name, p.get("host", "?"), p.get("path_prefix", ""), kf or "?", mark))
    print()
    by_family = {}
    for m in cat["models"]:
        by_family.setdefault(m.get("family", "?"), []).append(m)
    for fam in sorted(by_family):
        print(fam)
        for m in sorted(by_family[fam], key=lambda x: x.get("price", [0])[0]):
            if m.get("billing") == "subscription":
                # not $0 — a different currency, and now more than one plan can
                # be the payer, so say which pool it draws down.
                cost = "%s quota" % (m["id"].split("/")[0] if "/" in m["id"]
                                     else "claude")
            elif (m.get("price") or [0])[0] < 0:
                cost = "varies (delegated)"
            else:
                p = m.get("price", ["?", "?"])
                cost = "$%s/$%s per 1M" % (p[0], p[1])
            warn = "  ⚠ no ZDR" if m.get("zdr", True) is False else ""
            print("  %-9s %-34s %-18s %s%s"
                  % (m.get("tier", "?"), m["id"], cost, m.get("name", ""), warn))
            if m.get("specialties"):
                print("  %-9s %s★ %s%s" % ("", " " * 34, ", ".join(m["specialties"]),
                                           "  (%s)" % m["source"] if m.get("source") else ""))
        print()
    return 0


def cmd_add(args):
    cat = load()
    if any(m["id"] == args.id for m in cat["models"]):
        print("already in the catalog: %s" % args.id, file=sys.stderr)
        return 1

    if args.subscription or args.provider:
        # Plan-billed either way, so there is nothing to look up on OpenRouter.
        # Two kinds land here and they differ on one thing that matters — whether
        # the picker already knows the id:
        #   bare        an Anthropic id. Claude Code lists it natively; publishing
        #               it again would duplicate every Claude row.
        #   --provider  an outside subscription on that vendor's own endpoint. Its
        #               id is vendor-qualified, so it DOES have to be published.
        vendor = args.provider
        known = cat.get("providers", {})
        if vendor and vendor not in known:
            print("no provider %r in models.json. Known: %s"
                  % (vendor, ", ".join(sorted(known)) or "none"), file=sys.stderr)
            print("Add one under 'providers' first — it needs host, path_prefix "
                  "and key_file.", file=sys.stderr)
            return 1
        mid = args.id
        if vendor and not mid.startswith(vendor + "/"):
            mid = vendor + "/" + mid          # the prefix is what routes it
        if any(m["id"] == mid for m in cat["models"]):
            print("already in the catalog: %s" % mid, file=sys.stderr)
            return 1
        family, tier = guess(mid)
        entry = {"id": mid, "name": args.name or mid,
                 "family": args.family or vendor or family,
                 "tier": args.tier or tier,
                 "billing": "subscription", "price": [0, 0],
                 "good_at": args.good_at or ""}
        if args.upstream_id:
            entry["upstream_id"] = args.upstream_id
        if args.specialty:
            entry["specialties"] = args.specialty
        if args.source:
            entry["source"] = args.source
        cat["models"].append(entry)
        save(cat)
        print("added %s as a subscription model (no cash cost, consumes quota)" % mid)
        if not entry["good_at"]:
            print("⚠ no --good-at set. Auto's router model picks by these "
                  "descriptions, so an empty one makes this model unpickable.")
        if vendor:
            p = known[vendor]
            print("routes to %s%s, key read from %s"
                  % (p.get("host", "?"), p.get("path_prefix", ""),
                     p.get("key_file", "?")))
            if args.upstream_id:
                print("goes on the wire as %r" % args.upstream_id)
            print("Restart the router, then:")
            print("  switchboard.py picker --add %s --apply && switchboard.py sync"
                  % mid)
        else:
            print("Restart the router — no sync needed, it is already in the picker.")
        return 0

    try:
        remote = or_models()
    except Exception as e:
        print("could not reach OpenRouter: %s" % e, file=sys.stderr)
        return 1
    if args.id not in remote:
        print("no such model on OpenRouter: %s" % args.id, file=sys.stderr)
        near = [i for i in remote if args.id.split("/")[-1][:12] in i][:8]
        if near:
            print("did you mean:\n  " + "\n  ".join(near), file=sys.stderr)
        return 1

    m = remote[args.id]
    pricing = m.get("pricing", {})
    # Router models (openrouter/auto and friends) report -1: the price is whatever
    # they end up selecting, so there is no number to record.
    variable = float(pricing.get("prompt", 0)) < 0
    price = [-1, -1] if variable else [
        round(float(pricing.get("prompt", 0)) * 1e6, 4),
        round(float(pricing.get("completion", 0)) * 1e6, 4)]
    family, tier = guess(args.id)
    entry = {"id": args.id,
             "name": args.name or m.get("name") or args.id,
             "family": args.family or family,
             "tier": args.tier or tier,
             "price": price,
             "context": m.get("context_length"),
             "good_at": args.good_at or ""}
    if args.specialty:
        entry["specialties"] = args.specialty
    if args.source:
        entry["source"] = args.source
    if args.no_zdr:
        entry["zdr"] = False
    cat["models"].append(entry)
    save(cat)

    print("added %s  (%s / %s)  $%s/$%s per 1M"
          % (entry["id"], entry["family"], entry["tier"], price[0], price[1]))
    if not entry["good_at"]:
        print("⚠ no --good-at set. Auto's router model picks by these descriptions, "
              "so an empty one makes this model effectively unpickable.")
    if not args.no_zdr:
        print("⚠ ZDR was not checked — OpenRouter does not expose it in the model list. "
              "If its providers page shows only orange shields, re-add with --no-zdr, "
              "or requests for it will fail with no providers.")
    print("Restart the router and run: switchboard.py sync")
    return 0


def cmd_remove(args):
    cat = load()
    before = len(cat["models"])
    cat["models"] = [m for m in cat["models"] if m["id"] != args.id]
    if len(cat["models"]) == before:
        print("not in the catalog: %s" % args.id, file=sys.stderr)
        return 1
    for field in ("router_model", "fallback"):
        if cat.get(field) == args.id:
            print("⚠ %s was the %s; set a new one in models.json"
                  % (args.id, field), file=sys.stderr)
    save(cat)
    print("removed %s — restart the router and run: switchboard.py sync" % args.id)
    return 0


SETTINGS = os.path.expanduser("~/.claude/settings.json")


def cmd_apply(args):
    """Write a modelPicker lineup into ~/.claude/settings.json.

    replaceBuiltInOptions defines the WHOLE menu, so Claude rows you never use
    can be dropped and every gateway variant listed explicitly — which fits under
    the ten-row limit in a way that adding rows never does. Schema, from the
    binary: {"options": [{model, label?, description?}], replaceBuiltInOptions}.

    The lineup comes from picker_lineup in models.json, in order. It also owns
    the description column: without one Claude Code prints "From gateway" for a
    gateway row, which says nothing about when to pick it. A row carrying only a
    model id fills itself in from the catalog as "<blurb> · <price>".
    """
    cat = load()
    by_id = {m["id"]: m for m in cat["models"]}

    def money(n):
        return ("%g" % n) if isinstance(n, (int, float)) else str(n)

    opts = []
    for row in cat.get("picker_lineup", []):
        mid = row["model"]
        m = by_id.get(mid)
        if not m and "/" in mid and not mid.startswith(("~auto/", "~fam/", "~pick/")):
            print("not in the catalog, listed anyway: %s" % mid, file=sys.stderr)
        label = row.get("label") or (m or {}).get("name") or mid
        if m and m.get("zdr", True) is False and "ZDR" not in label:
            label += " (no ZDR)"
        desc = row.get("description")
        if desc is None:
            bits = []
            if (m or {}).get("blurb"):
                bits.append(m["blurb"])
            p = (m or {}).get("price") or []
            if (m or {}).get("billing") == "subscription":
                # Which plan pays is the whole distinction now that more than one
                # can. A vendor id carries its provider as the prefix; a bare id
                # is the Claude plan. "plan quota" on both would say nothing.
                vendor = mid.split("/")[0] if "/" in mid else ""
                prov = cat.get("providers", {}).get(vendor, {})
                bits.append("%s quota" % (prov.get("label") or vendor.title()
                                          or "plan"))
            elif p and isinstance(p[0], (int, float)) and p[0] < 0:
                bits.append("priced by whatever it selects")
            elif p:
                bits.append("$%s/$%s per 1M" % (money(p[0]), money(p[1])))
            desc = " · ".join(bits)
        if not desc:
            # Claude Code falls back to "From gateway" here, which is noise.
            print("⚠ no description for %s — add a blurb to the catalog" % mid,
                  file=sys.stderr)
        opts.append({"model": mid, "label": label, "description": desc})
    if not opts:
        print("picker_lineup is empty in models.json — nothing to write",
              file=sys.stderr)
        return 1

    try:
        with open(SETTINGS) as f:
            s = json.load(f)
    except Exception as e:
        print("could not read %s: %s" % (SETTINGS, e), file=sys.stderr)
        return 1

    if args.revert:
        if s.pop("modelPicker", None) is None:
            print("no modelPicker lineup was set")
            return 0
        with open(SETTINGS, "w") as f:
            json.dump(s, f, indent=2)
            f.write("\n")
        print("removed the modelPicker lineup — the built-in menu is back. "
              "Restart Claude Code.")
        return 0

    backup = SETTINGS + ".bak"
    with open(backup, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    s["modelPicker"] = {"replaceBuiltInOptions": True, "options": opts}
    with open(SETTINGS, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")

    print("wrote a %d-row lineup to %s (backup: %s)" % (len(opts), SETTINGS, backup))
    for i, o in enumerate(opts, 1):
        print("  %2d. %-13s %-36s %s" % (i, o.get("label", ""), o["model"],
                                         o.get("description", "")))
    if len(opts) > 10:
        print("⚠ over 10 rows — the rest collapse behind '… +%d models'."
              % (len(opts) - 10))
    print("\nreplaceBuiltInOptions hides gateway-discovered rows, so this lineup is "
          "the whole menu.\nRestart Claude Code. Undo with: switchboard.py apply --revert")
    return 0


def cmd_picker(args):
    """Compose the /model menu. Affects menu length only — Auto still sees everything."""
    cat = load()
    p = cat.setdefault("picker", {"families": [], "models": []})
    fams = p.setdefault("families", [])
    mods = p.setdefault("models", [])
    known = {m["id"] for m in cat["models"]}
    known_fams = {m.get("family") for m in cat["models"] if m.get("family") != "claude"}

    for tgt in (args.add or []):
        if tgt in known_fams:
            if tgt not in fams:
                fams.append(tgt)
        elif tgt in known:
            if "/" not in tgt:
                print("%s is an Anthropic id — already in the picker natively" % tgt,
                      file=sys.stderr)
                continue
            if tgt not in mods:
                mods.append(tgt)
        else:
            print("not a known family or catalog id: %s" % tgt, file=sys.stderr)
            return 1
    for tgt in (args.rm or []):
        if tgt in fams:
            fams.remove(tgt)
        elif tgt in mods:
            mods.remove(tgt)
        else:
            print("not currently in the picker: %s" % tgt, file=sys.stderr)
            return 1
    if args.add or args.rm:
        save(cat)

    rows = len(fams) + len(mods) + 1                  # +1 for Auto
    print("gateway rows (%d):" % rows)
    for f in fams:
        tiers = sorted({m.get("tier", "?") for m in cat["models"]
                        if m.get("family") == f})
        print("  family  %-14s -> %s" % (f, "/".join(tiers)))
    for m in mods:
        print("  model   %s" % m)
    print("  auto    ~auto/auto")
    total = 6 + rows
    print("\n6 Claude rows are always shown, so the menu is %d rows." % total)
    if total > 10:
        print("⚠ Over 10 — Claude Code collapses the rest behind '… +%d models'." % (total - 10))
    if args.add or args.rm:
        print("Restart the router and run: switchboard.py sync")
    return 0


def read_keyfile(path):
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return None


def mask(key):
    """Enough of a key to recognise it, never enough to use it."""
    k = key.strip()
    if len(k) <= 12:
        return "*" * len(k)
    return k[:6] + "…" + k[-4:]


def clipboard():
    """The system clipboard as text, or None where there is no clipboard tool."""
    for cmd in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"]):
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, timeout=5)
            except Exception:
                return None
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace").strip()
            continue          # installed but failing (e.g. wl-paste with no Wayland)
    return None


def open_url(url):
    """Hand a URL to the desktop browser. False if there is no opener.

    http/https only. `open` dispatches file:// and any registered app scheme, and
    console_url is free text from --set-console, so the scheme is checked here
    rather than trusted.
    """
    if not re.match(r"^https?://", url or ""):
        return False
    for cmd in ("open", "xdg-open"):
        if shutil.which(cmd):
            try:
                subprocess.run([cmd, url], capture_output=True, timeout=10)
                return True
            except Exception:
                return False
    return False


def provider_rows(cat):
    """Every declared provider with whether its key is actually stored."""
    rows = []
    for name, p in sorted(cat.get("providers", {}).items()):
        rows.append((name, p, bool(read_keyfile(p.get("key_file", "")))))
    return rows


def pick_provider(cat):
    """Choose a provider when the command line did not name one.

    `login` with no argument used to be an error listing the valid names, which
    is the same information one keystroke later than it is useful. The names are
    not memorable and the set is small, so show them with their key status and
    take a number.

    The list is printed even when only one provider is declared. Auto-selecting
    the single row saves a keystroke and hides the two things worth seeing first:
    which vendor is about to be signed into, and whether it already has a key.
    """
    rows = provider_rows(cat)
    if not rows:
        print("no providers declared under 'providers' in models.json", file=sys.stderr)
        return None
    print("Outside subscriptions:")
    for i, (name, p, has) in enumerate(rows, 1):
        print("  %d. %-8s %-28s %s"
              % (i, name, p.get("host", "?") + (p.get("path_prefix") or ""),
                 "✓ signed in" if has else "✗ no key"))
    names = dict((r[0], r) for r in rows)
    for _ in range(3):                 # a typo should not abort the whole command
        try:
            raw = input("which: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return None
        # "²".isdigit() is True while int("²") raises, hence the isascii guard.
        if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= len(rows):
            return rows[int(raw) - 1][0]
        if raw in names:
            return raw
        print("not a listed provider: %r — enter a number or a name." % raw,
              file=sys.stderr)
    return None


def prompt_key(name, prov):
    """Get a key from the user, via the clipboard by default.

    Three things went wrong with a bare getpass prompt, and each is handled here.

    The key comes from a page that is hard to find and easy to confuse with the
    vendor's other console, so the console URL is offered first — the wrong-page
    401 is the failure this whole command exists to catch, and catching it after
    the paste is later than necessary.

    A key pasted into an invisible prompt cannot be checked by eye, so a blind
    paste of a shell prompt, a whole curl command or the wrong line of a page
    reads as a vendor outage. Reading the clipboard and echoing a masked preview
    makes the mistake visible before the round trip.

    Typing into getpass is still there for anyone who would rather not have a
    live credential on the clipboard.
    """
    label = prov.get("label") or name
    url = prov.get("console_url")
    hint = prov.get("console_hint")
    if url:
        print("Key page for %s:\n  %s" % (label, url))
        if hint:
            print("⚠ " + hint)
        try:
            if input("open it in the browser? [Y/n] ").strip().lower() in ("", "y", "yes"):
                if not open_url(url):
                    print("  (no browser opener found — open it by hand)")
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return None
    else:
        print("⚠ The key must come from %s's SUBSCRIPTION console. A "
              "pay-as-you-go key from the same account looks identical and 401s "
              "against this endpoint." % label)

    # Whether a clipboard tool exists, NOT what is on the clipboard. Calling
    # clipboard() here would read a credential the user may never choose to use.
    has_clip = any(shutil.which(c) for c in ("pbpaste", "wl-paste", "xclip"))
    if has_clip:
        print("\nCopy the key, then press Enter and I will read it from the "
              "clipboard.\nOr type it here instead — it is not echoed and never "
              "enters shell history.")
    else:
        print("\nPaste the key — not echoed, and it never enters shell history.")
    try:
        key = getpass.getpass("key [Enter = clipboard]: " if has_clip else "key: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return None

    if not key and has_clip:
        key = (clipboard() or "").strip()
        if not key:
            print("clipboard is empty", file=sys.stderr)
            return None
        if "\n" in key:
            lines = [ln.strip() for ln in key.splitlines() if ln.strip()]
            pat0 = prov.get("key_pattern")
            hit = next((ln for ln in lines if pat0 and re.fullmatch(pat0, ln)), None)
            # A copied block is usually `export KIMI_KEY=…` or a curl line with the
            # key on the NEXT line, so "first line" is the wrong guess more often
            # than not. Prefer the line actually shaped like a key.
            key = hit or (lines[0] if lines else "")
            print("  (clipboard had several lines — using %s)"
                  % ("the one shaped like a key" if hit else "the first"))
        print("  from clipboard: %s" % mask(key))
    if not key:
        print("nothing entered", file=sys.stderr)
        return None

    pat = prov.get("key_pattern")
    if pat and not re.fullmatch(pat, key):
        # A warning, not a gate. The pattern is this repo's guess at the vendor's
        # key shape, and a vendor is free to change it without telling anyone --
        # so a mismatch must never be able to lock out a key that actually works.
        why = ("it contains spaces" if any(c.isspace() for c in key) else
               "it is only %d characters" % len(key) if len(key) < 16 else
               "it has characters a key would not")
        print("⚠ That does not look like a %s key — %s: %s"
              % (label, why, mask(key)))
        try:
            if input("  use it anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                return None
        except (EOFError, KeyboardInterrupt):
            print("\naborted", file=sys.stderr)
            return None
    return key


def provider_probe(name, prov, key, cat, wire=None):
    """One minimal request to the vendor, to prove a key works before saving it.

    This is the whole value of `login` over `printf > keyfile`. The common
    failure is a key from the *wrong console* — a pay-as-you-go platform key and
    a subscription key are the same shape from the same account, and the wrong
    one fails later as a 401 in the middle of a task. Caught here it costs one
    round trip.
    """
    if wire is None:
        m = next((x for x in cat["models"] if x["id"].split("/")[0] == name), None)
        if not m:
            return None, "no model in the catalog for provider %r to test with" % name
        wire = m.get("upstream_id") or m["id"].split("/", 1)[-1]
    prefix = prov.get("auth_prefix", "")
    headers = {"content-type": "application/json",
               "anthropic-version": "2023-06-01",
               prov.get("auth_header", "x-api-key"):
                   (prefix + " " + key) if prefix else key}
    if not prov.get("host"):
        return None, "provider declares no host"
    url = "https://%s%s/v1/messages" % (prov["host"],
                                        (prov.get("path_prefix") or "").rstrip("/"))
    body = json.dumps({"model": wire, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.loads(e.read()).get("error") or {}).get("message", "")
        except Exception:
            pass
        return e.code, detail
    except Exception as e:
        return None, str(e)


def probe_models(name, prov, key, cat):
    """Ask the vendor which model ids this key can actually use.

    Plan documentation is unreliable and secondhand — for Kimi Code no official
    page states whether the 1M model is tier-gated, and the blogs that claim it
    disagree with each other. The key itself knows. So try the candidates rather
    than reading about them; ordered best-first, the first that answers is the
    one to configure.
    """
    m = next((x for x in cat["models"] if x["id"].split("/")[0] == name), None)
    cands = (m or {}).get("upstream_candidates") or []
    results = []
    for c in cands:
        status, detail = provider_probe(name, prov, key, cat, wire=c["id"])
        if status in (200, 429):
            verdict = "available"          # 429 = rate limited, so it exists
        elif status in (400, 404):
            verdict = "not on this plan"
        else:
            verdict = "inconclusive (HTTP %s)" % status
        results.append((c, verdict, detail))
    return m, results


def device_id(path):
    """A stable per-machine id. The token endpoint requires one; generated once."""
    p = os.path.expanduser(path)
    existing = read_keyfile(p)
    if existing:
        return existing
    val = str(uuid.uuid4())
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(val)
    os.chmod(p, 0o600)
    return val


def oauth_headers(oa):
    """Device headers the token endpoint requires.

    ⚠ These identify the CALLER, and `headers` in the provider config carries the
    vendor's own CLI identity. That is what makes this work and it is also what
    makes it a judgement call — see the disclosure printed by `login --oauth`.
    """
    h = {"Content-Type": "application/x-www-form-urlencoded",
         "Accept": "application/json"}
    h.update(oa.get("headers") or {})
    h.setdefault("X-Msh-Device-Name", platform.node() or "unknown")
    h.setdefault("X-Msh-Device-Model", "%s %s %s"
                 % (platform.system(), platform.release(), platform.machine()))
    h.setdefault("X-Msh-Os-Version", platform.version())
    h["X-Msh-Device-Id"] = device_id(oa.get("device_id_file",
                                            "~/.config/switchboard-device-id"))
    return h


def oauth_post(oa, path, fields):
    """Form-encoded POST to the OAuth host. Returns (status, dict)."""
    url = oa["host"].rstrip("/") + path
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, headers=oauth_headers(oa),
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"error": "unreachable", "error_description": str(e)}


def save_oauth(name, prov, tok):
    """Persist the token set, and the access token where the router reads it.

    Two files on purpose. The router is deliberately dumb — it reads one file and
    puts the contents in a header — so the access token goes to `key_file`
    unchanged and no router code needs to know OAuth exists. The refresh token
    and expiry live beside it, for `login --refresh` only.
    """
    oa = prov["oauth"]
    tok = dict(tok)
    tok["obtained_at"] = int(time.time())
    tf = os.path.expanduser(oa.get("token_file", "~/.config/%s-oauth.json" % name))
    for path, blob in ((tf, json.dumps(tok, indent=2)),
                       (os.path.expanduser(prov["key_file"]), tok["access_token"])):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(blob)
        os.chmod(path, 0o600)

    # An OAuth bearer is not an API key: it goes in a different header. Applying
    # it here rather than asking the user to hand-edit models.json.
    changed = []
    for field in ("auth_header", "auth_prefix"):
        want = oa.get(field)
        if want and prov.get(field) != want:
            prov[field] = want
            changed.append("%s=%s" % (field, want))
    return tf, changed


def oauth_login(name, prov):
    """RFC 8628 device authorization. Returns the token dict, or None."""
    oa = prov.get("oauth")
    if not oa or not oa.get("client_id") or not oa.get("host"):
        print("%s declares no usable 'oauth' block in models.json" % name,
              file=sys.stderr)
        return None

    ident = (oa.get("headers") or {}).get("X-Msh-Platform")
    print("Signing in to %s via its public OAuth client (%s…) at %s."
          % (prov.get("label") or name, oa["client_id"][:8], oa["host"]))
    if ident:
        # Only reachable if a config puts an identity back. Measured on
        # 2026-09-04, auth.kimi.com requires none, so nothing is claimed.
        print("⚠ This presents to the vendor as %r, which is not what this is." % ident)
    print("A browser page will ask you to approve this device on your account.")
    try:
        if input("continue? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("aborted — nothing sent")
            return None
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return None

    status, data = oauth_post(oa, oa.get("device_path", "/api/oauth/device_authorization"),
                              {"client_id": oa["client_id"]})
    if status != 200 or not data.get("device_code"):
        print("✗ device authorization failed (HTTP %s): %s" %
              (status, data.get("error_description") or data.get("error") or data),
              file=sys.stderr)
        return None

    uri = data.get("verification_uri_complete") or data.get("verification_uri") or ""
    code = data.get("user_code") or ""
    print("\nApprove this device:\n  %s" % uri)
    if code:
        print("  user code: %s" % code)
    if uri and open_url(uri):
        print("  (opened in your browser)")

    interval = max(int(data.get("interval") or 5), 1)
    deadline = time.time() + min(int(data.get("expires_in") or 300), OAUTH_POLL_MAX)
    print("\nwaiting for approval", end="", flush=True)
    while time.time() < deadline:
        time.sleep(interval)
        print(".", end="", flush=True)
        st, tok = oauth_post(oa, oa.get("token_path", "/api/oauth/token"),
                             {"client_id": oa["client_id"],
                              "device_code": data["device_code"],
                              "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if st == 200 and tok.get("access_token"):
            print(" approved")
            return tok
        err = (tok or {}).get("error") or ""
        if err == "expired_token":
            print("\n✗ the code expired before it was approved — run login again",
                  file=sys.stderr)
            return None
        if err == "access_denied":
            print("\n✗ approval was denied", file=sys.stderr)
            return None
        if err == "slow_down":
            interval += 5
        # authorization_pending, or a blip: keep polling until the deadline.
    print("\n✗ timed out after %ds waiting for approval" % OAUTH_POLL_MAX,
          file=sys.stderr)
    return None


def oauth_refresh(name, prov):
    """Renew the access token from the stored refresh token."""
    oa = prov.get("oauth") or {}
    tf = os.path.expanduser(oa.get("token_file", "~/.config/%s-oauth.json" % name))
    try:
        with open(tf) as f:
            stored = json.load(f)
    except (OSError, ValueError):
        print("no stored OAuth token at %s — run: switchboard login %s --oauth"
              % (tf, name), file=sys.stderr)
        return None
    rt = stored.get("refresh_token")
    if not rt:
        print("stored token has no refresh_token — run login --oauth again",
              file=sys.stderr)
        return None
    st, tok = oauth_post(oa, oa.get("token_path", "/api/oauth/token"),
                         {"client_id": oa["client_id"], "grant_type": "refresh_token",
                          "refresh_token": rt})
    if st != 200 or not tok.get("access_token"):
        print("✗ refresh failed (HTTP %s): %s"
              % (st, tok.get("error_description") or tok.get("error") or tok),
              file=sys.stderr)
        return None
    # Some servers omit refresh_token on renewal; keep the old one so the next
    # refresh still works.
    tok.setdefault("refresh_token", rt)
    return tok


def human_secs(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 3600:
        return "%d min" % (n // 60)
    if n < 86400:
        return "%.1f h" % (n / 3600.0)
    return "%.1f days" % (n / 86400.0)


def cmd_login(args):
    """Store an outside subscription's key, after checking the vendor accepts it."""
    # Each of these picks a different branch below, so a pair of them means the
    # command is about to ignore one of the user's flags without saying so.
    modes = [n for n, on in (("--all", args.all),
                             ("--oauth", args.oauth),
                             ("--refresh", args.refresh),
                             ("--set-console", args.set_console is not None),
                             ("--remove", args.remove),
                             ("--check", args.check)) if on]
    if len(modes) > 1:
        print("%s do different things — pass one." % " and ".join(modes),
              file=sys.stderr)
        return 2
    if args.all and args.provider:
        print("--all checks every provider; drop the provider name to use it, "
              "or drop --all to sign into %s." % args.provider, file=sys.stderr)
        return 2

    cat = load()
    known = cat.get("providers", {})

    if args.all:
        rows = provider_rows(cat)
        if not rows:
            print("no providers declared in models.json")
            return 0
        bad = 0
        for name, p, has in rows:
            if not has:
                print("\u2717 %-8s no key      run: switchboard login %s" % (name, name))
                bad += 1
                continue
            status, detail = provider_probe(name, p, read_keyfile(p.get("key_file", "")), cat)
            if status in (200, 400, 429):
                print("\u2713 %-8s key works  (HTTP %s)" % (name, status))
            else:
                print("\u2717 %-8s HTTP %s%s  run: switchboard login %s"
                      % (name, status, ": " + detail[:50] if detail else "", name))
                bad += 1
        return 1 if bad else 0

    name = args.provider or pick_provider(cat)
    if not name:
        return 1
    args.provider = name
    prov = known.get(name)
    if not prov:
        print("no provider %r. Known: %s"
              % (name, ", ".join(sorted(known)) or "none"), file=sys.stderr)
        return 1

    if args.oauth or args.refresh:
        tok = oauth_login(name, prov) if args.oauth else oauth_refresh(name, prov)
        if not tok:
            return 1
        tf, changed = save_oauth(name, prov, tok)
        if changed:
            save(cat)
            print("→ %s: %s (restart the router)" % (name, ", ".join(changed)))
        exp = tok.get("expires_in")
        print("stored %s and %s (0600)%s"
              % (tf, prov["key_file"],
                 " — access token expires in %s" % human_secs(exp) if exp else ""))
        # The open question this whole path existed to answer: does a token from
        # the OAuth flow actually authenticate against the coding endpoint?
        status, detail = provider_probe(name, prov, tok["access_token"], cat)
        if status in (200, 400, 429):
            print("✓ the OAuth token authenticates against %s%s (HTTP %s)"
                  % (prov.get("host"), prov.get("path_prefix", ""), status))
        else:
            print("✗ stored, but %s%s rejected the OAuth token (HTTP %s)%s"
                  % (prov.get("host"), prov.get("path_prefix", ""), status,
                     ": " + detail if detail else ""), file=sys.stderr)
            print("  The token is valid for the vendor's own CLI but not for this "
                  "endpoint — the subscription may require its console key instead.",
                  file=sys.stderr)
            return 1
        if exp:
            print("Renew before it expires with:  switchboard login %s --refresh" % name)
        return 0

    if args.set_console is not None:
        prov["console_url"] = args.set_console
        save(cat)
        print("%s console_url = %s" % (name, args.set_console))
        return 0
    path = os.path.expanduser(prov.get("key_file") or "")
    if not path:
        print("provider %r declares no key_file" % args.provider, file=sys.stderr)
        return 1

    if args.remove:
        if os.path.exists(path):
            os.remove(path)
            print("removed %s — %s rows will fail until you log in again"
                  % (path, args.provider))
        else:
            print("nothing stored at %s" % path)
        return 0

    if args.check:
        key = read_keyfile(path)
        if not key:
            print("no key stored at %s" % path, file=sys.stderr)
            return 1
    else:
        key = prompt_key(args.provider, prov)
        if not key:
            return 1

    status, detail = provider_probe(args.provider, prov, key, cat)
    if status == 200:
        verdict = "key accepted"
    elif status in (400, 429):
        # got past authentication, which is the only thing being tested here
        verdict = "key accepted (HTTP %s — past auth)" % status
    elif status in (401, 403):
        print("✗ %s rejected the key (HTTP %s)%s"
              % (args.provider, status, ": " + detail if detail else ""),
              file=sys.stderr)
        print("  Usual cause: the wrong console. %s%s only accepts its "
              "subscription credentials." % (prov.get("host", "?"),
                                             prov.get("path_prefix") or ""),
              file=sys.stderr)
        if not args.force:
            print("  NOT saved. Re-run with --force to store it regardless.",
                  file=sys.stderr)
            return 1
        verdict = ("stored key is REJECTED by the vendor" if args.check
                   else "stored UNVERIFIED (vendor rejected it)")
    else:
        print("could not reach %s: %s" % (prov.get("host", "?"), detail or status),
              file=sys.stderr)
        if not args.force:
            print("  NOT saved. Re-run with --force to store it anyway.",
                  file=sys.stderr)
            return 1
        verdict = ("stored key unverified (endpoint unreachable)" if args.check
                   else "stored UNVERIFIED (endpoint unreachable)")

    m, results = probe_models(args.provider, prov, key, cat)
    if results:
        print("\nmodels this key can actually use:")
        # Deliberately not `verdict` — that name holds the AUTHENTICATION result,
        # and the one line telling you a --force key was rejected is printed from
        # it after this loop. Rebinding it here silently replaced that warning
        # with whichever model happened to be probed last.
        for c, mverdict, mdetail in results:
            print("  %s %-10s %11s tokens  %s%s"
                  % ("✓" if mverdict == "available" else "✗", c["id"],
                     "{:,}".format(c["context"]), mverdict,
                     ("  — " + mdetail[:60]) if mdetail and mverdict != "available" else ""))
        best = next((c for c, v, _ in results if v == "available"), None)
        if best is None:
            print("⚠ none of the candidates answered. Leaving the catalog at %r."
                  % m.get("upstream_id"), file=sys.stderr)
        elif m.get("upstream_id") != best["id"] or m.get("context") != best["context"]:
            was = m.get("upstream_id")
            m["upstream_id"], m["context"] = best["id"], best["context"]
            save(cat)
            print("→ catalog updated: %s → %s (%s tokens). Restart the router."
                  % (was, best["id"], "{:,}".format(best["context"])))
        else:
            print("→ catalog already correct (%s)." % best["id"])

    if not args.check:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        os.chmod(path, 0o600)      # os.open's mode is ignored for an existing file
        print("wrote %s (0600)" % path)
    print("%s: %s" % (args.provider, verdict))
    if not args.check:
        print("The router reads the file per request, but restart it if it was "
              "started before the provider existed:")
        print("  pkill -f claude-router/router.py")
    return 0


def cmd_sync(args):
    sync = os.path.join(HERE, "sync-models.py")
    return os.spawnv(os.P_WAIT, sys.executable, [sys.executable, sync])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the catalog").set_defaults(fn=cmd_list)

    a = sub.add_parser("add", help="add a model, looked up on OpenRouter")
    a.add_argument("id")
    a.add_argument("--name", help="label shown in the picker")
    a.add_argument("--family", help="e.g. gemini, deepseek")
    a.add_argument("--tier", help="e.g. flash, pro")
    a.add_argument("--good-at", help="what it is for — fed to Auto's router model")
    a.add_argument("--specialty", action="append", metavar="TAG",
                   help="a measured strength, e.g. --specialty frontend. Repeatable. "
                        "Specialties outrank tier and price when Auto routes, so add "
                        "one only for a strength you can point at evidence for.")
    a.add_argument("--source", help="where a specialty claim came from, e.g. a benchmark")
    a.add_argument("--no-zdr", action="store_true",
                   help="model has no zero-data-retention provider")
    a.add_argument("--subscription", action="store_true",
                   help="a plan-billed id, not sold per token; skips the OpenRouter "
                        "lookup. Bare id means Anthropic — for an outside "
                        "subscription use --provider instead")
    a.add_argument("--provider", metavar="NAME",
                   help="route to an outside subscription declared under 'providers' "
                        "in models.json (e.g. kimi). Implies --subscription, and "
                        "prefixes the id with NAME/ since that prefix is what routes it")
    a.add_argument("--upstream-id", metavar="ID",
                   help="the model name to put on the wire when it differs from the "
                        "picker id, e.g. 'k3[1m]' — brackets are Claude Code's own "
                        "context-variant syntax and cannot survive in a picker id")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="remove a model")
    r.add_argument("id")
    r.set_defaults(fn=cmd_remove)

    lg = sub.add_parser("login", help="store an outside subscription's key, after "
                                     "checking the vendor actually accepts it")
    lg.add_argument("provider", nargs="?",
                    help="a name under 'providers' in models.json, e.g. kimi. "
                         "Omit it to pick from a list showing which are signed in")
    lg.add_argument("--oauth", action="store_true",
                    help="sign in with the provider's device-authorization flow "
                         "(approve in a browser) instead of pasting a key")
    lg.add_argument("--refresh", action="store_true",
                    help="renew the OAuth access token from the stored refresh token")
    lg.add_argument("--all", action="store_true",
                    help="check every provider's stored key and exit")
    lg.add_argument("--set-console", metavar="URL",
                    help="record where this provider's key page lives, so login "
                         "can offer to open it")
    lg.add_argument("--check", action="store_true",
                    help="verify the stored key instead of prompting for a new one")
    lg.add_argument("--remove", action="store_true", help="delete the stored key")
    lg.add_argument("--force", action="store_true",
                    help="store the key even if the vendor rejects it or is unreachable")
    lg.set_defaults(fn=cmd_login)

    p = sub.add_parser("picker", help="compose the /model menu (length only; "
                                      "Auto still sees the whole catalog)")
    p.add_argument("--add", action="append", metavar="FAMILY|ID",
                   help="add a family row (collapses its variants, tier picked "
                        "per task) or one exact model row. Repeatable.")
    p.add_argument("--rm", action="append", metavar="FAMILY|ID", help="remove a row")
    p.set_defaults(fn=cmd_picker)

    ap2 = sub.add_parser("apply", help="write a modelPicker lineup to settings.json, "
                                       "replacing the whole menu so every variant fits")
    ap2.add_argument("--revert", action="store_true",
                     help="remove the lineup and restore the built-in menu")
    ap2.set_defaults(fn=cmd_apply)

    sub.add_parser("sync", help="publish the catalog to the picker cache"
                   ).set_defaults(fn=cmd_sync)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
