"""oauth_refresh safety (#100): one token-validity rule; failure reasons are fixed text. Hermetic: oauth_post is faked."""
import importlib.util, json, os, shlex, subprocess, urllib.parse
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sb = _load("switchboard")
r = _load("router")
OLD = "sk-old-SECRET-a1"
NEW = "sk-new-SECRET-b2"
RT1 = "rt-SECRET-c3"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Stub the OAuth POST; record what was sent; return a canned (status, reply)."""
    st = {"sent": [], "status": 200, "reply": {"access_token": NEW, "refresh_token": RT1}}

    def post(oa, path, fields):
        st["sent"].append(fields.get("refresh_token"))
        return st["status"], st["reply"]

    monkeypatch.setattr(sb, "oauth_post", post)
    tf = tmp_path / "kimi-oauth.json"
    tf.write_text(json.dumps({"access_token": OLD, "refresh_token": RT1}))
    prov = {"oauth": {"host": "http://x.invalid", "client_id": "c",
                      "token_path": "/t", "token_file": str(tf)}}
    st["prov"], st["tf"] = prov, tf
    return st


# ---- keep-or-rotate: values the plan lists ('', null, missing, whitespace) ----
@pytest.mark.parametrize("bad", ["", None, [], {}, 0, "   ", "\n\t "])
def test_non_token_refresh_token_in_reply_keeps_old(fake, bad):
    fake["reply"] = {"access_token": NEW, "refresh_token": bad}
    tok = sb.oauth_refresh("kimi", fake["prov"], {})
    assert tok is not None
    assert tok["refresh_token"] == RT1, "next refresh would send %r" % tok["refresh_token"]


def test_missing_refresh_token_in_reply_keeps_old(fake):
    fake["reply"] = {"access_token": NEW}
    assert sb.oauth_refresh("kimi", fake["prov"], {})["refresh_token"] == RT1


# whitespace-only stored refresh token: should count as no grant, not be sent.
@pytest.mark.parametrize("stored", ["   ", "\n", 5, ["rt"]])
def test_whitespace_stored_refresh_token_is_dead_not_sent(fake, stored):
    fake["tf"].write_text(json.dumps({"access_token": OLD, "refresh_token": stored}))
    why = {}
    assert sb.oauth_refresh("kimi", fake["prov"], why) is None
    assert fake["sent"] == [], "whitespace refresh token was POSTed: %r" % fake["sent"]
    assert why.get("dead") is True


# ---- stored-file type guard ----
@pytest.mark.parametrize("content", ["[]", "null", "5", '"str"', "true", "{}",
                                     '{"access_token": "x"}', ""])
def test_stored_file_type_guard_is_dead(fake, content):
    fake["tf"].write_text(content)
    why = {}
    assert sb.oauth_refresh("kimi", fake["prov"], why) is None
    assert why.get("dead") is True and fake["sent"] == []


def test_stored_file_non_utf8_is_dead(fake):
    fake["tf"].write_bytes(b'{"refresh_token": "\xff\xfe"}')
    why = {}
    assert sb.oauth_refresh("kimi", fake["prov"], why) is None
    assert why.get("dead") is True


# ---- (a) invariant: a failed refresh's reason is fixed text only ----
STATUSES = [None, 200, 400, 401, 403, 500, 503]
HOSTILE = [
    {"error": "invalid_grant", "error_description": "refresh_token %s revoked" % RT1},
    {"error": "invalid_grant", "error_description": "token " + urllib.parse.quote_plus(RT1)},
    {"error": "invalid_grant", "error_description": "bad token " + json.dumps(RT1)},
    {"error": "invalid_grant", "error_description": "x" * 185 + RT1[:9]},
    {"error": "invalid_grant", "error_description": "y" * 2_000_000 + RT1},
    {"error": "invalid_grant", "error_description": "old %s bad" % OLD},
    {"error": RT1},
    {"error": "rt_secret_lower"},      # passes ^[a-z_]{1,40}$; must still never appear
    {"error": ["invalid_grant"]}, {"error": {"k": RT1}}, {"error": 7},
    {"error": "unreachable", "error_description": "nodename " + RT1},
    {"access_token": "   ", "error_description": RT1},
    {}, [RT1], "SECRET", None,
]


def allowed_reasons():
    codes = set(sb.OAUTH_ERRORS) | {"unrecognised error"}
    out = {"OAuth host unreachable", "HTTP 200: no access_token in reply"}
    out |= {"HTTP %d: %s" % (s, c) for s in STATUSES if s not in (None, 200) for c in codes}
    return out


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("reply", HOSTILE, ids=[str(i) for i in range(len(HOSTILE))])
def test_failed_refresh_reason_is_fixed_text(fake, capsys, status, reply):
    fake["status"], fake["reply"] = status, reply
    why = {}
    assert sb.oauth_refresh("kimi", fake["prov"], why) is None
    assert why["reason"] in allowed_reasons(), why["reason"][:80]
    assert "secret" not in why["reason"].lower()
    assert "secret" not in capsys.readouterr().err.lower()


def test_known_error_code_is_reported(fake):
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant", "error_description": "anything"}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert why == {"dead": True, "reason": "HTTP 400: invalid_grant"}


# ---- (b) invariant: one validity rule for stored and received tokens ----
CANDIDATES = [("rt-ok-1", True), (" rt-ok-2 ", True), ("", False), ("   ", False),
              ("\n\t ", False), (None, False), (0, False), (5, False), (True, False),
              ([], False), ({}, False), (["rt"], False)]
CIDS = [repr(v) for v, _ in CANDIDATES]


@pytest.mark.parametrize("v,valid", CANDIDATES, ids=CIDS)
def test_stored_refresh_token_sent_iff_valid(fake, v, valid):
    fake["tf"].write_text(json.dumps({"access_token": OLD, "refresh_token": v}))
    why = {}
    tok = sb.oauth_refresh("kimi", fake["prov"], why)
    if valid:
        assert fake["sent"] == [v] and tok is not None
    else:
        assert fake["sent"] == [] and tok is None and why["dead"] is True


@pytest.mark.parametrize("v,valid", CANDIDATES, ids=CIDS)
def test_reply_refresh_token_rotates_iff_valid(fake, v, valid):
    fake["reply"] = {"access_token": NEW, "refresh_token": v}
    tok = sb.oauth_refresh("kimi", fake["prov"], {})
    assert tok["refresh_token"] == (v if valid else RT1)


@pytest.mark.parametrize("v,valid", CANDIDATES, ids=CIDS)
def test_reply_access_token_accepted_iff_valid(fake, v, valid):
    fake["reply"] = {"access_token": v}
    why = {}
    tok = sb.oauth_refresh("kimi", fake["prov"], why)
    if valid:
        assert tok["access_token"] == v
    else:
        assert tok is None and why == {"dead": False,
                                       "reason": "HTTP 200: no access_token in reply"}


# ---- relogin_cmd quoting ----
@pytest.mark.parametrize("name", ["My Projects", "it's", "$(touch PWNED)", "uni-码 ü", 'q"uote'])
def test_relogin_cmd_survives_shell(tmp_path, monkeypatch, name):
    marker = tmp_path / "PWNED"
    folder = tmp_path / name
    folder.mkdir()
    monkeypatch.setattr(r, "__file__", str(folder / "router.py"))
    cmd = r.relogin_cmd("kimi")
    parts = shlex.split(cmd)
    assert parts[1] == str(folder / "switchboard.py"), parts
    # run the quoted path through a real sh; it must print literally and not execute
    quoted = shlex.quote(str(folder / "switchboard.py"))
    out = subprocess.run(["sh", "-c", "printf '%%s' %s" % quoted],
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert out.stdout == str(folder / "switchboard.py")
    assert not marker.exists()
