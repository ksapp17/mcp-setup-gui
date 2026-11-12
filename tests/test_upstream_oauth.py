import base64
import hashlib
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import runtime
import upstream_oauth as up

CFG = {"authorizeUrl": "https://id.example.com/authorize", "tokenUrl": "https://id.example.com/token",
       "clientId": "cid", "clientSecretRef": "secret://gh/client", "scopes": ["read:user"]}


def oauth_api():
    return {"id": "gh", "baseUrl": "https://api.example.com",
            "auth": {"secretRef": "secret://gh/oauth", "placement": {"in": "header", "name": "Authorization", "prefix": "Bearer "},
                     "oauth2": dict(CFG)},
            "tools": [{"name": "me", "method": "GET", "path": "/user", "paramLocations": {}, "inputSchema": {"type": "object"}}]}


def form(request):
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --------------------------------------------------------------- pieces ---

def test_pkce_pair_is_s256():
    verifier, challenge = up.pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_parses_form_encoded_and_errors_in_200():
    r = httpx.Response(200, text="access_token=abc&token_type=bearer")
    assert up.parse_token_response(r)["access_token"] == "abc"
    with pytest.raises(up.OAuthError, match="bad_refresh_token"):
        up.parse_token_response(httpx.Response(200, json={"error": "bad_refresh_token"}))
    with pytest.raises(up.OAuthError, match="no access_token"):
        up.parse_token_response(httpx.Response(200, json={}))


def test_refresh_keeps_old_refresh_token_when_none_sent():
    old = up.make_bundle({"access_token": "a1", "refresh_token": "r1", "expires_in": 60})
    new = up.make_bundle({"access_token": "a2", "expires_in": 3600}, old)
    assert new["refresh_token"] == "r1" and new["expires_at"] > old["expires_at"]


def test_bare_token_is_accepted():
    assert up.parse_bundle("ghp_plain") == {"access_token": "ghp_plain"}


@pytest.mark.parametrize("change,match", [({"clientId": ""}, "clientId"), ({"tokenUrl": "http://id.example.com/t"}, "https"),
                                          ({"clientSecretRef": "plain"}, "secret://"), ({"tokenAuth": "jwt"}, "tokenAuth")])
def test_config_checks(change, match):
    api = oauth_api()
    api["auth"]["oauth2"].update(change)
    with pytest.raises(up.OAuthError, match=match):
        up.check_config(api)


# ----------------------------------------------------------- the runtime ---

def token_endpoint(issued, rotate=True):
    """A provider that rotates refresh tokens and kills old ones, like GitHub."""
    def handle(request):
        if request.url.path == "/token":
            f = form(request)
            assert f["client_id"] == "cid" and f["client_secret"] == "csec"
            if f["refresh_token"] not in issued["refresh"]:
                return httpx.Response(200, json={"error": "bad_refresh_token"})
            issued["refresh"].discard(f["refresh_token"])
            issued["n"] = n = issued.get("n", 0) + 1
            issued["access"].add(f"a{n}")
            issued["refresh"].add(f"r{n}")
            return httpx.Response(200, json={"access_token": f"a{n}", "refresh_token": f"r{n}", "expires_in": 3600})
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in issued["access"]:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(200, json={"login": "kaden"})
    return handle


def signed_in(keychain, access="a0", refresh="r0", expires_at=None):
    keychain[("mcp-setup-gui", "gh/client")] = "csec"
    bundle = {"access_token": access, "refresh_token": refresh, "issued_at": 1}
    if expires_at is not None:
        bundle["expires_at"] = expires_at
    keychain[("mcp-setup-gui", "gh/oauth")] = json.dumps(bundle)


def stored(keychain):
    return json.loads(keychain[("mcp-setup-gui", "gh/oauth")])


@pytest.mark.anyio
async def test_expired_token_refreshes_and_saves_rotation(keychain, upstream):
    issued = {"access": set(), "refresh": {"r0"}}
    upstream.handler = token_endpoint(issued)
    signed_in(keychain, expires_at=0)
    project = {"name": "x", "apis": [oauth_api()]}
    key = runtime.load_secrets(project)["gh"]
    api = project["apis"][0]
    assert json.loads(await runtime.call_upstream(api, api["tools"][0], {}, key, 10_000)) == {"login": "kaden"}
    assert stored(keychain)["access_token"] in issued["access"]
    assert stored(keychain)["refresh_token"] != "r0"  # the spent one was replaced


