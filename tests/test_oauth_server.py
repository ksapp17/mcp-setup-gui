"""The built-in sign-in server for remote MCP (oauth.py): stateless, signed, single owner."""

import time

import pytest
from mcp.server.auth.provider import AuthorizationParams, RegistrationError, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from oauth import OwnerOAuthProvider, Signer

TOKEN = "t" * 43
PUBLIC = "https://mcp.example.com"


def provider(token=TOKEN):
    return OwnerOAuthProvider(token, PUBLIC, PUBLIC + "/mcp", "test")


def client_info(redirect="https://claude.ai/api/mcp/auth_callback"):
    return OAuthClientInformationFull(client_id="tmp", redirect_uris=[redirect], client_name="Claude",
                                      token_endpoint_auth_method="client_secret_post", client_secret="x",
                                      grant_types=["authorization_code", "refresh_token"], response_types=["code"])


def test_signer_rejects_tampering_kind_and_expiry():
    s = Signer(TOKEN)
    good = s.sign("access", {"c": "a"})
    assert s.verify("access", good)["c"] == "a"
    assert s.verify("refresh", good) is None                     # wrong kind
    body, mac = good.split(".")
    assert s.verify("access", body[:-2] + "AA." + mac) is None    # edited payload
    assert s.verify("access", s.sign("access", {"exp": time.time() - 1})) is None
    assert Signer("other" * 9).verify("access", good) is None     # another server's key


@pytest.mark.anyio
async def test_register_and_rebuild_client():
    p = provider()
    info = client_info()
    await p.register_client(info)
    again = await p.get_client(info.client_id)
    assert again.redirect_uris[0].unicode_string() == "https://claude.ai/api/mcp/auth_callback"
    assert again.client_secret == info.client_secret
    assert await p.get_client("forged.client") is None


@pytest.mark.anyio
async def test_plain_http_redirect_refused():
    with pytest.raises(RegistrationError):
        await provider().register_client(client_info("http://evil.example.com/cb"))
    await provider().register_client(client_info("http://127.0.0.1:5000/cb"))  # loopback is fine


@pytest.mark.anyio
async def test_foreign_resource_refused():
    p = provider()
    info = client_info()
    await p.register_client(info)
    params = AuthorizationParams(state="s", scopes=["mcp"], code_challenge="c" * 43,
                                 redirect_uri=info.redirect_uris[0], redirect_uri_provided_explicitly=True,
                                 resource="https://other.example.com/mcp")
    with pytest.raises(AuthorizeError):
        await p.authorize(info, params)


@pytest.mark.anyio
async def test_code_is_single_use_and_tokens_work():
    p = provider()
    info = client_info()
    await p.register_client(info)
    code = p.issue_code({"c": info.client_id, "sc": ["mcp"], "ch": "c" * 43,
                         "r": str(info.redirect_uris[0]), "re": True})
    loaded = await p.load_authorization_code(info, code)
    tokens = await p.exchange_authorization_code(info, loaded)
    with pytest.raises(TokenError):
        await p.exchange_authorization_code(info, loaded)
    assert await p.load_access_token(tokens.access_token)
    assert await p.load_access_token(tokens.refresh_token) is None  # refresh token isn't an access token
    assert await p.load_access_token(TOKEN)                         # the raw server token still works
    assert await provider("n" * 43).load_access_token(tokens.access_token) is None  # rotated token: all out
