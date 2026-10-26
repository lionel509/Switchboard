"""Breaker round 3 for PR #101 at head 67a63fa. Hermetic: oauth_post is faked."""
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
@pytest.mark.parametrize("stored", ["   ", "\n"])
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


# ---- redaction on the failed-POST branch ----
def test_redaction_removed_is_caught_by_plain_secret(fake):
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant",
                     "error_description": "refresh_token %s revoked" % RT1}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert RT1 not in why["reason"]


def test_url_encoded_refresh_token_is_redacted(fake):
    rt = "rt+SECRET/e=5"
    fake["tf"].write_text(json.dumps({"access_token": OLD, "refresh_token": rt}))
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant",
                     "error_description": "token %s is revoked" % urllib.parse.quote_plus(rt)}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert "SECRET" not in why["reason"], "url-encoded token in reason: %r" % why["reason"]


def test_double_json_escaped_refresh_token_is_redacted(fake):
    rt = 'rt"SECRET\\e5'
    fake["tf"].write_text(json.dumps({"access_token": OLD, "refresh_token": rt}))
    fake["status"] = 400
    # server embeds its own JSON-escaped copy of the token inside error_description
    fake["reply"] = {"error": "invalid_grant",
                     "error_description": "bad token " + json.dumps(rt)}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert "SECRET" not in why["reason"], "escaped token in reason: %r" % why["reason"]


def test_token_straddling_cut_is_redacted(fake):
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant",
                     "error_description": "x" * 185 + RT1 + " tail"}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert RT1 not in why["reason"] and len(why["reason"]) <= 200


def test_partial_token_prefix_at_cut_is_not_shown(fake):
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant",
                     "error_description": "x" * 185 + RT1[:9]}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert RT1[:4] not in why["reason"], "token prefix shown: %r" % why["reason"][-12:]


def test_huge_error_body_redacted_and_bounded(fake):
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant",
                     "error_description": "y" * 2_000_000 + RT1}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert RT1 not in why["reason"] and len(why["reason"]) <= 200


def test_access_token_echo_is_redacted(fake):
    fake["status"] = 400
    fake["reply"] = {"error": "invalid_grant", "error_description": "old %s bad" % OLD}
    why = {}
    sb.oauth_refresh("kimi", fake["prov"], why)
    assert OLD not in why["reason"]


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
