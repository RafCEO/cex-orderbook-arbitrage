"""Exchange request signing: pure functions, data in -> headers/params out.

Binance: HMAC-SHA256 hex digest over the URL-encoded query string, appended
    as the `signature` param; key travels in the X-MBX-APIKEY header.
Bybit: HMAC-SHA256 hex digest of `timestamp + api_key + recv_window + payload`
    in the X-BAPI-SIGN header alongside key/timestamp/recv-window headers.
Kraken: HMAC-SHA512 over `uri + nonce + postdata` with the base64-decoded
    secret, base64-encoded, in the API-Sign header.

All functions are deterministic given their inputs; clocks are injected so
tests can pin timestamps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

DEFAULT_RECV_WINDOW_MS = 5000


def now_ms() -> int:
    return int(time.time() * 1000)


def binance_signature(secret: str, query_string: str) -> str:
    """HMAC-SHA256 hex digest over the exact query string sent to Binance."""
    return hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()


def bybit_signature(
    secret: str, timestamp: str, api_key: str, recv_window: str, payload: str
) -> str:
    """HMAC-SHA256 hex digest over timestamp + api_key + recv_window + payload."""
    message = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def kraken_signature(secret_b64: str, uri: str, nonce: str, postdata: str) -> str:
    """HMAC-SHA512 over uri + nonce + postdata, base64-encoded to match Kraken."""
    key = base64.b64decode(secret_b64)
    message = (uri + nonce + postdata).encode()
    digest = hmac.new(key, message, hashlib.sha512).digest()
    return base64.b64encode(digest).decode()


def binance_signed_request(
    api_key: str,
    api_secret: str,
    params: dict[str, str],
    timestamp_ms: int | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build a signed Binance request: (params with signature, headers)."""
    ts = timestamp_ms if timestamp_ms is not None else now_ms()
    signed: dict[str, str] = dict(params)
    signed["timestamp"] = str(ts)
    signed["recvWindow"] = str(DEFAULT_RECV_WINDOW_MS)
    query = urlencode(signed)
    signed["signature"] = binance_signature(api_secret, query)
    return signed, {"X-MBX-APIKEY": api_key}


def bybit_signed_request(
    api_key: str,
    api_secret: str,
    params: dict[str, str],
    timestamp_ms: int | None = None,
    recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
) -> tuple[str, dict[str, str]]:
    """Build a signed Bybit v5 request: (payload query string, headers)."""
    ts = str(timestamp_ms if timestamp_ms is not None else now_ms())
    payload = urlencode(params)
    signature = bybit_signature(api_secret, ts, api_key, str(recv_window_ms), payload)
    headers = {
        "X-BAPI-APIKEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECVWINDOW": str(recv_window_ms),
        "X-BAPI-SIGN": signature,
    }
    return payload, headers


def kraken_signed_request(
    api_key: str,
    api_secret: str,
    uri: str,
    postdata: str,
    nonce: int | None = None,
) -> tuple[str, dict[str, str]]:
    """Build a signed Kraken private request: (postdata with nonce, headers)."""
    n = str(nonce if nonce is not None else now_ms())
    pairs = [("nonce", n)]
    if postdata:
        pairs.extend(
            (kv.split("=", 1)[0], kv.split("=", 1)[1]) for kv in postdata.split("&") if "=" in kv
        )
    body = urlencode(pairs)
    headers = {
        "API-Key": api_key,
        "API-Sign": kraken_signature(api_secret, uri, n, body),
    }
    return body, headers
