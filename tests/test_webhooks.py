from app.webhooks import TransportResult, backoff, classify, sign, verify

BODY = b'{"referral_id":"ref_1"}'


def test_sign_and_verify_roundtrip():
    header = sign("secret", BODY, timestamp=1_000_000)
    assert verify("secret", BODY, header, now=1_000_010)


def test_verify_rejects_tampering_wrong_secret_and_replays():
    header = sign("secret", BODY, timestamp=1_000_000)
    assert not verify("secret", BODY + b" ", header, now=1_000_000)
    assert not verify("other", BODY, header, now=1_000_000)
    assert not verify("secret", BODY, header, now=1_000_000 + 301)   # outside replay window
    assert not verify("secret", BODY, None)
    assert not verify("secret", BODY, "garbage")


def test_classify():
    assert classify(TransportResult(200)) == "success"
    assert classify(TransportResult(204)) == "success"
    for code in (500, 502, 503, 408, 429, None):
        assert classify(TransportResult(code)) == "retry", code
    for code in (400, 401, 403, 404, 422):
        assert classify(TransportResult(code)) == "permanent", code


def test_backoff_grows_and_is_capped():
    lo = [backoff(n, rand=lambda: 0.0).total_seconds() for n in range(1, 6)]
    hi = [backoff(n, rand=lambda: 1.0).total_seconds() for n in range(1, 6)]
    assert lo == [15, 30, 60, 120, 240]
    assert hi == [30, 60, 120, 240, 480]
    assert backoff(30, rand=lambda: 1.0).total_seconds() == 3600
