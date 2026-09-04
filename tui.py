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
        return [(pad(u.get("label") or u["name"], 11), "id"), (pad(host, 30), "price"),
                (pad(state, 16), role)]
    if kind == "prov":
        p = cat["providers"][payload]
        host = p.get("host", "?") + (p.get("path_prefix") or "")
        state = "●  signed in" if keyed else "○  no key"
        return [(pad(payload, 11), "id"), (pad(host, 30), "price"),
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
    return [(box, box_role), (pad(short(mid, 34), 35), "id"),
            (pad(m.get("tier", "?"), 7), "tier"),
            (pad(price_of(m), 15), "price"), (m.get("name", ""), "name")]


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
        self.rebuild()

    # ---------- model ----------

    def picker_set(self):
        return self.cat.setdefault("picker", {}).setdefault("models", [])

    def keyed(self, name):
        p = self.cat["providers"][name]
        return bool(self.sb.read_keyfile(p.get("key_file", "")))

    def rebuild(self):
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
        rows += [("blank", ""), ("head", "MODELS")]
        by_family = {}
        for m in self.cat.get("models", []):
            by_family.setdefault(m.get("family", "?"), []).append(m)
        for fam in sorted(by_family):
            for m in sorted(by_family[fam], key=lambda x: (x.get("price") or [0])[0]):
                rows.append(("model", m))
        self.rows = rows
        if self.sel >= len(rows):
            self.sel = max(0, len(rows) - 1)

    def selectable(self, i):
        kind, payload = self.rows[i]
        if kind == "up":
            return not payload.get("fixed")      # nothing to do for a fixed row
        return kind in ("prov", "model")

    def move(self, delta):
        i = self.sel
        while True:
            i += delta
            if i < 0 or i >= len(self.rows):
                return
            if self.selectable(i):
                self.sel = i
                return

    def current(self):
        return self.rows[self.sel] if self.rows else ("blank", "")

    # ---------- drawing ----------

    def put(self, y, x, text, a=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return x
        try:
            self.scr.addnstr(y, x, text, max(0, w - x - 1), a)
        except curses.error:
            pass
        return x + len(text)

    def putsegs(self, y, x, segs, force=None):
        for text, role in segs:
            if not text:
                continue
            x = self.put(y, x, text, attr(force or role))
        return x

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()

        self.put(0, 2, "Switchboard", attr("title"))
        meta = "router %s   fallback %s" % (short(self.cat.get("router_model", "?"), 26),
                                            short(self.cat.get("fallback", "?"), 20))
        if self.dirty:
            self.put(0, 15, "unsaved", attr("warn"))
        if w > len(meta) + 26:
            self.put(0, w - len(meta) - 3, meta, attr("meta"))
        self.put(1, 2, "─" * max(0, w - 5), attr("rule"))

        body_top, body_h = 2, h - 4
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + body_h:
            self.top = self.sel - body_h + 1

        y = body_top
        for i in range(self.top, min(len(self.rows), self.top + body_h)):
            kind, payload = self.rows[i]
            cur = (i == self.sel)
            if cur:
                self.put(y, 1, "▌", attr("sel"))
            if kind == "head":
                self.put(y, 2, payload, attr("head"))
            elif kind in ("blank", "note"):
                self.put(y, 3, payload, attr("note"))
            elif kind == "up":
                kf = payload.get("key_file") or ""
                self.putsegs(y, 3, row_segs(kind, payload, self.cat, (),
                                            bool(self.sb.read_keyfile(kf))),
                             force="sel" if cur else None)
            elif kind == "prov":
                self.putsegs(y, 3, row_segs(kind, payload, self.cat, (),
                                            self.keyed(payload)),
                             force="sel" if cur else None)
            else:
                m = payload
                self.putsegs(y, 3, row_segs(kind, m, self.cat, self.picker_set()),
                             force="sel" if cur else None)
                if cur:
                    # Detail lines respect the same bound as the list, or they
                    # run into the status row.
                    ga = (m.get("good_at") or "").replace("\n", " ")
                    extra = []
                    if ga:
                        extra.append([(short(ga, max(10, w - 14)), "note")])
                    if m.get("specialties"):
                        extra.append([("★ ", "star"),
                                      (", ".join(m["specialties"]), "note")])
                    for segs in extra:
                        if y + 1 >= body_top + body_h:
                            break
                        y += 1
                        self.putsegs(y, 9, segs)
            y += 1
            if y >= body_top + body_h:
                break

        self.put(h - 2, 2, short(self.status, max(0, w - 5)), attr("meta"))
        self.footer(h - 1, w, [("↑↓", "move"), ("space", "pick"), ("enter", "edit"),
                               ("a", "add"), ("d", "del"), ("l", "login"),
                               ("s", "save"), ("q", "quit")])
        self.scr.refresh()

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
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 2, m["id"], attr("title"))
            self.put(1, 2, "─" * max(0, w - 5), attr("rule"))
            y = 3
            for i, (key, label, kind) in enumerate(FIELDS):
                val = m.get(key)
                if kind == "list":
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
                               ("upstream", m.get("upstream_id") or "(same as id)")):
                self.put(y, 3, pad(label, 14), attr("note"))
                self.put(y, 17, str(val), attr("note"))
                y += 1
            self.footer(h - 1, w, [("↑↓", "field"), ("enter", "edit"), ("esc", "back")])
            self.scr.refresh()
            c = self.scr.getch()
            if c in (27, ord("q")):
                return
            if c in (curses.KEY_UP, ord("k")):
                fsel = max(0, fsel - 1)
            elif c in (curses.KEY_DOWN, ord("j")):
                fsel = min(len(FIELDS) - 1, fsel + 1)
            elif c in (10, 13, curses.KEY_ENTER):
                key, label, kind = FIELDS[fsel]
                cur = m.get(key)
                cur = ", ".join(cur or []) if kind == "list" else ("" if cur is None
                                                                  else str(cur))
                new = self.prompt(label + ":", cur)
                if new is None:
                    continue
                if kind == "list":
                    vals = [x.strip() for x in new.split(",") if x.strip()]
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
        mid = self.prompt("model id to add (looked up on OpenRouter):", "")
        if not mid or not mid.strip():
            return
        self.shell_out([sys.executable, os.path.join(HERE, "switchboard.py"),
                        "add", mid.strip()])
        self.cat = self.sb.load()
        self.rebuild()
        self.status = "reloaded after add"

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

    def save(self):
        self.sb.save(self.cat)
        self.dirty = False
        self.shell_out([sys.executable, os.path.join(HERE, "sync-models.py")])
        self.status = "saved and synced — restart Claude Code for the picker"

    # ---------- loop ----------

    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        init_colors()
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
            elif c == ord(" ") and kind == "model":
                self.toggle_pick(payload)
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
    return curses.wrapper(lambda scr: UI(sb, scr).loop())
