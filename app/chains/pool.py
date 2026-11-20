"""A pool of RPC/HTTP endpoints of one provider role (primary reads or independent verification).

Free public endpoints fail, rate-limit and lag; the pool hides that from adapters:

* endpoints are tried in order of preference, a failing one is skipped for ``cooldown`` seconds
  (circuit breaker), and the last good one is tried first next time;
* HTTP 429/5xx, timeouts and JSON-RPC errors count as endpoint failures;
* when every endpoint failed, :class:`ChainUnavailable` is raised (the watcher retries later);
* API keys come from environment variables named in the config and are sent as headers — they
  are never logged.
"""

from __future__ import annotations

import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar

import httpx

from app.chains.base import ChainUnavailable
from app.config import RpcEndpoint
from app.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")
DEFAULT_TIMEOUT = 10.0


class RpcError(Exception):
    """A JSON-RPC error object (or a REST error body) returned by an endpoint."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def range_too_large(self) -> bool:
        text = self.message.lower()
        return any(
            s in text for s in ("range", "too many", "limit", "exceed", "10000 results", "block range")
        )


class EndpointPool:
    def __init__(
        self,
        endpoints: Sequence[RpcEndpoint],
        *,
        key_header: str | None = None,
        key_prefix: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        cooldown: float = 30.0,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not endpoints:
            raise ValueError("at least one endpoint is required")
        env = os.environ if env is None else env
        self._endpoints = list(endpoints)
        self._clients: dict[str, httpx.AsyncClient] = {}
        for ep in self._endpoints:
            headers = {"User-Agent": "escrow-bot/1.0"}
            key = env.get(ep.api_key_env, "") if ep.api_key_env else ""
            if key and key_header:
                headers[key_header] = f"{key_prefix}{key}"
            self._clients[ep.name] = httpx.AsyncClient(
                transport=transport, timeout=timeout, headers=headers, follow_redirects=False
            )
        self._down_until: dict[str, float] = {}
        self._cooldown = cooldown
        self._clock = clock
        self.active: str | None = None

    @property
    def endpoints(self) -> list[RpcEndpoint]:
        return list(self._endpoints)

    def _ordered(self, prefer_method: str | None) -> list[RpcEndpoint]:
        """Healthy endpoints first (the last good one, then those known to support the method, then
        by weight); endpoints in cooldown are kept as a last resort rather than stalling forever."""
        now = self._clock()

        def rank(ep: RpcEndpoint) -> tuple[int, int, int, int]:
            down = int(self._down_until.get(ep.name, 0) > now)
            knows = int(not (prefer_method and prefer_method in ep.methods))
            return (down, int(ep.name != self.active), knows, -ep.weight)

        return sorted(self._endpoints, key=rank)

    async def run(
        self,
        op: Callable[[httpx.AsyncClient, RpcEndpoint], Awaitable[T]],
        *,
        prefer_method: str | None = None,
    ) -> T:
        last: BaseException | None = None
        for ep in self._ordered(prefer_method):
            try:
                result = await op(self._clients[ep.name], ep)
            except RpcError as exc:
                if exc.range_too_large:
                    raise  # the request is too big for any endpoint of this kind: the caller splits it
                last = exc
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                last = exc
            else:
                self._down_until.pop(ep.name, None)
                self.active = ep.name
                return result
            self._down_until[ep.name] = self._clock() + self._cooldown
            log.warning("rpc_endpoint_failed", endpoint=ep.name, error=_safe(last))
        raise ChainUnavailable(f"no endpoint answered: {_safe(last)}")

    async def json_rpc(self, method: str, params: list[Any]) -> Any:
        async def op(client: httpx.AsyncClient, ep: RpcEndpoint) -> Any:
            response = await client.post(
                ep.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            _raise_for_status(response)
            body = response.json()
            if body.get("error"):
                err = body["error"]
                raise RpcError(err.get("code"), str(err.get("message", err)))
            if "result" not in body:
                raise ValueError("no result in the JSON-RPC response")
            return body["result"]

        return await self.run(op, prefer_method=method)

    async def json_rpc_at(self, endpoint: RpcEndpoint, method: str, params: list[Any]) -> Any:
        """One specific endpoint, no failover (broadcasting to every endpoint on purpose)."""
        response = await self._clients[endpoint.name].post(
            endpoint.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        _raise_for_status(response)
        body = response.json()
        if body.get("error"):
            err = body["error"]
            raise RpcError(err.get("code"), str(err.get("message", err)))
        return body.get("result")

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        async def op(client: httpx.AsyncClient, ep: RpcEndpoint) -> Any:
            response = await client.get(ep.url.rstrip("/") + path, params=params)
            _raise_for_status(response)
            return response.json()

        return await self.run(op)

    async def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        async def op(client: httpx.AsyncClient, ep: RpcEndpoint) -> Any:
            response = await client.post(ep.url.rstrip("/") + path, json=dict(payload))
            _raise_for_status(response)
            return response.json()

        return await self.run(op)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise httpx.HTTPStatusError(
            f"HTTP {response.status_code}", request=response.request, response=response
        )
    if response.status_code >= 400:
        raise RpcError(response.status_code, response.text[:200])


def _safe(exc: BaseException | None) -> str:
    """Error text without URLs (an endpoint URL may embed an API key)."""
    if exc is None:
        return "no endpoints"
    text = f"{type(exc).__name__}: {exc}"
    return " ".join(w for w in text.split() if "://" not in w)[:300]
