#!/usr/bin/env python3
"""Full-screen editor for the Switchboard catalog.

`switchboard` with no subcommand lands here. The CLI still does everything this
does -- this exists because the CLI equivalents are long: `add` alone carries ten
flags, and putting a model in the picker is three chained commands.

curses from the standard library, to keep the repo dependency-free. Colour is
256-colour indexed rather than 24-bit: Apple Terminal advertises COLORTERM
truecolor but does not render it, and indexed degrades cleanly everywhere.
"""
import curses
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

FIELDS = [
    ("failover",    "Fallback",     "list"),
    ("name",        "Picker name",  "text"),
    ("blurb",       "Blurb",        "text"),
    ("tier",        "Tier",         "text"),
    ("good_at",     "Good at",      "text"),
    ("specialties", "Specialties",  "list"),
    ("source",      "Evidence",     "text"),
]

# xterm-256 indices. ACCENT is #875fff, the nearest cube colour to the vault's
# #9061ff, so the editor matches the Obsidian theme rather than inventing a look.
ACCENT, ACCENT_D = 99, 61
GREEN, RED, YELLOW = 114, 203, 179
DIM, FAINT = 245, 240

ROLES = {}          # role -> attr, filled by init_colors()
_DIRTY = [False]    # module-level so the Ctrl-C handler can see it


def init_colors():
    """Semantic roles -> attributes, degrading to plain attrs without colour."""
    global ROLES
    plain = {
        "title": curses.A_BOLD, "meta": curses.A_DIM, "rule": curses.A_DIM,
        "head": curses.A_BOLD, "sel": curses.A_BOLD, "id": 0, "tier": curses.A_DIM,
        "price": curses.A_DIM, "name": 0, "on": curses.A_BOLD, "off": curses.A_DIM,
        "na": curses.A_DIM, "ok": curses.A_BOLD, "bad": curses.A_BOLD,
        "star": curses.A_BOLD, "note": curses.A_DIM, "key": curses.A_BOLD,
        "warn": curses.A_BOLD,
    }
    if not curses.has_colors():
        ROLES = plain
        return
    curses.start_color()
    try:
        curses.use_default_colors()          # keep the user's own background
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    n = curses.COLORS
    def c(idx, fallback):
        return idx if n >= 256 else fallback
    spec = {
        "title": (c(ACCENT, curses.COLOR_MAGENTA), curses.A_BOLD),
        "meta":  (c(DIM, curses.COLOR_WHITE), 0),
        "rule":  (c(FAINT, curses.COLOR_BLUE), 0),
        "head":  (c(ACCENT_D, curses.COLOR_MAGENTA), curses.A_BOLD),
        "sel":   (c(ACCENT, curses.COLOR_MAGENTA), curses.A_BOLD),
        "id":    (-1, 0),
        "tier":  (c(DIM, curses.COLOR_CYAN), 0),
        "price": (c(DIM, curses.COLOR_WHITE), 0),
        "name":  (-1, 0),
        "on":    (c(ACCENT, curses.COLOR_MAGENTA), curses.A_BOLD),
        "off":   (c(FAINT, curses.COLOR_WHITE), 0),
        "na":    (c(FAINT, curses.COLOR_WHITE), 0),
        "ok":    (c(GREEN, curses.COLOR_GREEN), 0),
        "bad":   (c(RED, curses.COLOR_RED), 0),
        "star":  (c(YELLOW, curses.COLOR_YELLOW), 0),
        "note":  (c(DIM, curses.COLOR_WHITE), 0),
        "key":   (c(ACCENT, curses.COLOR_MAGENTA), curses.A_BOLD),
        "warn":  (c(YELLOW, curses.COLOR_YELLOW), curses.A_BOLD),
    }
    ROLES = {}
    for i, (role, (fg, attr)) in enumerate(sorted(spec.items()), start=1):
        if fg == -1:
            ROLES[role] = attr
            continue
        try:
            curses.init_pair(i, fg, bg)
            ROLES[role] = curses.color_pair(i) | attr
        except curses.error:
            ROLES[role] = plain.get(role, 0)


def attr(role):
    return ROLES.get(role, 0)


def price_of(m):
    if m.get("billing") == "subscription":
        # Not $0 -- a different currency. Name the pool it draws down.
        return "%s quota" % (m["id"].split("/")[0] if "/" in m["id"] else "claude")
    if (m.get("price") or [0])[0] < 0:
        return "varies"
    p = m.get("price", ["?", "?"])
    return "$%s/$%s" % (p[0], p[1])


def publishable(mid):
    """Bare claude-* ids are already in the picker natively; publishing them
    again duplicates every Claude row. Only ids with a "/" can be toggled."""
    return "/" in mid


def pad(s, n):
    s = str(s)
    return s if len(s) >= n else s + " " * (n - len(s))

def spend_today(lines, date):
    """Calculate daily spend from requests log lines."""
    results = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue

        ts = rec.get('ts', '')
        if not isinstance(ts, str) or not ts.startswith(date):
            continue

        group = rec.get('key') or rec.get('upstream')
        if group is None:
            continue

        if group not in results:
            results[group] = {'requests': 0, 'cost': 0.0, 'failover_cost': 0.0, 'refused': 0}

        res = results[group]
        res['requests'] += 1

        cost = rec.get('cost', 0)
        if cost is None:
            cost = 0
        res['cost'] += cost

        if 'failover_from' in rec:
            res['failover_cost'] += cost

        status = rec.get('status', 0)
        try:
            if status and int(status) >= 400:
                res['refused'] += 1
        except (ValueError, TypeError):
            pass

    for group in results:
        results[group]['cost'] = round(results[group]['cost'], 6)
        results[group]['failover_cost'] = round(results[group]['failover_cost'], 6)

    return results

