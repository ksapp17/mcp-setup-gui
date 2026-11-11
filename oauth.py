"""Built-in OAuth 2.1 sign-in for the remote runtime.

claude.ai's "Sign in" connectors need an OAuth authorization server. This module is a small
one for a personal server: one owner, who approves a client by entering the server's token
(MCP_BEARER_TOKEN) on a sign-in page the runtime serves.

It is stateless. Registered clients, authorization codes, access and refresh tokens are all
self-contained values signed with a key derived from MCP_BEARER_TOKEN, so they survive
restarts and redeploys, and any number of copies of the server accept them. Replacing the
token (remote.py token --new) invalidates every one of them at once.

The MCP SDK provides the OAuth endpoints (/authorize, /token, /register, metadata) and checks
PKCE, redirect URIs and client credentials. This module supplies the storage they call into,
plus the /login page.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

ACCESS_TTL = 3600               # 1 hour
REFRESH_TTL = 30 * 24 * 3600    # 30 days
CODE_TTL = 300                  # 5 minutes
LOGIN_TTL = 600                 # 10 minutes to finish signing in
SCOPE = "mcp"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """Tamper-proof values: base64(json) + "." + base64(HMAC-SHA256)."""

    def __init__(self, secret: str):
        # A separate key for this purpose, derived from the token
        self.key = hmac.new(secret.encode(), b"mcp-setup-gui/oauth/v1", hashlib.sha256).digest()

    def sign(self, kind: str, payload: dict) -> str:
        body = b64(json.dumps({"k": kind, **payload}, separators=(",", ":")).encode())
        mac = b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{mac}"

    def verify(self, kind: str, value: str):
        """The payload, or None if the value was forged, altered, expired or of another kind."""
        try:
            body, mac = value.split(".", 1)
            expected = b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(mac, expected):
                return None
            data = json.loads(unb64(body))
        except (ValueError, TypeError):
            return None
        if data.get("k") != kind:
            return None  # e.g. a refresh token presented as an access token
        if "exp" in data and data["exp"] < time.time():
            return None
        return data

    def derive(self, label: str) -> str:
        return b64(hmac.new(self.key, label.encode(), hashlib.sha256).digest())


def same_url(a, b) -> bool:
    return str(a).rstrip("/") == str(b).rstrip("/")


class OwnerOAuthProvider:
    """OAuthAuthorizationServerProvider for a single owner, with no stored state."""

    def __init__(self, owner_token: str, public_url: str, resource_url: str, server_name: str):
        self.owner_token = owner_token
        self.public_url = public_url.rstrip("/")
        self.resource_url = resource_url
        self.server_name = server_name
        self.signer = Signer(owner_token)
        self.used_codes: dict[str, float] = {}  # nonce -> expiry; codes are single use

    # ---------------------------------------------------------------- clients ---

    def client_secret_for(self, client_id: str) -> str:
        return self.signer.derive("client-secret:" + client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            parts = urlsplit(str(uri))
            loopback = parts.hostname in ("localhost", "127.0.0.1", "::1")
            if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="Redirect URIs must be https, or http on localhost.")
        # The client id itself carries the registration, signed, so nothing is stored.
        # (The SDK returns this same object to the client after this call.)
        client_info.client_id = self.signer.sign("client", {
            "r": [str(u) for u in client_info.redirect_uris or []],
            "n": client_info.client_name,
            "m": client_info.token_endpoint_auth_method,
            "g": client_info.grant_types,
            "s": client_info.scope,
        })
        if client_info.client_secret:
            client_info.client_secret = self.client_secret_for(client_info.client_id)

    async def get_client(self, client_id: str):
        data = self.signer.verify("client", client_id)
        if not data:
            return None
        method = data.get("m") or "client_secret_post"
        secret = None if method == "none" else self.client_secret_for(client_id)
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=secret,
            client_secret_expires_at=0 if secret else None,
            redirect_uris=data["r"],
            client_name=data.get("n"),
            token_endpoint_auth_method=method,
            grant_types=data.get("g") or ["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=data.get("s") or SCOPE,
        )

    # -------------------------------------------------------------- authorize ---

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and not same_url(params.resource, self.resource_url):
            raise AuthorizeError(error="invalid_request",
                                 error_description=f"This server only issues tokens for {self.resource_url}")
        request = self.signer.sign("login", {
            "c": client.client_id,
            "st": params.state,
            "sc": params.scopes or [SCOPE],
            "ch": params.code_challenge,
            "r": str(params.redirect_uri),
            "re": params.redirect_uri_provided_explicitly,
            "exp": int(time.time()) + LOGIN_TTL,
        })
        return f"{self.public_url}/login?req={request}"

    def issue_code(self, login: dict) -> str:
        return self.signer.sign("code", {
            "c": login["c"], "sc": login["sc"], "ch": login["ch"], "r": login["r"], "re": login["re"],
            "n": secrets.token_urlsafe(12), "exp": int(time.time()) + CODE_TTL,
        })

    async def load_authorization_code(self, client, authorization_code: str):
        data = self.signer.verify("code", authorization_code)
        if not data or data["c"] != client.client_id or data["n"] in self.used_codes:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=data["sc"], expires_at=data["exp"], client_id=data["c"],
            code_challenge=data["ch"], redirect_uri=data["r"], redirect_uri_provided_explicitly=data["re"],
            resource=self.resource_url, subject="owner")

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        data = self.signer.verify("code", authorization_code.code)
        if not data or data["n"] in self.used_codes:
            raise TokenError(error="invalid_grant", error_description="Code already used or expired")
        now = time.time()
        self.used_codes = {n: exp for n, exp in self.used_codes.items() if exp > now}  # forget old ones
        self.used_codes[data["n"]] = data["exp"]
        return self.issue_tokens(client.client_id, authorization_code.scopes)

    # ----------------------------------------------------------------- tokens ---

    def issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access = self.signer.sign("access", {"c": client_id, "sc": scopes, "exp": now + ACCESS_TTL})
        refresh = self.signer.sign("refresh", {"c": client_id, "sc": scopes, "exp": now + REFRESH_TTL})
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    async def load_refresh_token(self, client, refresh_token: str):
        data = self.signer.verify("refresh", refresh_token)
        if not data or data["c"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=data["c"], scopes=data["sc"],
                            expires_at=data["exp"], resource=self.resource_url, subject="owner")

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        return self.issue_tokens(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str):
        # The plain server token keeps working for clients that send it as a header
        if hmac.compare_digest(token.encode(), self.owner_token.encode()):
            return AccessToken(token=token, client_id="owner-token", scopes=[SCOPE],
                               resource=self.resource_url, subject="owner")
        data = self.signer.verify("access", token)
        if not data:
            return None
        return AccessToken(token=token, client_id=data["c"], scopes=data["sc"], expires_at=data["exp"],
                           resource=self.resource_url, subject="owner")

    async def revoke_token(self, token) -> None:
        # Stateless tokens can't be revoked one by one. Replacing the server token revokes all.
        return None

    # ------------------------------------------------------------------ login ---

    def login_routes(self):
        return [Route("/login", self.login, methods=["GET", "POST"])]

    async def login(self, request: Request):
        if request.method == "POST":
            form = await request.form()
            req = form.get("req", "")
        else:
            form, req = None, request.query_params.get("req", "")

        login = self.signer.verify("login", req)
        if not login:
            return self.page("Sign-in link expired",
                             "<p>This sign-in link is invalid or has expired. Start again from Claude.</p>", 400)
        client = await self.get_client(login["c"])
        if not client:
            return self.page("Unknown app", "<p>This app isn't registered with this server.</p>", 400)

        error = ""
        if form is not None:
            if form.get("action") == "deny":
                return RedirectResponse(construct_redirect_uri(login["r"], error="access_denied",
                                                               state=login["st"]), status_code=302)
            if hmac.compare_digest(form.get("secret", "").strip().encode(), self.owner_token.encode()):
                code = self.issue_code(login)
                return RedirectResponse(construct_redirect_uri(login["r"], code=code, state=login["st"]),
                                        status_code=302, headers={"Cache-Control": "no-store"})
            await asyncio.sleep(1)  # slow down guessing (the token is 256 bits, but still)
            error = '<p class="err">That token doesn\'t match this server\'s.</p>'

        app = html.escape(client.client_name or "An app")
        dest = html.escape(urlsplit(login["r"]).hostname or "")
        body = f"""
