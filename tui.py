#!/usr/bin/env python3
"""Full-screen editor for the Switchboard catalog.

`switchboard` with no subcommand lands here. The CLI still does everything this
does -- this exists because the CLI equivalents are long: `add` alone carries ten
flags, and putting a model in the picker is three chained commands. Editing what
you can see is a different job from scripting it, and both are worth having.

curses from the standard library, to keep the repo dependency-free.
"""
import curses
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Fields exposed in the detail editor: (key, label, kind)
FIELDS = [
    ("name",        "Picker name",  "text"),
    ("blurb",       "Blurb",        "text"),
    ("tier",        "Tier",         "text"),
    ("good_at",     "Good at",      "text"),
    ("specialties", "Specialties",  "list"),
    ("source",      "Evidence",     "text"),
]


def price_of(m):
    """The cost column, in the same language as `switchboard list`."""
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


def row_text(kind, payload, cat, picks):
    """The text of one row. Pure, so column alignment can be tested without a tty."""
    if kind in ("head", "blank", "note"):
        return payload
    if kind == "prov":
        p = cat["providers"][payload]
        return "%-10s %-30s %-13s%s" % (
            payload, p.get("host", "?") + (p.get("path_prefix") or ""),
            p.get("_keyed_label", ""), " oauth" if p.get("oauth") else "")
    m = payload
    mid = m["id"]
    box = ("[x]" if mid in picks else "[ ]") if publishable(mid) else " - "
    return "%s %-34s %-7s %-15s %s" % (box, short(mid, 34), m.get("tier", "?"),
                                       price_of(m), m.get("name", ""))