def short(s, n=28):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def row_segs(kind, payload, cat, picks, keyed=False):
    """One row as [(text, role), ...]. Pure, so layout is testable without a tty."""
    if kind == "head":
        return [(payload, "head")]
    if kind in ("blank", "note"):
        return [(payload, "note")]
    if kind == "up":
        u = payload
        host = u.get("host", "?")
        if u.get("fixed"):
            state, role = "—  " + u.get("note", "always on"), "na"
        elif keyed:
            state, role = "●  signed in", "ok"
        else:
            state, role = "○  no key", "bad"
        return [(pad(u.get("label") or u["name"], 21), "id"), (pad(host, 22), "price"),
                (pad(state, 16), role)]
    if kind == "menu":
        n, r, primary = payload
        by_id = {m["id"]: m for m in cat.get("models", [])}
        m = by_id.get(r["model"]) or {}
        label = r.get("label") or m.get("name") or r["model"]
        return [(pad("%2d." % n, 4), "note"), (pad(short(label, 30), 32), "id"),
                (pad(short(r["model"], 34), 36), "price"),
                ("\u2605 new sessions start here" if primary else "", "star")]
    if kind == "pol":
        key, row = payload
        return [(pad(short(key, 34), 36), "id"), (pad("data " + row.get("data", "\u2014"), 13),
                 "bad" if row.get("data") in ("zdr", "claude") else "price"),
                ("tier " + row.get("tier", "\u2014"), "tier")]
    if kind == "spend":
        group, st = payload
        segs = [(pad(group, 27), "id"), (pad("$%.4f" % st["cost"], 11), "price"),
                (pad("%d req" % st["requests"], 10), "tier")]
        if st.get("failover_cost"):
            segs.append((pad("$%.4f failover" % st["failover_cost"], 18), "bad"))
        if st.get("refused"):
            segs.append(("%d refused" % st["refused"], "bad"))
        return segs
    if kind == "keyuse":
        label, use = payload
        if use is None:
            return [(pad(label, 27), "id"), ("checking…", "note")]
        if isinstance(use, str):
            return [(pad(label, 27), "id"), (use, "bad")]
        used, limit, reset = use
        txt = "$%.2f of $%s %s" % (used, "%g" % limit if limit else "∞", reset or "")
        return [(pad(label, 27), "id"), (txt, "bad" if limit and used >= 0.8 * limit else "ok")]
    if kind == "prov":
        p = cat["providers"][payload]
        host = p.get("host", "?") + (p.get("path_prefix") or "")
        state = "●  signed in" if keyed else "○  no key"
        return [(pad(payload, 21), "id"), (pad(host, 22), "price"),
                (pad(state, 16), "ok" if keyed else "bad"),
                ("oauth" if p.get("oauth") else "", "note")]
    m = payload
    mid = m["id"]
    if not publishable(mid):
        box, box_role = "·  ", "na"
    elif mid in picks:
        box, box_role = "●  ", "on"
    else:
        box, box_role = "○  ", "off"
    segs = [(box, box_role), (pad(short(mid, 34), 35), "id"),
            (pad(m.get("tier", "?"), 7), "tier"),
            (pad(price_of(m), 15), "price"), (m.get("name", ""), "name")]
    if m.get("upstream"):
        segs.append(("  key: " + m["upstream"], "star"))
    return segs


def key_of(m):
    """Which upstream key the router sends this model with (router.or_key)."""
    return m.get("upstream") or ("openrouter-nonzdr" if m.get("zdr", True) is False
                                 else "openrouter")


def key_choices(cat):
    """Upstream rows a model can pick: OpenRouter keys, in catalog order."""
    return [u["name"] for u in cat.get("upstreams", [])
            if u.get("host") == "openrouter.ai" and u.get("key_file")]


def routes_openrouter(m):
    """Cash models reached through OpenRouter -- the only ones a key choice means anything for."""
    return m.get("billing") != "subscription" and "/" in m["id"]


def cycle_key(cat, m):
    """Step m to the next key. Landing on the default drops the field, so a model
    that never chose a key keeps following its zdr flag."""
    names = key_choices(cat) or ["openrouter"]
    cur = key_of(m)
    nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
    m.pop("upstream", None)
    if nxt != key_of(m):
        m["upstream"] = nxt


def new_upstream(cat, name, label="", key_file=""):
    """An upstreams row for another OpenRouter key. 'gemma' -> openrouter-gemma."""
    import re
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name or ""):
        raise ValueError("name: lowercase letters, digits and dashes")
    if not name.startswith("openrouter"):
        name = "openrouter-" + name
    if any(u["name"] == name for u in cat.get("upstreams", [])):
        raise ValueError("%s already exists" % name)
    return {"name": name, "label": label or "OpenRouter (%s)" % name[len("openrouter-"):],
            "host": "openrouter.ai", "key_file": key_file or "~/.config/" + name.replace(
                "openrouter-", "openrouter-key-", 1),
            "verify_url": "https://openrouter.ai/api/v1/key",
            "console_url": "https://openrouter.ai/settings/keys",
            "note": "metered; sent only for models that pick it"}


# ---- the /model menu, fallbacks, folder policy (pure, so testable) ----------

MAX_MENU = 10            # Claude Code folds the rest behind "… +N models"
FALLBACK_MAX_IN = 0.20   # $/1M input: a cash fallback must be Flash-class (CLAUDE.md)
POLICY_DATA = ["any", "zdr", "claude"]
POLICY_TIER = ["pro", "flash"]


def in_menu(cat, mid):
    return any(r.get("model") == mid for r in cat.get("picker_lineup", []))


