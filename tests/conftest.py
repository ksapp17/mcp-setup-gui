"""Shared fixtures. Nothing here touches the network, the real keychain or Claude's config."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No MCP_* variables leak in from the shell, and HOME is a temp folder."""
    for name in list(os.environ):
        if name.startswith("MCP_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


@pytest.fixture
def keychain(monkeypatch):
    """An in-memory keychain: {(service, account): value}."""
    import keyring
    store = {}

    def delete(service, account):
        if (service, account) not in store:
            raise KeyError(account)
        del store[(service, account)]

    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    monkeypatch.setattr(keyring, "set_password", lambda s, a, v: store.__setitem__((s, a), v))
    monkeypatch.setattr(keyring, "delete_password", delete)
    return store


class Upstream:
    """Stands in for every API the code calls. Set .handler to fn(httpx.Request) -> httpx.Response."""

    def __init__(self):
        self.handler = None
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def upstream(monkeypatch):
    import httpx
    fake = Upstream()
    transport = httpx.MockTransport(fake)
    real_async, real_client = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_async(*a, transport=transport, **kw))

    def post(url, **kw):
        with real_client(transport=transport) as client:
            return client.post(url, **kw)

    monkeypatch.setattr(httpx, "post", post)
    return fake
