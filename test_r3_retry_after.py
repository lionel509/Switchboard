import time
import router as r

KIMI = "kimi/k2.8"


class FakeResp:
    def __init__(self, status, headers):
        self.status, self.h = status, headers

    def getheader(self, k, d=None):
        return self.h.get(k, d)


def test_retry_after_http_date_is_honoured():
    now = time.time()
    resp = FakeResp(429, {"retry-after": time.strftime(
        "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(now + 60))})
    r.mark_429(KIMI, resp, now=now)
    assert r.SKIP_UNTIL[KIMI] <= now + 61


def test_to_epoch_honours_utc_offset():
    assert r.to_epoch("2026-01-01T09:00:00+08:00") == r.to_epoch("2026-01-01T01:00:00Z")