@pytest.mark.anyio
async def test_401_triggers_one_refresh_and_retry(keychain, upstream):
    issued = {"access": set(), "refresh": {"r0"}}
    upstream.handler = token_endpoint(issued)
    signed_in(keychain, access="revoked")
    project = {"name": "x", "apis": [oauth_api()]}
    key = runtime.load_secrets(project)["gh"]
    api = project["apis"][0]
    await runtime.call_upstream(api, api["tools"][0], {}, key, 10_000)
    assert stored(keychain)["access_token"] in issued["access"]
    assert stored(keychain)["refresh_token"] in issued["refresh"]


@pytest.mark.anyio
async def test_two_clients_share_one_sign_in(keychain, upstream):
    """Desktop and Claude Code each run a runtime. When one refreshes (spending the refresh
    token), the other must pick up the new tokens instead of failing."""
    issued = {"access": {"a0"}, "refresh": {"r0"}}
    upstream.handler = token_endpoint(issued)
    signed_in(keychain)
    project = {"name": "x", "apis": [oauth_api()]}
    api = project["apis"][0]
    first = runtime.load_secrets(project)["gh"]
    second = runtime.load_secrets(project)["gh"]
    first.bundle["expires_at"] = 0
    await runtime.call_upstream(api, api["tools"][0], {}, first, 10_000)  # rotates r0, kills a0
    assert json.loads(await runtime.call_upstream(api, api["tools"][0], {}, second, 10_000)) == {"login": "kaden"}
    refreshes = [r for r in upstream.requests if r.url.path == "/token"]
    assert len(refreshes) == 1


@pytest.mark.anyio
async def test_dead_refresh_token_says_sign_in_again(keychain, upstream):
    upstream.handler = token_endpoint({"access": set(), "refresh": set()})
    signed_in(keychain, expires_at=0)
    project = {"name": "x", "apis": [oauth_api()]}
    key = runtime.load_secrets(project, "gh.json")["gh"]
    api = project["apis"][0]
    with pytest.raises(up.OAuthError, match="login --project gh.json --api gh"):
        await runtime.call_upstream(api, api["tools"][0], {}, key, 10_000)


def test_not_signed_in_message(keychain):
    keychain[("mcp-setup-gui", "gh/client")] = "csec"
    with pytest.raises(SystemExit, match="isn't signed in"):
        runtime.load_secrets({"name": "x", "apis": [oauth_api()]})


# ------------------------------------------------------------- the login ---

def browse(url, **extra):
    """Act as the browser: follow the provider's redirect to the loopback listener."""
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    params = {"state": q["state"], **extra}
    if "code" not in extra and "error" not in extra:
        params["code"] = "the-code"
    return httpx.get(q["redirect_uri"], params=params)


def run_login(flow):
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=_wait(flow)))
    thread.start()
    return thread, box


def _wait(flow):
    try:
        return flow.wait()
    except up.OAuthError as e:
        return e


def test_login_flow(upstream):
    seen = {}

    def token(request):
        seen.update(form(request))
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 28800})
    upstream.handler = token

    flow = up.LoginFlow(oauth_api(), "csec", timeout=10)
    url = flow.start()
    q = parse_qs(urlsplit(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["read:user"]
    assert q["redirect_uri"][0].startswith("http://127.0.0.1:")
    thread, box = run_login(flow)

    forged = httpx.get(q["redirect_uri"][0], params={"state": "wrong", "code": "evil"})
    assert forged.status_code == 400  # ignored; still waiting for the real one
    page = browse(url)
    assert "Signed in" in page.text and "default-src 'none'" in page.headers["content-security-policy"]
    thread.join(5)

    bundle = box["result"]
    assert bundle["access_token"] == "A" and bundle["refresh_token"] == "R"
    assert seen["code"] == "the-code" and seen["code_verifier"] == flow.verifier
    assert seen["client_secret"] == "csec" and seen["redirect_uri"] == flow.redirect_uri


def test_login_denied(upstream):
    flow = up.LoginFlow(oauth_api(), "csec", timeout=10)
    url = flow.start()
    thread, box = run_login(flow)
    browse(url, error="access_denied")
    thread.join(5)
    assert isinstance(box["result"], up.OAuthError) and "access_denied" in str(box["result"])


def test_basic_client_auth(upstream):
    upstream.handler = lambda r: httpx.Response(200, json={"access_token": "A"})
    api = oauth_api()
    api["auth"]["oauth2"]["tokenAuth"] = "basic"
    flow = up.LoginFlow(api, "csec", timeout=10)
    url = flow.start()
    thread, _ = run_login(flow)
    browse(url)
    thread.join(5)
    sent = upstream.requests[0]
    assert sent.headers["authorization"] == "Basic " + base64.b64encode(b"cid:csec").decode()
    assert "client_secret" not in form(sent)
