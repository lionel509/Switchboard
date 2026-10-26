"""Breaker round 5 on PR #101 at ded862a. Hermetic: every network call is a fake."""
import importlib.util, io, json, os, sys, urllib.error
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sb = _load("switchboard")
SECRET_DEV = "SECRET-DEV-9f3"
RT_NEW = "rt-new-SECRET-7d"


@pytest.fixture
def device(tmp_path, monkeypatch):
    """Scripted device flow: replies[0] = device_authorization, replies[1:] = polls."""
    st = {"replies": [], "calls": 0}

    def post(oa, path, fields):
        i = st["calls"]
        st["calls"] += 1
        return st["replies"][min(i, len(st["replies"]) - 1)]

    monkeypatch.setattr(sb, "oauth_post", post)
    monkeypatch.setattr(sb, "open_url", lambda u: False)
    monkeypatch.setattr(sb.time, "sleep", lambda s: None)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    prov = {"oauth": {"host": "http://x.invalid", "client_id": "c",
                      "device_path": "/d", "token_path": "/t"},
            "key_file": str(tmp_path / "key")}
    st["prov"] = prov
    st["ok_auth"] = (200, {"device_code": "dc", "user_code": "U-1",
                           "verification_uri": "http://x.invalid/v",
                           "interval": 0, "expires_in": 30})
    return st


# 1. One predicate for every token: the device poll only checks truthiness.
@pytest.mark.parametrize("bad", ["   ", "​sk-live", "sk-live\nX-Injected: 1", "sk\x7f"])
def test_device_poll_rejects_bad_access_token(device, bad):
    device["replies"] = [device["ok_auth"],
                         (200, {"access_token": bad, "refresh_token": RT_NEW})]
    assert sb.oauth_login("kimi", device["prov"]) is None


# 2. Same predicate for the refresh token received at device login.
@pytest.mark.parametrize("bad", ["   ", "rt​", "rt\n"])
def test_device_poll_rejects_bad_refresh_token(device, bad):
    device["replies"] = [device["ok_auth"],
                         (200, {"access_token": "sk-live-ok", "refresh_token": bad})]
    assert sb.oauth_login("kimi", device["prov"]) is None


# 3. Server text must not reach stderr (device authorization failure line).
def test_device_auth_error_does_not_echo_server_text(device, capsys):
    device["replies"] = [(400, {"error": "invalid_client",
                                "error_description": "client %s unknown" % SECRET_DEV})]
    sb.oauth_login("kimi", device["prov"])
    assert SECRET_DEV not in capsys.readouterr().err


# 4. A reply with no error fields is printed whole (dict repr).
def test_device_auth_error_without_fields_does_not_dump_reply(device, capsys):
    device["replies"] = [(400, {"detail": "hidden %s" % SECRET_DEV})]
    sb.oauth_login("kimi", device["prov"])
    assert SECRET_DEV not in capsys.readouterr().err


# 5. Malformed 200 body on the poll: a JSON list must not crash login.
def test_device_poll_non_object_reply_does_not_crash(device):
    device["replies"] = [device["ok_auth"], (200, ["not", "an", "object"])]
    assert sb.oauth_login("kimi", device["prov"]) is None


# 6. Malformed interval from the server must not crash login.
def test_device_auth_non_numeric_interval_does_not_crash(device):
    device["replies"] = [(200, {"device_code": "dc", "interval": "soon",
                                "expires_in": "later"})]
    sb.oauth_login("kimi", device["prov"])


# 7. Provider probe detail is server text; cmd_login prints it on a 401 (": " + detail).
def test_probe_401_detail_is_not_server_text(monkeypatch):
    body = json.dumps({"error": {"message": "bad key %s" % SECRET_DEV}}).encode()

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)
    cat = {"models": [{"id": "kimi/k2", "upstream_id": "k2"}]}
    prov = {"host": "api.invalid", "path_prefix": "", "auth_header": "x-api-key"}
    status, detail = sb.provider_probe("kimi", prov, "sk-key", cat)
    assert status == 401
    assert SECRET_DEV not in str(detail)


# 8. Ordering: key-file write fails after the refresh token file is written.
#    Expect the rotated refresh token to be on disk. (Probe; expected to pass.)
def test_save_failure_keeps_new_refresh_token(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    tf = tmp_path / "kimi-oauth.json"
    prov = {"oauth": {"token_file": str(tf)}, "key_file": str(blocker / "key")}
    with pytest.raises(OSError):
        sb.save_oauth("kimi", prov, {"access_token": "sk-ok", "refresh_token": RT_NEW})
    assert json.loads(tf.read_text())["refresh_token"] == RT_NEW


# 9. Over-rejection probe: real-shaped tokens must still pass the predicate.
@pytest.mark.parametrize("tok", [
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln-_",
    "abc+def/ghi==", "rt-" + "A" * 4000, "sk-kimi-AbC123_-.~xyz"])
def test_real_shaped_tokens_accepted(tok):
    assert sb._is_token(tok)
