#!/usr/bin/env python3
"""Manage the Switchboard model catalog.

    switchboard.py list
    switchboard.py add ~google/gemini-pro-latest
    switchboard.py add qwen/qwen3.8-flash --no-zdr --good-at "cheap agentic coding"
    switchboard.py remove qwen/qwen3.8-flash
    switchboard.py sync

`add` looks the id up on OpenRouter and fills in name, price and context itself,
so the catalog cannot drift into models that do not exist. Restart the router and
re-run `sync` for a change to reach the picker.
"""
import argparse, json, os, re, sys, urllib.request

HERE    = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(HERE, "models.json")
KEYFILE = os.path.expanduser("~/.config/openrouter-key")

TIERS = ("flash", "mini", "lite", "air", "pro", "max", "ultra")


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
    print("fallback     : %s\n" % cat.get("fallback", "(none)"))
    by_family = {}
    for m in cat["models"]:
        by_family.setdefault(m.get("family", "?"), []).append(m)
    for fam in sorted(by_family):
        print(fam)
        for m in sorted(by_family[fam], key=lambda x: x.get("price", [0])[0]):
            if m.get("billing") == "subscription":
                cost = "quota, no cash"       # not $0 — it is a different currency
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

    if args.subscription:
        # An Anthropic id billed to the plan. Not on OpenRouter, so nothing to
        # look up, and never published to the picker — it is already there.
        family, tier = guess(args.id)
        cat["models"].append({"id": args.id, "name": args.name or args.id,
                              "family": args.family or family,
                              "tier": args.tier or tier,
                              "billing": "subscription", "price": [0, 0],
                              "good_at": args.good_at or ""})
        save(cat)
        print("added %s as a subscription model (no cash cost, consumes quota)"
              % args.id)
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
    """
    cat = load()
    by_id = {m["id"]: m for m in cat["models"]}
    opts = list(cat.get("claude_rows", []))
    for mid in cat.get("apply_models", []):
        m = by_id.get(mid)
        if not m:
            print("not in the catalog, skipped: %s" % mid, file=sys.stderr)
            continue
        label = m.get("name") or mid
        if m.get("zdr", True) is False:
            label += " (no ZDR)"
        p = m.get("price", ["?", "?"])
        if isinstance(p[0], (int, float)) and p[0] < 0:
            desc = "Priced by whatever it selects"
        else:
            desc = "$%s/$%s per 1M" % (p[0], p[1])
        if m.get("specialties"):
            desc += " · " + ", ".join(m["specialties"])
        opts.append({"model": mid, "label": label, "description": desc})
    opts.append({"model": "~auto/auto", "label": "Auto",
                 "description": "Routed per task by a cheap router model"})

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
        print("  %2d. %-16s %s" % (i, o.get("label", ""), o["model"]))
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
                   help="an Anthropic id billed to the plan; skips the OpenRouter lookup")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("remove", help="remove a model")
    r.add_argument("id")
    r.set_defaults(fn=cmd_remove)

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