def menu_toggle(cat, mid):
    """Add mid to the end of the /model menu, or take it off. Gateway ids also go
    in picker.models, which is what the router publishes to the picker cache."""
    lineup = cat.setdefault("picker_lineup", [])
    picks = cat.setdefault("picker", {}).setdefault("models", [])
    if in_menu(cat, mid):
        cat["picker_lineup"] = [r for r in lineup if r.get("model") != mid]
        if mid in picks:
            picks.remove(mid)
    else:
        lineup.append({"model": mid})
        if "/" in mid and mid not in picks:
            picks.append(mid)


def menu_move(cat, mid, delta):
    lineup = cat.get("picker_lineup", [])
    i = next((n for n, r in enumerate(lineup) if r.get("model") == mid), None)
    j = None if i is None else i + delta
    if j is not None and 0 <= j < len(lineup):
        lineup[i], lineup[j] = lineup[j], lineup[i]


def fallback_problems(cat, ids):
    """Why each fallback breaks the money rule: a failover that can bill must be a
    plan model or the cheapest capable (<= $0.20/1M in), never a pro-tier one."""
    by_id = {m["id"]: m for m in cat.get("models", [])}
    out = []
    for i in ids:
        m = by_id.get(i)
        if not m:
            out.append("%s is not in the catalog, so it can't be a fallback" % i)
        elif m.get("billing") != "subscription" and (m.get("price") or [9e9])[0] > FALLBACK_MAX_IN:
            out.append("%s costs $%g/1M in -- a fallback must be a plan model or <= $%.2f/1M"
                       % (i, (m.get("price") or [0])[0], FALLBACK_MAX_IN))
    return out


def loosens(old, new):
    """True when a data policy change lets data go to more places."""
    rank = {d: i for i, d in enumerate(POLICY_DATA)}          # any < zdr < claude
    return rank.get(new, 0) < rank.get(old, 0)


def cycle_policy(cat, key, field):
    pol = cat.setdefault("folder_policy", {"_default": {"data": "any", "tier": "pro"}})
    row = pol.setdefault(key, {})
    order = POLICY_DATA if field == "data" else POLICY_TIER
    cur = row.get(field) or (pol.get("_default") or {}).get(field) or order[0]
    row[field] = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]


def row_text(kind, payload, cat, picks, keyed=False):
    return "".join(t for t, _ in row_segs(kind, payload, cat, picks, keyed))


def verify_gateway_key(u, key):
    """One request, so a bad paste is caught here rather than mid-task."""
    url = u.get("verify_url")
    if not url:
        return True, "not verified (no verify_url configured)"
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, "verified (HTTP %s)" % r.status
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "%s rejected the key (HTTP %s)." % (u["name"], e.code)
        return True, "accepted, then HTTP %s (not a credential problem)" % e.code
    except Exception as e:
        return False, "could not reach %s: %s" % (url, e)


