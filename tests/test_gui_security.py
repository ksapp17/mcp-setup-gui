"""The browser UI only answers to the person who started it."""

import pytest
from starlette.testclient import TestClient

import gui


@pytest.fixture
def client():
    base = f"http://127.0.0.1:{gui.PORT}"
    with TestClient(gui.app, base_url=base, follow_redirects=False) as c:
        yield c, base


def test_foreign_host_refused(client):
    c, _ = client
    assert c.get("/", headers={"host": "evil.example.com"}).status_code == 403


def test_needs_the_token(client):
    c, _ = client
    assert c.get("/").status_code == 403
    r = c.get(f"/?t={gui.TOKEN}")
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    page = c.get("/")
    assert page.status_code == 200
    assert "script-src" not in page.headers["content-security-policy"]  # no scripts allowed at all
    assert page.headers["cache-control"] == "no-store"


def test_cross_site_post_refused(client):
    c, base = client
    c.get(f"/?t={gui.TOKEN}")
    assert c.post("/remote/token", headers={"origin": "https://evil.example.com"}).status_code == 403
    assert c.post("/registry/apply", data={"id": "x"}, headers={"origin": base}).status_code == 200  # gets "expired" page


def test_wrong_token_refused(client):
    c, _ = client
    assert c.get("/?t=guess").status_code == 403
