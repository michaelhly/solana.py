import os
import logging
import asyncio
import websockets
import json
import requests
import itertools

from threading import Thread
from types import TracebackType
from typing import (
    Any,
    Optional,
    Type,
    Union,
)


from .._utils.encoding import FriendlyJsonSerde
from ..types import URI, RPCMethod, RPCResponse
from .base import BaseProvider
from .http import HTTPProvider


RESTRICTED_WEBSOCKET_KWARGS = {"uri", "loop"}
DEFAULT_WEBSOCKET_TIMEOUT = 10


def get_default_endpoint() -> URI:
    """Get the default http rpc endpoint."""
    return URI(os.environ.get("SOLANA_WS_PROVIDER_URI", "ws://localhost:8900"))


def _start_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()
    loop.close()


def _get_threaded_loop() -> asyncio.AbstractEventLoop:
    new_loop = asyncio.new_event_loop()
    thread_loop = Thread(target=_start_event_loop, args=(new_loop,), daemon=True)
    thread_loop.start()
    return new_loop


class PersistentWebSocket:
    """Sets up a persistent websocket"""

    def __init__(self, endpoint_uri: URI, loop: asyncio.AbstractEventLoop, websocket_kwargs: Any) -> None:
        self.ws: websockets.WebSocketClientProtocol = None
        self.endpoint_uri = endpoint_uri
        self.loop = loop
        self.websocket_kwargs = websocket_kwargs

    async def __aenter__(self) -> websockets.WebSocketClientProtocol:
        if self.ws is None:
            self.ws = await websockets.connect(uri=self.endpoint_uri, loop=self.loop, **self.websocket_kwargs)
        return self.ws

    async def __aexit__(self, exc_type: Type[BaseException], exc_val: BaseException, exc_tb: TracebackType) -> None:
        if exc_val is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None


class Provider(BaseProvider, FriendlyJsonSerde):
    logger = logging.getLogger("solana.providers.WebsocketProvider")
    _loop = None

    def __init__(
        self,
        endpoint_uri: Optional[Union[URI, str]] = None,
        websocket_kwargs: Optional[Any] = None,
        websocket_timeout: int = DEFAULT_WEBSOCKET_TIMEOUT,
    ) -> None:
        self._request_counter = itertools.count()
        self.endpoint_uri = URI(endpoint_uri)
        self.websocket_timeout = websocket_timeout
        if self.endpoint_uri is None:
            self.endpoint_uri = get_default_endpoint()
        if Provider._loop is None:
            Provider._loop = _get_threaded_loop()
        if websocket_kwargs is None:
            websocket_kwargs = {}
        else:
            found_restricted_keys = set(websocket_kwargs.keys()).intersection(RESTRICTED_WEBSOCKET_KWARGS)
            if found_restricted_keys:
                raise Exception(
                    "{0} are not allowed in websocket_kwargs, "
                    "found: {1}".format(RESTRICTED_WEBSOCKET_KWARGS, found_restricted_keys)
                )
        self.conn = PersistentWebSocket(self.endpoint_uri, Provider._loop, websocket_kwargs)
        print(self.conn)
        super().__init__()

    def __str__(self) -> str:
        return "WS connection {0}".format(self.endpoint_uri)

    async def coro_make_request(self, request_data: bytes) -> RPCResponse:
        async with self.conn as conn:
            await asyncio.wait_for(conn.send(request_data), timeout=self.websocket_timeout)
            return json.loads(await asyncio.wait_for(conn.recv(), timeout=self.websocket_timeout))

    def make_request(self, method: RPCMethod, *params: Any) -> RPCResponse:
        """Make a request ot the rpc endpoint."""
        request_id = next(self._request_counter) + 1
        self.logger.debug(
            "Making HTTP request. URI: %s, RequestID: %d, Method: %s, Params: %s",
            self.endpoint_uri,
            request_id,
            method,
            params,
        )
        request_data = self.json_encode({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        future = asyncio.run_coroutine_threadsafe(self.coro_make_request(request_data), Provider._loop)
        return future.result()

    def is_connected(self) -> bool:
        """Health check."""
        if self.conn and self.conn.ws:
            self.conn.ws.recv()
            return self.conn.ws.connected
        return False