class UI(object):
    def __init__(self, sb, scr):
        self.sb = sb
        self.scr = scr
        self.cat = sb.load()
        self.sel = 0
        self.top = 0
        self.dirty = False
        self.status = ""
        self.rows = []
        try:
            with open(sb.SETTINGS) as f:
                self.primary = json.load(f).get("model", "")
        except (OSError, ValueError):
            self.primary = ""
        self.new_primary = None
        self.keyuse = {}
        self.refresh_spend()
        self.rebuild()

    # ---------- spend ----------

    def refresh_spend(self):
        """Today's ledger totals now; each OpenRouter key's usage from its /key
        endpoint in the background, so opening the editor never waits on the network."""
        import threading, time
        try:
            with open(os.path.join(HERE, "requests.log")) as f:
                lines = f.readlines()[-30000:]
        except OSError:
            lines = []
        self.spend = spend_today(lines, time.strftime("%Y-%m-%d"))
        ups = [u for u in self.cat.get("upstreams", []) if u.get("verify_url") and u.get("key_file")]
        for u in ups:
            self.keyuse[u["name"]] = None

        def fetch(u):
            import urllib.request
            key = self.sb.read_keyfile(u["key_file"])
            if not key:
                self.keyuse[u["name"]] = "no key"
                return
            try:
                req = urllib.request.Request(u["verify_url"], headers={"Authorization": "Bearer " + key})
                d = json.load(urllib.request.urlopen(req, timeout=15)).get("data", {})
                self.keyuse[u["name"]] = (d.get("usage_daily", d.get("usage", 0)) or 0,
                                          d.get("limit"), d.get("limit_reset") or "")
            except Exception as e:
                self.keyuse[u["name"]] = "unreachable (%s)" % type(e).__name__
        for u in ups:
            threading.Thread(target=fetch, args=(u,), daemon=True).start()

    # ---------- model ----------

    def picker_set(self):
        return self.cat.setdefault("picker", {}).setdefault("models", [])

    def keyed(self, name):
        p = self.cat["providers"][name]
        return bool(self.sb.read_keyfile(p.get("key_file", "")))

    def rebuild(self):
        self._drawn_top = None           # rows may have shifted: repaint in full next draw
        rows = [("head", "PROVIDERS")]
        # Every credential path the router can use, not just the vendor-direct
        # ones. `upstreams` is display-only config -- the router never reads it --
        # so more can be listed here as they are wired up.
        for u in self.cat.get("upstreams", []):
            rows.append(("up", u))
        provs = self.cat.get("providers", {})
        for name in sorted(provs):
            rows.append(("prov", name))
        if len(rows) == 1:
            rows.append(("note", "none declared"))
        primary = self.new_primary or self.primary
        lineup = self.cat.get("picker_lineup", [])
        rows += [("blank", ""), ("head", "MENU   /model, in order  \u00b7  [ ] move  \u00b7  enter = start here  \u00b7  d remove")]
        for n, r in enumerate(lineup, 1):
            rows.append(("menu", (n, r, r.get("model") == primary)))
        if len(lineup) > MAX_MENU:
            rows.append(("note", "\u26a0 %d rows: Claude Code folds everything after row %d behind \u2026"
                         % (len(lineup), MAX_MENU)))
        if not lineup:
            rows.append(("note", "empty -- space on a model adds it"))
        rows += [("blank", ""), ("head", "MODELS   space = on/off the menu")]
        by_family = {}
        for m in self.cat.get("models", []):
            by_family.setdefault(m.get("family", "?"), []).append(m)
        for fam in sorted(by_family):
            for m in sorted(by_family[fam], key=lambda x: (x.get("price") or [0])[0]):
                rows.append(("model", m))
        pol = self.cat.get("folder_policy", {})
        rows += [("blank", ""), ("head", "POLICY   enter = data any/zdr/claude  \u00b7  t tier  \u00b7  a add folder  \u00b7  d forget")]
        for key in sorted(k for k in pol if not k.startswith("_") or k == "_default"):
            rows.append(("pol", (key, pol[key])))
        rows += [("blank", ""), ("head", "SPEND   today, from requests.log  \u00b7  r refresh")]
        for group, st in sorted(self.spend.items(), key=lambda kv: -kv[1]["cost"]):
            rows.append(("spend", (group, st)))
        if not self.spend:
            rows.append(("note", "nothing logged today"))
        for name in self.keyuse:
            label = next((u.get("label") or name for u in self.cat.get("upstreams", []) if u["name"] == name), name)
            rows.append(("keyuse", ("key: " + label, name)))
        self.rows = rows
        if self.sel >= len(rows):
            self.sel = max(0, len(rows) - 1)

    def selectable(self, i):
        kind, payload = self.rows[i]
        if kind == "up":
            return not payload.get("fixed")      # nothing to do for a fixed row
        return kind in ("prov", "model", "menu", "pol")

    def move(self, delta):
        self.reveal_end = False
        i = self.sel
        while True:
            i += delta
            if i < 0 or i >= len(self.rows):
                if delta > 0:                # past the last row you can select: show
                    self.reveal_end = True   # what's below it (SPEND is read-only)
                return
            if self.selectable(i):
                self.sel = i
                return

    def current(self):
        return self.rows[self.sel] if self.rows else ("blank", "")

    # ---------- drawing ----------

    def put(self, y, x, text, a=0, right=None):
        """Draw clipped to the screen and, when given, to a panel's right edge.

        Without `right` a long row runs straight through the panel border and into
        whatever is drawn beside it -- which is exactly what the model list did to
        the detail pane.
        """
        h, w = self.scr.getmaxyx()
        limit = w - 1 if right is None else min(w - 1, right)
        if y < 0 or y >= h or x >= limit:
            return x
        try:
            self.scr.addnstr(y, x, text, max(0, limit - x), a)
        except curses.error:
            pass
        return x + len(text)

    def putsegs(self, y, x, segs, force=None, right=None):
        for text, role in segs:
            if not text:
                continue
            x = self.put(y, x, text, attr(force or role), right)
            if right is not None and x >= right:
                break
        return x

    def box(self, top, left, height, width, title, active):
        """A rounded panel. The active one gets the accent border."""
        a = attr("sel" if active else "rule")
        if height < 2 or width < 4:
            return
        self.put(top, left, "\u256d" + "\u2500" * (width - 2) + "\u256e", a)
        if title:
            self.put(top, left + 2, " " + title + " ", attr("title" if active else "head"))
        for y in range(top + 1, top + height - 1):
            self.put(y, left, "\u2502", a)
            self.put(y, left + width - 1, "\u2502", a)
        self.put(top + height - 1, left,
                 "\u2570" + "\u2500" * (width - 2) + "\u256f", a)

    def wrap(self, text, width):
        out, line = [], ""
        for word in str(text).split():
            if len(line) + len(word) + 1 > width:
                if line:
                    out.append(line)
                line = word
            else:
                line = (line + " " + word).strip()
        if line:
            out.append(line)
        return out

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        _DIRTY[0] = self.dirty

        # Detail pane only when there is room for it; below that, list only.
        det_w = 0
        if w >= 92:
            det_w = max(30, min(40, w // 3))
        list_w = w - det_w - (1 if det_w else 0)
        panel_h = h - 2                      # leave the footer row + a gap

        title = "Switchboard"
        if self.dirty:
            title += "  \u2022 unsaved"

        inner_h = panel_h - 2
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + inner_h:
            self.top = self.sel - inner_h + 1
        if getattr(self, "reveal_end", False):     # scrolled to the end, cursor still on screen
            self.top = max(self.top, min(len(self.rows) - inner_h, self.sel))
        # Scrolling shifts every row, and ncurses' diff left stale characters
        # behind when it did (same fault as the model browser's filter): repaint.
        if self.top != getattr(self, "_drawn_top", self.top):
            self.scr.clear()          # also set to None by rebuild(), for the same reason
        self._drawn_top = self.top
        self.box(0, 0, panel_h, list_w, title, True)   # after any clear(), or it is wiped

        y = 1
        for i in range(self.top, min(len(self.rows), self.top + inner_h)):
            kind, payload = self.rows[i]
            cur = (i == self.sel)
            self.put(y, 1, "\u258c" if cur else " ", attr("sel"))
            x, edge = 3, list_w - 1
            if kind == "head":
                self.put(y, x, payload, attr("head"), edge)
            elif kind in ("blank", "note"):
                self.put(y, x, payload, attr("note"), edge)
            elif kind == "up":
                kf = payload.get("key_file") or ""
                self.putsegs(y, x, row_segs(kind, payload, self.cat, (),
                                            bool(self.sb.read_keyfile(kf))),
                             force="sel" if cur else None, right=edge)
            elif kind == "prov":
                self.putsegs(y, x, row_segs(kind, payload, self.cat, (),
                                            self.keyed(payload)),
                             force="sel" if cur else None, right=edge)
            else:
                if kind == "keyuse":             # filled in by a background fetch
                    payload = (payload[0], self.keyuse.get(payload[1]))
                self.putsegs(y, x, row_segs(kind, payload, self.cat,
                                            {r.get("model") for r in self.cat.get("picker_lineup", [])}),
                             force="sel" if cur else None, right=edge)
            y += 1

        # scroll position, drawn on the border so it costs no row
        if len(self.rows) > inner_h:
            frac = self.top / float(max(1, len(self.rows) - inner_h))
            self.put(1 + int(frac * (inner_h - 1)), list_w - 1, "\u2503", attr("sel"))

        if det_w:
            self.detail(0, list_w + 1, panel_h, det_w)

        kind = self.current()[0]
        keys = {"menu": [("[ ]", "move"), ("enter", "start here"), ("d", "remove")],
                "pol": [("enter", "data"), ("t", "tier"), ("a", "add folder"), ("d", "forget")],
                "model": [("space", "menu on/off"), ("enter", "edit"), ("a", "add"), ("d", "del")],
                }.get(kind, [("enter", "sign in"), ("a", "add key"), ("l", "login")])
        if self.status:
            self.put(h - 2, 2, short(self.status, w - 4), attr("note"))
        self.footer(h - 1, w, [("\u2191\u2193", "move")] + keys + [("s", "save all"), ("q", "quit")])
        self.scr.refresh()

    def detail(self, top, left, height, width):
        kind, payload = self.current()
        iw = width - 4
        if kind == "model":
            m = payload
            self.box(top, left, height, width, short(m["id"], width - 6), False)
            y = top + 2
            pairs = [("name", m.get("name", "")), ("tier", m.get("tier", "?")),
                     ("price", price_of(m)),
                     ("context", "{:,}".format(m.get("context", 0))),
                     ("vendor id", m.get("upstream_id") or "same as id"),
                     ("key", key_of(m) if routes_openrouter(m) else "\u2014 plan"),
                     ("in picker", "yes" if m["id"] in self.picker_set()
                      else ("n/a \u2014 native" if not publishable(m["id"]) else "no"))]
            for k, v in pairs:
                if y >= top + height - 1:
                    return
                self.put(y, left + 2, pad(k, 11), attr("tier"))
                self.put(y, left + 13, short(v, iw - 11), attr("name"), left + width - 1)
                y += 1
            if m.get("specialties") and y < top + height - 2:
                y += 1
                self.put(y, left + 2, "\u2605 ", attr("star"))
                self.put(y, left + 4, short(", ".join(m["specialties"]), iw - 2),
                         attr("name"), left + width - 1)
                y += 1
            if m.get("good_at") and y < top + height - 2:
                y += 1
                self.put(y, left + 2, "GOOD AT", attr("head"))
                y += 1
                for line in self.wrap(m["good_at"], iw):
                    if y >= top + height - 1:
                        break
                    self.put(y, left + 2, line, attr("note"), left + width - 1)
                    y += 1
        elif kind in ("prov", "up"):
            name = payload if kind == "prov" else payload.get("label", payload["name"])
            src = (self.cat["providers"][payload] if kind == "prov" else payload)
            self.box(top, left, height, width, name, False)
            y = top + 2
            for k, v in (("host", src.get("host", "?")),
                         ("path", src.get("path_prefix") or "/"),
                         ("key file", src.get("key_file") or "\u2014"),
                         ("auth", src.get("auth_header") or "\u2014"),
                         ("oauth", "yes" if src.get("oauth") else "no")):
                if y >= top + height - 1:
                    return
                self.put(y, left + 2, pad(k, 11), attr("tier"))
                self.put(y, left + 13, short(v, iw - 11), attr("name"), left + width - 1)
                y += 1
            note = src.get("detail") or src.get("note") or src.get("console_hint")
            if note and y < top + height - 2:
                y += 1
                for line in self.wrap(note, iw):
                    if y >= top + height - 1:
                        break
                    self.put(y, left + 2, line, attr("note"), left + width - 1)
                    y += 1
        else:
            self.box(top, left, height, width, "", False)

    def footer(self, y, w, pairs):
        x = 2
        for i, (k, label) in enumerate(pairs):
            if i:
                x = self.put(y, x, "  ·  ", attr("rule"))
            x = self.put(y, x, k, attr("key"))
            x = self.put(y, x, " " + label, attr("meta"))
            if x >= w - 2:
                return

    # ---------- input ----------

    def prompt(self, label, initial=""):
        buf = list(initial)
        pos = len(buf)
        curses.curs_set(1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self.put(h - 2, 0, " " * max(0, w - 1))
                x = self.put(h - 2, 2, label + " ", attr("key"))
                self.put(h - 2, x, "".join(buf), attr("name"))
                try:
                    self.scr.move(h - 2, min(w - 2, x + pos))
                except curses.error:
                    pass
                self.scr.refresh()
                c = self.scr.getch()
                if c == 27:
                    return None
                if c in (10, 13, curses.KEY_ENTER):
                    return "".join(buf)
                if c in (curses.KEY_BACKSPACE, 127, 8):
                    if pos:
                        del buf[pos - 1]
                        pos -= 1
                elif c == curses.KEY_DC:
                    if pos < len(buf):
                        del buf[pos]
                elif c == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif c == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif c == curses.KEY_HOME:
                    pos = 0
                elif c == curses.KEY_END:
                    pos = len(buf)
                elif c == curses.KEY_RESIZE:
                    continue
                elif 32 <= c < 127:
                    buf.insert(pos, chr(c))
                    pos += 1
        finally:
            curses.curs_set(0)

    def confirm(self, question):
        return (self.prompt(question + " [y/N]", "") or "").strip().lower() in ("y", "yes")

    def shell_out(self, argv, pause=True):
        """Drop out of curses so a normal command can own the terminal."""
        curses.def_prog_mode()
        curses.endwin()
        print("\n$ " + " ".join(argv))
        try:
            rc = subprocess.call(argv)
        except Exception as e:
            print("failed: %s" % e)
            rc = 1
        if pause:
            try:
                input("\n[enter] back to Switchboard ")
            except (EOFError, KeyboardInterrupt):
                pass
        curses.reset_prog_mode()
        self.scr.redrawwin()
        self.scr.refresh()
        return rc

    # ---------- actions ----------

    def toggle_pick(self, m):
        mid = m["id"]
        if not publishable(mid):
            self.status = "%s is a Claude id — already in the picker natively" % mid
            return
        picks = self.picker_set()
        if mid in picks:
            picks.remove(mid)
            self.status = "%s removed from the picker" % mid
        else:
            picks.append(mid)
            self.status = "%s added to the picker" % mid
        self.dirty = True

    def edit_model(self, m):
        fsel = 0
        fields = FIELDS + ([("upstream", "Key", "key")] if routes_openrouter(m) else [])
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 2, m["id"], attr("title"))
            self.put(1, 2, "─" * max(0, w - 5), attr("rule"))
            y = 3
            for i, (key, label, kind) in enumerate(fields):
                val = m.get(key)
                if kind == "key":
                    val = key_of(m) + ("" if m.get("upstream") else "  (default)")
                elif kind == "list":
                    val = ", ".join(val or [])
                val = "" if val is None else str(val)
                if i == fsel:
                    self.put(y, 1, "▌", attr("sel"))
                self.put(y, 3, pad(label, 14), attr("sel" if i == fsel else "tier"))
                self.put(y, 17, short(val, max(10, w - 22)) or "—",
                         attr("name" if val else "off"))
                y += 1
            y += 1
            for label, val in (("id", m["id"]),
                               ("context", "{:,}".format(m.get("context", 0))),
                               ("price", price_of(m)),
                               ("vendor id", m.get("upstream_id") or "(same as id)")):
                self.put(y, 3, pad(label, 14), attr("note"))
                self.put(y, 17, str(val), attr("note"))
                y += 1
            self.footer(h - 1, w, [("↑↓", "field"), ("enter", "edit / next key"),
                                   ("esc", "back")])
            self.scr.refresh()
            c = self.scr.getch()
            if c in (27, ord("q")):
                return
            if c in (curses.KEY_UP, ord("k")):
                fsel = max(0, fsel - 1)
            elif c in (curses.KEY_DOWN, ord("j")):
                fsel = min(len(fields) - 1, fsel + 1)
            elif c in (10, 13, curses.KEY_ENTER):
                key, label, kind = fields[fsel]
                if kind == "key":
                    cycle_key(self.cat, m)
                    self.dirty = True
                    continue
                cur = m.get(key)
                cur = ", ".join(cur or []) if kind == "list" else ("" if cur is None
                                                                  else str(cur))
                new = self.prompt(label + ":", cur)
                if new is None:
                    continue
                if kind == "list":
                    vals = [x.strip() for x in new.split(",") if x.strip()]
                    probs = fallback_problems(self.cat, vals) if key == "failover" else []
                    if probs:
                        self.put(h - 3, 3, short("\u26a0 not set: " + probs[0], w - 6), attr("bad"))
                        self.scr.getch()
                        continue
                    if vals:
                        m[key] = vals
                    else:
                        m.pop(key, None)
                else:
                    if new.strip():
                        m[key] = new.strip()
                    else:
                        m.pop(key, None)
                self.dirty = True

    def add_model(self):
        """Pick from OpenRouter's whole list; type an id only if it can't be fetched."""
        try:
            listing = list(self.sb.or_models().values())
        except Exception as e:
            self.status = "couldn't fetch OpenRouter's list (%s)" % e
            listing = None
        mid = self.browse(listing) if listing else self.prompt(
            "model id to add (looked up on OpenRouter):", "")
        if not mid or not mid.strip():
            return
        self.shell_out([sys.executable, os.path.join(HERE, "switchboard.py"),
                        "add", mid.strip()])
        self.cat = self.sb.load()
        self.rebuild()
        self.status = "reloaded after add"

    def add_upstream(self):
        """A new OpenRouter key: an upstreams row, then the usual sign-in to fill it."""
        name = self.prompt("key name (e.g. gemma -> openrouter-gemma):", "")
        if not name or not name.strip():
            return
        try:
            u = new_upstream(self.cat, name.strip())
        except ValueError as e:
            self.status = "not added: %s" % e
            return
        label = self.prompt("label:", u["label"])
        kf = self.prompt("key file:", u["key_file"])
        if label is None or kf is None:
            self.status = "cancelled"
            return
        u["label"], u["key_file"] = label.strip() or u["label"], kf.strip() or u["key_file"]
        self.cat.setdefault("upstreams", []).append(u)
        self.dirty = True
        self.rebuild()
        self.sel = next(i for i, r in enumerate(self.rows) if r[0] == "up" and r[1] is u)
        self.login_upstream(u)
        self.status = "%s added -- not saved yet; pick it per model under Key" % u["name"]

    def browse(self, listing):
        """Full-screen filter over OpenRouter's models. Returns an id, or None."""
        have = {m["id"] for m in self.cat.get("models", [])}
        q, sel, top, shown = "", 0, 0, None
        while True:
            rows = self.sb.browse_rows(listing, q, have)
            sel = max(0, min(sel, len(rows) - 1))
            # A new filter moves every row. ncurses' diff against what it thinks is
            # on screen left stale characters behind, so repaint in full then.
            if q != shown:
                self.scr.clear()
                shown = q
            else:
                self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 2, "Add a model", attr("title"))
            self.put(0, 15, "%d of %d on OpenRouter" % (len(rows), len(listing)), attr("note"))
            self.put(1, 2, "filter ", attr("tier"))
            self.put(1, 9, q + "\u258f", attr("name"))
            self.put(2, 2, "\u2500" * max(0, w - 5), attr("rule"))
            body = h - 5
            top = min(max(top, sel - body + 1), sel)
            for i, r in enumerate(rows[top:top + body]):
                y, cur = 3 + i, top + i == sel
                pr = ("$%s/$%s" % (round(r["price"][0], 3), round(r["price"][1], 3))
                      if r["price"] else "varies")
                ctx = "%dK" % ((r["context"] or 0) // 1000)
                segs = [("\u2713  " if r["added"] else "   ", "ok"),
                        (pad(short(r["id"], 44), 46), "id"), (pad(pr, 17), "price"),
                        (pad(ctx, 7), "tier"), (r["name"], "name")]
                if cur:
                    self.put(y, 1, "\u258c", attr("sel"))
                self.putsegs(y, 3, segs, force="sel" if cur else None, right=w - 1)
            if not rows:
                self.put(3, 3, "nothing matches", attr("note"))
            self.footer(h - 1, w, [("type", "filter"), ("\u2191\u2193", "move"),
                                   ("enter", "add"), ("esc", "back")])
            self.scr.refresh()
            c = self.scr.getch()
            if c == 27:
                return None
            if c == curses.KEY_UP:
                sel -= 1
            elif c == curses.KEY_DOWN:
                sel += 1
            elif c in (curses.KEY_BACKSPACE, 127, 8):
                q, sel = q[:-1], 0
            elif c in (10, 13, curses.KEY_ENTER) and rows:
                if rows[sel]["added"]:
                    self.status = "%s is already in the catalog" % rows[sel]["id"]
                    continue
                return rows[sel]["id"]
            elif 32 <= c < 127:
                q, sel = q + chr(c), 0

    def add_policy(self):
        path = self.prompt("folder (e.g. ~/Documents/Goldman):", "~/Documents/")
        if not path or not path.strip():
            return
        full = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isdir(full):
            self.status = "no such folder: %s" % full
            return
        home = os.path.expanduser("~")
        key = "~" + full[len(home):] if full.startswith(home + "/") else full
        self.cat.setdefault("folder_policy", {}).setdefault(key, {"data": "zdr"})
        self.dirty = True
        self.rebuild()
        self.status = "%s added as zdr -- enter cycles it" % key

    def delete_model(self, m):
        if not self.confirm("remove %s from the catalog?" % m["id"]):
            return
        self.cat["models"] = [x for x in self.cat["models"] if x["id"] != m["id"]]
        picks = self.picker_set()
        if m["id"] in picks:
            picks.remove(m["id"])
        self.dirty = True
        self.rebuild()
        self.status = "%s removed — not saved yet" % m["id"]

    def login(self, name):
        p = self.cat["providers"][name]
        # An accidental enter used to drop straight into a blocking key prompt
        # with no way back. Ask first; esc or n returns to the list.
        if not self.confirm("sign in to %s?" % name):
            self.status = "sign-in cancelled"
            return
        argv = [sys.executable, os.path.join(HERE, "switchboard.py"), "login", name]
        if p.get("oauth") and self.confirm("use the browser OAuth flow?"):
            argv.append("--oauth")
        self.shell_out(argv)
        self.cat = self.sb.load()
        self.rebuild()
        self.status = "back from login"

    def login_upstream(self, u):
        """Sign in to a gateway that is just a key file, e.g. OpenRouter."""
        kf = u.get("key_file")
        if not kf:
            self.status = "%s needs no key here" % u.get("label", u["name"])
            return
        if not self.confirm("sign in to %s?" % u.get("label", u["name"])):
            self.status = "sign-in cancelled"
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            key = self.sb.prompt_key(u["name"], u)
            if key:
                ok, why = verify_gateway_key(u, key)
                if ok or input("%s Store it anyway? [y/N] " % why).strip().lower() \
                        in ("y", "yes"):
                    path = os.path.expanduser(kf)
                    d = os.path.dirname(path)
                    if d:
                        os.makedirs(d, exist_ok=True)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(key)
                    os.chmod(path, 0o600)
                    print("wrote %s (0600) -- %s" % (path, why))
                else:
                    print("not stored")
            input("\n[enter] back to Switchboard ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            curses.reset_prog_mode()
            self.scr.redrawwin()
            self.scr.refresh()
        self.status = "back from %s sign-in" % u["name"]

    def on_mouse(self):
        """Click a row to select it; wheel scrolls. Clicking the marker toggles."""
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return
        up = getattr(curses, "BUTTON4_PRESSED", 0)
        down = getattr(curses, "BUTTON5_PRESSED", 0)
        if up and (bstate & up):
            self.move(-1)
            return
        if down and (bstate & down):
            self.move(1)
            return
        idx = self.top + (my - 1)            # row 0 is the panel border
        if 0 <= idx < len(self.rows) and self.selectable(idx):
            same = (idx == self.sel)
            self.sel = idx
            kind, payload = self.rows[idx]
            # A second click on an already-selected model row toggles the pick,
            # which is what the checkbox looks like it should do.
            if same and kind == "model" and mx <= 6:
                self.toggle_pick(payload)

    def save(self):
        """One key for everything the CLI does in four: write the catalog, publish
        the picker cache, write the /model menu, set the start model, restart the router."""
        self.sb.save(self.cat)
        sb_py = os.path.join(HERE, "switchboard.py")
        steps = [("sync", [sys.executable, os.path.join(HERE, "sync-models.py")]),
                 ("menu", [sys.executable, sb_py, "apply"])]
        if self.new_primary and self.new_primary != self.primary:
            steps.append(("start model", [sys.executable, sb_py, "route", "--primary", self.new_primary]))
        steps.append(("router restart", [sys.executable, sb_py, "restart"]))
        failed = []
        for name, argv in steps:
            r = subprocess.run(argv, capture_output=True, text=True)
            if r.returncode:
                failed.append("%s: %s" % (name, (r.stderr or r.stdout).strip().splitlines()[-1:] or "exit %d" % r.returncode))
        if self.new_primary:
            self.primary, self.new_primary = self.new_primary, None
        self.dirty = bool(failed)
        self.refresh_spend()
        self.rebuild()
        self.status = ("saved: catalog, picker, /model menu, router restarted -- restart Claude Code to see the menu"
                       if not failed else "saved the catalog, but " + "; ".join(str(f) for f in failed))

    # ---------- loop ----------

    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        init_colors()
        # Wheel-down (BUTTON5) is absent from some ncurses builds -- macOS's among
        # them -- so every constant is looked up rather than assumed.
        mask = 0
        for nm in ("BUTTON1_CLICKED", "BUTTON1_PRESSED", "BUTTON4_PRESSED",
                   "BUTTON5_PRESSED"):
            mask |= getattr(curses, nm, 0)
        try:
            curses.mousemask(mask)
        except curses.error:
            pass
        if not any(self.selectable(i) for i in range(len(self.rows))):
            return 0
        if not self.selectable(self.sel):
            self.move(1)
        while True:
            self.draw()
            c = self.scr.getch()
            kind, payload = self.current()
            if c in (curses.KEY_UP, ord("k")):
                self.move(-1)
            elif c in (curses.KEY_DOWN, ord("j")):
                self.move(1)
            elif c == curses.KEY_RESIZE:
                continue
            elif c == curses.KEY_MOUSE:
                self.on_mouse()
                continue
            elif c == ord(" ") and kind in ("model", "menu"):
                mid = payload["id"] if kind == "model" else payload[1]["model"]
                menu_toggle(self.cat, mid)
                self.dirty = True
                self.status = "%s %s the /model menu" % (mid, "added to" if in_menu(self.cat, mid) else "taken off")
                self.rebuild()
                if kind == "model":          # the menu grew or shrank above: stay on the model
                    self.sel = next((i for i, r in enumerate(self.rows)
                                     if r[0] == "model" and r[1]["id"] == mid), self.sel)
            elif c in (ord("["), ord("]")) and kind == "menu":
                mid = payload[1]["model"]
                menu_move(self.cat, mid, -1 if c == ord("[") else 1)
                self.dirty = True
                self.rebuild()
                self.sel = next(i for i, r in enumerate(self.rows) if r[0] == "menu" and r[1][1]["model"] == mid)
            elif c in (10, 13, curses.KEY_ENTER) and kind == "menu":
                self.new_primary = payload[1]["model"]
                self.dirty = True
                self.status = "new sessions will start on %s once saved" % self.new_primary
                self.rebuild()
            elif c in (10, 13, curses.KEY_ENTER) and kind == "pol":
                key = payload[0]
                before = dict(self.cat["folder_policy"].get(key, {}))
                cycle_policy(self.cat, key, "data")
                old = before.get("data") or self.cat["folder_policy"].get("_default", {}).get("data", "any")
                new = self.cat["folder_policy"][key]["data"]
                # One stray enter must never widen where a private folder's data can
                # go: loosening asks, tightening doesn't.
                if loosens(old, new) and not self.confirm(
                        "loosen %s from %s to %s? its data could then leave for %s"
                        % (key, old, new, "any provider" if new == "any" else "ZDR routes")):
                    self.cat["folder_policy"][key] = before
                    self.status = "%s kept at %s" % (key, old)
                else:
                    self.dirty = True
                self.rebuild()
            elif c == ord("t") and kind == "pol":
                cycle_policy(self.cat, payload[0], "tier")
                self.dirty = True
                self.rebuild()
            elif c == ord("a") and kind == "pol":
                self.add_policy()
            elif c == ord("d") and kind == "pol" and payload[0] != "_default":
                self.cat["folder_policy"].pop(payload[0], None)
                self.dirty = True
                self.status = "%s now follows _default" % payload[0]
                self.rebuild()
            elif c == ord("d") and kind == "menu":
                menu_toggle(self.cat, payload[1]["model"])
                self.dirty = True
                self.rebuild()
            elif c == ord("r"):
                self.refresh_spend()
                self.rebuild()
                self.status = "spend refreshed"
            elif c in (10, 13, curses.KEY_ENTER):
                if kind == "model":
                    self.edit_model(payload)
                elif kind == "prov":
                    self.login(payload)
                elif kind == "up":
                    self.login_upstream(payload)
            elif c == ord("l"):
                if kind == "prov":
                    self.login(payload)
                elif kind == "up":
                    self.login_upstream(payload)
            elif c == ord("a"):
                if kind in ("up", "prov"):
                    self.add_upstream()
                else:
                    self.add_model()
            elif c == ord("d") and kind == "model":
                self.delete_model(payload)
            elif c == ord("s"):
                self.save()
            # ESC is deliberately not bound: keypad(True) makes every arrow an
            # escape sequence, and a split one returns a bare 27 -- quitting on
            # that would discard unsaved edits on a keystroke meaning "move down".
            elif c == ord("q"):
                if self.dirty and not self.confirm("unsaved changes — quit anyway?"):
                    continue
                return 0


def run(sb):
    if not sys.stdout.isatty():
        print("the editor needs a terminal; use the subcommands instead "
              "(switchboard --help)", file=sys.stderr)
        return 1
    try:
        return curses.wrapper(lambda scr: UI(sb, scr).loop())
    except KeyboardInterrupt:
        # curses.wrapper has already restored the terminal by here. Ctrl-C is a
        # normal way to leave a full-screen app; a traceback is not an answer.
        print("interrupted — nothing saved" if _DIRTY[0] else "bye")
        return 130