<p><b>{app}</b> wants to use the tools on <b>{html.escape(self.server_name)}</b>.</p>
<p class="muted">After you approve, it is sent back to <b>{dest}</b>.</p>
{error}
<form method="post">
  <input type="hidden" name="req" value="{html.escape(req)}">
  <label for="secret">Server token</label>
  <input id="secret" type="password" name="secret" autocomplete="off" autofocus>
  <p class="muted">The token from <code>uv run remote.py token</code>, the same value as MCP_BEARER_TOKEN.</p>
  <button name="action" value="allow">Allow</button>
  <button name="action" value="deny" class="secondary">Deny</button>
</form>"""
        return self.page("Sign in", body)

    @staticmethod
    def page(title, body, status=200):
        style = """body{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#f6f6f4;color:#1d1d1f;
margin:0;display:flex;justify-content:center;padding:60px 16px}main{background:#fff;border:1px solid #e2e2de;
border-radius:12px;padding:28px 30px;max-width:440px;width:100%}h1{font-size:20px;margin:0 0 14px}
label{display:block;font-weight:600;font-size:13px;margin:16px 0 4px}input[type=password]{width:100%;
padding:9px 10px;border:1px solid #ccc;border-radius:6px;font:inherit;box-sizing:border-box}
button{background:#1d1d1f;color:#fff;border:0;border-radius:6px;padding:9px 18px;font:inherit;font-weight:600;
margin:16px 8px 0 0;cursor:pointer}button.secondary{background:#fff;color:#1d1d1f;border:1px solid #bbb}
.muted{color:#666;font-size:13px}.err{background:#fde3e1;padding:8px 12px;border-radius:6px}
code{background:#f0f0ec;padding:1px 4px;border-radius:4px;font-size:13px}"""
        return HTMLResponse(
            f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{style}</style></head>"
            f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>",
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https: http://localhost:* http://127.0.0.1:*; frame-ancestors 'none'",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
            },
        )
