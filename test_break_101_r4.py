"""Breaker round 4 on PR #101 (head e1ba631). Fakes only, no network, no real tokens."""
import json, os, sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import switchboard as sb      # noqa: E402
import router as r            # noqa: E402

REAL_POST = sb.oauth_post
OLD = "sk-old-SECRET-a1"
NEW = "sk-new-SECRET-b2"
RT1 = "rt-SECRET-c3"


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = {"sent": [], "status": 200, "reply": {"access_token": NEW, "refresh_token": RT1}}

    def post(oa, path, fields):
        st["sent"].append(fields.get("refresh_token"))
        return st["status"], st["reply"]

    monkeypatch.setattr(sb, "oauth_post", post)
    monkeypatch.setattr(r, "_renew_failed", {})
    tf = tmp_path / "kimi-oauth.json"
    tf.write_text(json.dumps({"access_token": OLD, "refresh_token": RT1}))
    kf = tmp_path / "kimi.key"
    kf.write_text(OLD)
    st["prov"] = {"oauth": {"host": "http://127.0.0.1:9", "client_id": "c",
                            "token_path": "/t", "token_file": str(tf)},
                  "key_file": str(kf)}
    st["tf"], st["kf"] = tf, kf
    return st


# B1: renew_key's save-failure branch still formats str(e) into stderr and the reason.
# A lone surrogate in the reply's access_token passes _is_token and fails the key write.
def test_save_failure_reason_carries_server_codepoint(env, capsys):
    env["reply"] = {"access_token": "\ud800" + "tok", "refresh_token": "rt-new"}
    assert r.renew_key("kimi", env["prov"], OLD) is None
    reason = r._renew_failed["kimi"]["reason"]
    err = capsys.readouterr().err
    assert "\\ud800" not in reason and "surrogate" not in reason and "codec" not in reason, reason
    assert "\\ud800" not in err, err


# B2 (weak): a surrogate refresh_token passes _is_token, is stored, and can never be encoded
# for the POST. The failure is classed transient, so it is never marked dead.
def test_surrogate_refresh_token_is_not_stuck_transient(env, monkeypatch):
    env["reply"] = {"access_token": NEW, "refresh_token": "\ud800"}
    assert r.renew_key("kimi", env["prov"], OLD) == NEW
    monkeypatch.setattr(sb, "oauth_post", REAL_POST)   # real urlencode, closed port: no network
    for _ in range(3):
        assert r.renew_key("kimi", env["prov"], NEW) is None
    assert r._renew_failed["kimi"]["dead"] is True, r._renew_failed["kimi"]


# B3 (gap): a zero-width-only access_token is non-blank by the plan's literal predicate and is
# accepted, written to the key file, and sent as the bearer.
@pytest.mark.parametrize("tokval", ["​", "​​", "﻿", "⁠"])
def test_zero_width_access_token_is_not_a_token(env, tokval):
    env["reply"] = {"access_token": tokval, "refresh_token": RT1}
    assert sb.oauth_refresh("kimi", env["prov"], {}) is None