class UI(object):
    def __init__(self, sb, scr):
        self.sb = sb
        self.scr = scr
        self.cat = sb.load()
        self.sel = 0
        self.top = 0
        self.dirty = False
        self.status = "loaded %s" % os.path.join(HERE, "models.json")
        self.rows = []
        self.rebuild()

    # ---------- model ----------

    def picker_set(self):
        return self.cat.setdefault("picker", {}).setdefault("models", [])

    def rebuild(self):
        """Flatten providers + models into one navigable list."""
        rows = [("head", "PROVIDERS")]
        provs = self.cat.get("providers", {})
        if not provs:
            rows.append(("note", "  none declared"))
        for name in sorted(provs):
            rows.append(("prov", name))
        rows.append(("blank", ""))
        rows.append(("head", "MODELS"))
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
        return self.rows[i][0] in ("prov", "model")

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

    def put(self, y, x, text, attr=0):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        A = curses.A_BOLD
        hdr = " Switchboard%s" % ("  *unsaved*" if self.dirty else "")
        right = "router: %s   fallback: %s " % (
            short(self.cat.get("router_model", "?")), short(self.cat.get("fallback", "?")))
        self.put(0, 0, hdr.ljust(max(0, w - len(right) - 1)) + right, A | curses.A_REVERSE)

        body_h = h - 3
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + body_h:
            self.top = self.sel - body_h + 1

        y = 1
        for i in range(self.top, min(len(self.rows), self.top + body_h)):
            kind, payload = self.rows[i]
            cur = (i == self.sel)
            mark = curses.A_REVERSE if cur else 0
            if kind == "head":
                self.put(y, 1, payload, A)
            elif kind in ("blank", "note"):
                self.put(y, 1, payload)
            elif kind == "prov":
                p = self.cat["providers"][payload]
                p["_keyed_label"] = ("OK signed in"
                                     if self.sb.read_keyfile(p.get("key_file", ""))
                                     else "-- no key")
                self.put(y, 2, row_text(kind, payload, self.cat, ()), mark)
            else:
                m = payload
                self.put(y, 2, row_text(kind, m, self.cat, self.picker_set()), mark)
                if cur:
                    # Detail lines must respect the same bound as the list, or
                    # they run into the status row.
                    ga = (m.get("good_at") or "").replace("\n", " ")
                    extra = [("good at: " + short(ga, max(10, w - 22))) if ga else None,
                             ("* " + ", ".join(m["specialties"]))
                             if m.get("specialties") else None]
                    for line in [e for e in extra if e]:
                        if y + 1 >= 1 + body_h:
                            break
                        y += 1
                        self.put(y, 8, line, curses.A_DIM)
            y += 1
            if y >= 1 + body_h:
                break

        self.put(h - 2, 0, (" " + self.status).ljust(max(0, w - 1))[:max(0, w - 1)],
                 curses.A_DIM)
        keys = (" up/dn move  space pick  enter edit  a add  d del  "
                "l login  s save+sync  q quit")
        self.put(h - 1, 0, keys.ljust(max(0, w - 1))[:max(0, w - 1)], curses.A_REVERSE)
        self.scr.refresh()

    # ---------- input ----------

    def prompt(self, label, initial=""):
        """One-line editor on the status row. Returns None if cancelled."""
        buf = list(initial)
        pos = len(buf)
        curses.curs_set(1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = "".join(buf)
                self.put(h - 2, 0, (" " + label + " " + text).ljust(max(0, w - 1)),
                         curses.A_BOLD)
                try:
                    self.scr.move(h - 2, min(w - 2, 2 + len(label) + pos))
                except curses.error:
                    pass
                self.scr.refresh()
                c = self.scr.getch()
                if c in (27,):                       # esc
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
        a = self.prompt(question + " [y/N]", "")
        return (a or "").strip().lower() in ("y", "yes")

    def shell_out(self, argv, pause=True):
        """Drop out of curses so a normal command can own the terminal.

        Sign-in prompts, the OpenRouter lookup and sync all print and some of them
        read -- none of that can share a screen with curses.
        """
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
            self.status = "%s is a Claude id -- already in the picker natively" % mid
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
        """Field list for one model; enter edits, esc leaves."""
        fsel = 0
        while True:
            self.scr.erase()
            h, w = self.scr.getmaxyx()
            self.put(0, 0, (" " + m["id"]).ljust(max(0, w - 1)),
                     curses.A_BOLD | curses.A_REVERSE)
            y = 2
            for i, (key, label, kind) in enumerate(FIELDS):
                val = m.get(key)
                if kind == "list":
                    val = ", ".join(val or [])
                val = "" if val is None else str(val)
                self.put(y, 2, "%-13s %s" % (label + ":", short(val, max(10, w - 20))),
                         curses.A_REVERSE if i == fsel else 0)
                y += 1
            y += 1
            for line in ("id        %s" % m["id"],
                         "context   %s" % "{:,}".format(m.get("context", 0)),
                         "price     %s" % price_of(m),
                         "upstream  %s" % (m.get("upstream_id") or "(same as id)")):
                self.put(y, 2, line, curses.A_DIM)
                y += 1
            self.put(h - 1, 0, " up/dn field   enter edit   esc back"
                     .ljust(max(0, w - 1)), curses.A_REVERSE)
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
        self.cat = self.sb.load()          # add wrote the file; reload
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
        self.status = "%s removed -- not saved yet" % m["id"]

    def login(self, name):
        p = self.cat["providers"][name]
        argv = [sys.executable, os.path.join(HERE, "switchboard.py"), "login", name]
        if p.get("oauth") and self.confirm("use the browser OAuth flow?"):
            argv.append("--oauth")
        self.shell_out(argv)
        self.cat = self.sb.load()          # login may rewrite auth_header/prefix
        self.rebuild()
        self.status = "back from login"

    def save(self):
        self.sb.save(self.cat)
        self.dirty = False
        self.status = "saved"
        self.shell_out([sys.executable, os.path.join(HERE, "sync-models.py")])
        self.status = "saved and synced -- restart Claude Code for the picker"

    # ---------- loop ----------

    def loop(self):
        curses.curs_set(0)
        self.scr.keypad(True)
        try:
            curses.set_escdelay(25)     # 3.9+; keeps a real ESC snappy in overlays
        except (AttributeError, curses.error):
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
            elif c == ord(" ") and kind == "model":
                self.toggle_pick(payload)
            elif c in (10, 13, curses.KEY_ENTER):
                if kind == "model":
                    self.edit_model(payload)
                elif kind == "prov":
                    self.login(payload)
            elif c == ord("l") and kind == "prov":
                self.login(payload)
            elif c == ord("a"):
                self.add_model()
            elif c == ord("d") and kind == "model":
                self.delete_model(payload)
            elif c == ord("s"):
                self.save()
            elif c == ord("q"):
                if self.dirty and not self.confirm("unsaved changes -- quit anyway?"):
                    continue
                return 0


def short(s, n=28):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def run(sb):
    if not sys.stdout.isatty():
        print("the editor needs a terminal; use the subcommands instead "
              "(switchboard --help)", file=sys.stderr)
        return 1
    return curses.wrapper(lambda scr: UI(sb, scr).loop())
