"""Signing functions: fixed-key HMAC regression digests, header/param placement."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode

from execution.auth import (
    binance_signature,
    binance_signed_request,
    bybit_signature,
    bybit_signed_request,
    kraken_signature,
    kraken_signed_request,
)

SECRET = "test-secret"
KEY = "test-api-key"


def expected_hmac_sha256_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def expected_kraken_sign(secret_b64: str, message: str) -> str:
    key = base64.b64decode(secret_b64)
    digest = hmac.new(key, message.encode(), hashlib.sha512).digest()
    return base64.b64encode(digest).decode()


# --- raw signature primitives -------------------------------------------------


def test_binance_signature_is_hmac_sha256_hex() -> None:
    query = "symbol=BTCUSDT&side=BUY&quantity=0.5"
    assert binance_signature(SECRET, query) == expected_hmac_sha256_hex(SECRET, query)


def test_bybit_signature_message_order() -> None:
    ts, recv = "1712345678901", "5000"
    payload = "category=spot&symbol=BTCUSDT"
    expected = expected_hmac_sha256_hex(SECRET, f"{ts}{KEY}{recv}{payload}")
    assert bybit_signature(SECRET, ts, KEY, recv, payload) == expected


def test_kraken_signature_base64_sha512() -> None:
    secret_b64 = base64.b64encode(b"kraken-private-key").decode()
    uri, nonce, postdata = "/0/private/AddOrder", "1234", "nonce=1234"
    expected = expected_kraken_sign(secret_b64, uri + nonce + postdata)
    assert kraken_signature(secret_b64, uri, nonce, postdata) == expected


# --- signed request builders -----------------------------------------------------


def test_binance_signed_request_places_signature_and_key_header() -> None:
    params, headers = binance_signed_request(KEY, SECRET, {"symbol": "BTCUSDT"}, timestamp_ms=123)
    assert params["timestamp"] == "123"
    assert params["recvWindow"] == "5000"
    assert params["symbol"] == "BTCUSDT"
    signed_query = urlencode({k: v for k, v in params.items() if k != "signature"})
    assert params["signature"] == binance_signature(SECRET, signed_query)
    assert headers == {"X-MBX-APIKEY": KEY}


def test_binance_signature_depends_on_every_param() -> None:
    p1, _ = binance_signed_request(KEY, SECRET, {"symbol": "BTCUSDT"}, timestamp_ms=1)
    p2, _ = binance_signed_request(KEY, SECRET, {"symbol": "ETHUSDT"}, timestamp_ms=1)
    assert p1["signature"] != p2["signature"]


def test_bybit_signed_request_headers_and_payload() -> None:
    payload, headers = bybit_signed_request(
        KEY, SECRET, {"symbol": "BTCUSDT"}, timestamp_ms=123, recv_window_ms=5000
    )
    assert payload == "symbol=BTCUSDT"
    assert headers["X-BAPI-APIKEY"] == KEY
    assert headers["X-BAPI-TIMESTAMP"] == "123"
    assert headers["X-BAPI-RECVWINDOW"] == "5000"
    assert headers["X-BAPI-SIGN"] == bybit_signature(SECRET, "123", KEY, "5000", payload)


def test_kraken_signed_request_nonce_and_signature() -> None:
    secret_b64 = base64.b64encode(b"kraken-private-key").decode()
    body, headers = kraken_signed_request(
        KEY, secret_b64, "/0/private/Balance", "asset=XBT", nonce=999
    )
    assert body == "nonce=999&asset=XBT"
    assert headers["API-Key"] == KEY
    assert headers["API-Sign"] == kraken_signature(secret_b64, "/0/private/Balance", "999", body)


def test_kraken_signed_request_empty_postdata() -> None:
    secret_b64 = base64.b64encode(b"k").decode()
    body, headers = kraken_signed_request(KEY, secret_b64, "/0/private/Balance", "", nonce=7)
    assert body == "nonce=7"
    assert headers["API-Sign"] == expected_kraken_sign(
        secret_b64, "/0/private/Balance" + "7" + body
    )


def test_signature_changes_with_key() -> None:
    p1, _ = binance_signed_request(KEY, "secret-one", {"a": "1"}, timestamp_ms=5)
    p2, _ = binance_signed_request(KEY, "secret-two", {"a": "1"}, timestamp_ms=5)
    assert p1["signature"] != p2["signature"]
