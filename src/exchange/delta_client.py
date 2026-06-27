"""Minimal Delta Exchange REST client (public market data + signed trading).

Docs: https://docs.delta.exchange/  (REST v2)

Only the endpoints the bot needs are implemented. Public market-data calls work
without keys; trading/account calls require DELTA_API_KEY / DELTA_API_SECRET and
are signed with HMAC-SHA256 per Delta's scheme:

    signature = hex(hmac_sha256(secret, method + timestamp + path + query + body))
    headers   = {api-key, signature, timestamp}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional

import requests


class DeltaError(RuntimeError):
    pass


class DeltaClient:
    def __init__(
        self,
        base_url: str = "https://api.india.delta.exchange",
        api_key: str = "",
        api_secret: str = "",
        timeout: int = 15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "delta-trading-bot/0.1"})

    # ------------------------------------------------------------------ #
    # Signing / requests
    # ------------------------------------------------------------------ #
    def _sign(self, method: str, path: str, query: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = method + timestamp + path + query + body
        signature = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "signature": signature,
            "timestamp": timestamp,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        signed: bool = False,
    ) -> Any:
        params = params or {}
        # Deterministic query string for signing.
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={params[k]}" for k in params)
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers: dict[str, str] = {}
        if signed:
            if not (self.api_key and self.api_secret):
                raise DeltaError("API key/secret required for signed endpoint.")
            headers = self._sign(method, path, query, body_str)

        url = self.base_url + path + query
        resp = self.session.request(
            method,
            url,
            data=body_str if body_str else None,
            headers=headers,
            timeout=self.timeout,
        )
        try:
            payload = resp.json()
        except ValueError:
            raise DeltaError(f"Non-JSON response ({resp.status_code}): {resp.text[:200]}")

        if not resp.ok or (isinstance(payload, dict) and payload.get("success") is False):
            raise DeltaError(f"{resp.status_code} {path} -> {payload}")
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    # ------------------------------------------------------------------ #
    # Public market data
    # ------------------------------------------------------------------ #
    def get_candles(
        self, symbol: str, resolution: str, start: int, end: int
    ) -> list[dict]:
        """OHLC candles. start/end are unix seconds. Max ~2000 candles/call."""
        params = {
            "resolution": resolution,
            "symbol": symbol,
            "start": int(start),
            "end": int(end),
        }
        result = self._request("GET", "/v2/history/candles", params=params)
        return result or []

    def get_product(self, symbol: str) -> dict:
        result = self._request("GET", f"/v2/products/{symbol}")
        return result

    def get_ticker(self, symbol: str) -> dict:
        return self._request("GET", f"/v2/tickers/{symbol}")

    # ------------------------------------------------------------------ #
    # Account / trading (signed)
    # ------------------------------------------------------------------ #
    def get_balances(self) -> list[dict]:
        return self._request("GET", "/v2/wallet/balances", signed=True)

    def get_positions(self) -> list[dict]:
        return self._request("GET", "/v2/positions/margined", signed=True)

    def place_order(
        self,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "reduce_only": reduce_only,
        }
        if order_type == "limit_order":
            if limit_price is None:
                raise DeltaError("limit_price required for limit_order.")
            body["limit_price"] = str(limit_price)
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._request("POST", "/v2/orders", body=body, signed=True)

    def place_bracket_order(
        self,
        product_id: int,
        size: int,
        side: str,
        stop_loss_price: float,
        take_profit_price: float,
        order_type: str = "market_order",
        limit_price: Optional[float] = None,
    ) -> dict:
        """Entry order with attached stop-loss and take-profit (bracket)."""
        body: dict[str, Any] = {
            "product_id": product_id,
            "size": int(size),
            "side": side,
            "order_type": order_type,
            "bracket_stop_loss_price": str(stop_loss_price),
            "bracket_take_profit_price": str(take_profit_price),
        }
        if order_type == "limit_order":
            if limit_price is None:
                raise DeltaError("limit_price required for limit_order.")
            body["limit_price"] = str(limit_price)
        return self._request("POST", "/v2/orders", body=body, signed=True)

    def cancel_all(self, product_id: int) -> Any:
        body = {"product_id": product_id}
        return self._request("DELETE", "/v2/orders/all", body=body, signed=True)
