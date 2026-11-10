"""Outgoing HTTP client factory sharing a single cached SSLContext."""
import functools
import ssl

import httpx


@functools.cache
def _tls() -> ssl.SSLContext:
    return httpx.create_ssl_context()


def client(timeout: float, **options) -> httpx.AsyncClient:
    """A client for one call, to use with `async with`. `options` go to httpx.AsyncClient (follow_redirects, event_hooks...)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), verify=_tls(), **options)
