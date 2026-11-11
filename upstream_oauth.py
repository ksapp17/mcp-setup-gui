"""Upstream OAuth: sign in to APIs that use OAuth 2.0 instead of an API key
(GitHub, Google, Spotify, ...), so the runtime can call them as you.

    uv run upstream_oauth.py client-secret --project project.json   store the app's client secret
    uv run upstream_oauth.py login  --project project.json          sign in in the browser
    uv run upstream_oauth.py status --project project.json          who's signed in, when tokens expire
    uv run upstream_oauth.py logout --project project.json          forget the tokens

Add --api <id> when the project has more than one OAuth API.

How it works: the authorization code flow with PKCE and a loopback redirect
(RFC 8252). login listens on http://127.0.0.1:<port>/callback, opens the
provider's sign-in page, checks the state value that comes back, and trades
the code for tokens. The tokens are stored as one JSON value in the OS
keychain under the API's secretRef. The runtime sends the access token and
refreshes it when it expires, saving rotated refresh tokens back.

project.json, for an API that uses OAuth:

    "auth": {
      "secretRef": "secret://github/github/oauth",
      "placement": {"in": "header", "name": "Authorization", "prefix": "Bearer "},
      "oauth2": {
        "authorizeUrl": "https://github.com/login/oauth/authorize",
        "tokenUrl": "https://github.com/login/oauth/access_token",
        "clientId": "Ov23li...",
        "clientSecretRef": "secret://github/github/clientSecret",
        "scopes": ["read:user"]
      }
    }

Optional oauth2 keys: redirectPort (fixed port, for providers that need an
exact redirect URI such as Spotify), redirectPath (default /callback),
tokenAuth ("post" sends the client secret in the form, "basic" in an
Authorization header), extraAuthorizeParams (e.g. Google's
{"access_type": "offline", "prompt": "consent"} to get a refresh token).
"""

import argparse
import base64
import hashlib
import html
import json
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

EXPIRY_SKEW = 60          # refresh this many seconds before the access token expires
LOGIN_TIMEOUT = 300       # seconds to wait for the browser to come back
DEFAULT_REDIRECT_PATH = "/callback"


class OAuthError(Exception):
    pass


# ------------------------------------------------------------------ config ---

def oauth_config(api):
    return (api.get("auth") or {}).get("oauth2")


def check_config(api):
    """Raise OAuthError naming the first problem with an API's oauth2 block."""
    cfg = oauth_config(api)
    if not cfg:
        raise OAuthError(f"{api['id']} doesn't use OAuth (no auth.oauth2 in project.json).")
    for key in ("authorizeUrl", "tokenUrl"):
        url = cfg.get(key, "")
        parts = urlsplit(url)
        local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
        if parts.scheme != "https" and not local:
            raise OAuthError(f"{api['id']}: oauth2.{key} must be an https:// URL.")
    if not cfg.get("clientId"):
        raise OAuthError(f"{api['id']}: oauth2.clientId is empty. Register an OAuth app with the provider "
                         "and put its client ID in project.json (or set it in the GUI).")
    ref = cfg.get("clientSecretRef")
    if ref is not None and not str(ref).startswith("secret://"):
        raise OAuthError(f"{api['id']}: oauth2.clientSecretRef must start with secret://")
    if cfg.get("tokenAuth", "post") not in ("post", "basic"):
        raise OAuthError(f"{api['id']}: oauth2.tokenAuth must be \"post\" or \"basic\".")
    return cfg


# ------------------------------------------------------------------ tokens ---

def pkce_pair():
    verifier = secrets.token_urlsafe(48)  # 64 characters, inside RFC 7636's 43-128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def token_request(cfg, client_secret, form):
    """(data, headers, auth) for a POST to the token endpoint."""
    data = dict(form, client_id=cfg["clientId"])
    auth = None
    if client_secret:
        if cfg.get("tokenAuth", "post") == "basic":
            auth = httpx.BasicAuth(cfg["clientId"], client_secret)
        else:
            data["client_secret"] = client_secret
    # GitHub answers form-encoded unless asked for JSON
    return data, {"accept": "application/json"}, auth


def parse_token_response(response):
    try:
        data = response.json()
    except ValueError:
        data = {k: v[0] for k, v in parse_qs(response.text).items()}
    if not isinstance(data, dict):
        data = {}
    # GitHub reports errors with status 200, so check the body, not just the status
    if data.get("error"):
        detail = data.get("error_description") or ""
        raise OAuthError(f"The provider said {data['error']}" + (f": {detail}" if detail else ""))
    if response.status_code >= 400:
        raise OAuthError(f"The token endpoint returned {response.status_code}.")
    if not data.get("access_token"):
        raise OAuthError("The provider's answer had no access_token.")
    return data


def make_bundle(data, old=None):
    now = int(time.time())
    bundle = {"access_token": data["access_token"], "token_type": data.get("token_type", "bearer"),
              "issued_at": now}
    if data.get("expires_in"):
        bundle["expires_at"] = now + int(data["expires_in"])
    # Some providers (Google) don't send a new refresh token on refresh: keep the old one
    refresh = data.get("refresh_token") or (old or {}).get("refresh_token")
    if refresh:
        bundle["refresh_token"] = refresh
    if data.get("refresh_token_expires_in"):
        bundle["refresh_expires_at"] = now + int(data["refresh_token_expires_in"])
    elif old and not data.get("refresh_token") and old.get("refresh_expires_at"):
        bundle["refresh_expires_at"] = old["refresh_expires_at"]
    if data.get("scope"):
        bundle["scope"] = data["scope"]
    return bundle


def parse_bundle(raw):
    """The stored value: a JSON bundle from login, or a bare token pasted by hand."""
    if raw is None:
        return None
    try:
        bundle = json.loads(raw)
        if isinstance(bundle, dict) and bundle.get("access_token"):
            return bundle
    except ValueError:
        pass
    return {"access_token": raw.strip()}


def dump_bundle(bundle):
    return json.dumps(bundle, separators=(",", ":"))  # compact: Windows caps a credential at 2560 bytes


def expiring(bundle, now=None):
    at = bundle.get("expires_at")
    return at is not None and (now or time.time()) >= at - EXPIRY_SKEW


class TokenSource:
    """The runtime's handle on one API's tokens. Refreshes when needed, at most one
    refresh at a time, and saves the new tokens with save(bundle)."""

    def __init__(self, api_id, cfg, raw, client_secret, load_latest, save, log, project_path="<project.json>"):
        self.api_id = api_id
        self.project_path = project_path
        self.cfg = cfg
        self.bundle = parse_bundle(raw)
        self.client_secret = client_secret
        self.load_latest = load_latest  # () -> stored raw value now, or None
        self.save = save                # (bundle) -> None
        self.log = log
        self._lock = None

    @property
    def can_refresh(self):
        return bool(self.bundle.get("refresh_token"))

    async def access_token(self):
        if expiring(self.bundle):
            await self.refresh(self.bundle["access_token"])
        return self.bundle["access_token"]

    async def refresh(self, failed_token):
        """Get a new access token. failed_token is the one that was expired or refused;
        if it has already been replaced (another request, or another process like a
        second Claude client sharing the keychain), use the replacement instead."""
        import anyio
        if self._lock is None:
            self._lock = anyio.Lock()
        async with self._lock:
            if self.bundle["access_token"] != failed_token:
                return  # refreshed by another request while we waited
            latest = parse_bundle(self.load_latest())
            if latest and latest["access_token"] != failed_token and not expiring(latest):
                self.bundle = latest
                return
            if latest and latest.get("refresh_token"):
                self.bundle = latest  # its refresh token is the newest one
            if not self.can_refresh:
                raise OAuthError(self.signin_hint("The sign-in has expired"))
            data, headers, auth = token_request(self.cfg, self.client_secret, {
                "grant_type": "refresh_token", "refresh_token": self.bundle["refresh_token"]})
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.post(self.cfg["tokenUrl"], data=data, headers=headers, auth=auth)
            except httpx.RequestError as e:
                raise OAuthError(f"Could not reach the token endpoint ({type(e).__name__})") from None
            try:
                answer = parse_token_response(response)
            except OAuthError as e:
                raise OAuthError(self.signin_hint(f"Refreshing the sign-in failed ({e})")) from None
            self.bundle = make_bundle(answer, self.bundle)
            self.log(f"{self.api_id}: access token refreshed")
            try:
                self.save(self.bundle)
            except Exception as e:  # keep working on the in-memory token
                self.log(f"{self.api_id}: couldn't save the refreshed token ({type(e).__name__})")

    def signin_hint(self, what):
        return f"{what}. Sign in again: uv run upstream_oauth.py login --project {self.project_path} --api {self.api_id}"


# ------------------------------------------------------------------- login ---

PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem}}</style>
<h2>{title}</h2><p>{body}</p>{link}"""


class LoginFlow:
    """One sign-in attempt. start() returns the URL to open; wait() blocks until the
    browser comes back (or the timeout) and returns the token bundle."""

    def __init__(self, api, client_secret, return_url=None, timeout=LOGIN_TIMEOUT):
        self.api = api
        self.cfg = check_config(api)
        self.client_secret = client_secret
        self.return_url = return_url
        self.timeout = timeout
        self.state = secrets.token_urlsafe(32)
        self.verifier, self.challenge = pkce_pair()
        self.result = None  # ("code", value) or ("error", message)
        self.server = None

    def start(self):
        flow = self
        path = self.cfg.get("redirectPath", DEFAULT_REDIRECT_PATH)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                if url.path != path or flow.result is not None:
                    return self.reply(404, "Not found", "Nothing here.")
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if not secrets.compare_digest(q.get("state", ""), flow.state):
                    # A stray or forged request: ignore it and keep waiting for the real one
                    return self.reply(400, "Sign-in not accepted",
                                      "This sign-in response doesn't match the one MCP Setup started.")
                if q.get("error"):
                    flow.result = ("error", f"The provider said {q['error']}"
                                   + (f": {q['error_description']}" if q.get("error_description") else ""))
                    return self.reply(200, "Sign-in cancelled", html.escape(flow.result[1]) + ".")
                if not q.get("code"):
                    return self.reply(400, "Sign-in not accepted", "The response had no code.")
                flow.result = ("code", q["code"])
                self.reply(200, "Signed in", f"You can close this tab. MCP Setup is finishing the sign-in "
                                             f"for <b>{html.escape(flow.api['id'])}</b>.")

            def reply(self, status, title, body):
                link = (f'<p><a href="{html.escape(flow.return_url)}">Back to MCP Setup</a></p>'
                        if flow.return_url else "")
                data = PAGE.format(title=title, body=body, link=link).encode()
                self.send_response(status)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-security-policy", "default-src 'none'; style-src 'unsafe-inline'")
                self.send_header("referrer-policy", "no-referrer")  # the URL holds the code
                self.send_header("cache-control", "no-store")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass  # the request line holds the code: keep it out of the terminal

        try:
            self.server = HTTPServer(("127.0.0.1", int(self.cfg.get("redirectPort", 0))), Handler)
        except OSError:
            raise OAuthError(f"Port {self.cfg.get('redirectPort')} is in use, so the sign-in can't listen "
                             "for the redirect. Close whatever is using it and try again.") from None
        self.server.timeout = 0.5
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}{path}"

        params = {
            "response_type": "code",
            "client_id": self.cfg["clientId"],
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
        }
        if self.cfg.get("scopes"):
            params["scope"] = " ".join(self.cfg["scopes"])
        params.update(self.cfg.get("extraAuthorizeParams") or {})
        sep = "&" if urlsplit(self.cfg["authorizeUrl"]).query else "?"
        return self.cfg["authorizeUrl"] + sep + urlencode(params)

    def wait(self):
        deadline = time.monotonic() + self.timeout
        try:
            while self.result is None and time.monotonic() < deadline:
                self.server.handle_request()
        finally:
            self.server.server_close()
        if self.result is None:
            raise OAuthError(f"No answer from the browser within {self.timeout // 60} minutes. Try again.")
        kind, value = self.result
        if kind == "error":
            raise OAuthError(value)

        data, headers, auth = token_request(self.cfg, self.client_secret, {
            "grant_type": "authorization_code", "code": value,
            "redirect_uri": self.redirect_uri, "code_verifier": self.verifier})
        try:
            response = httpx.post(self.cfg["tokenUrl"], data=data, headers=headers, auth=auth, timeout=15)
        except httpx.RequestError as e:
            raise OAuthError(f"Could not reach the token endpoint ({type(e).__name__})") from None
        return make_bundle(parse_token_response(response))

    def cancel(self):
        self.result = self.result or ("error", "Sign-in cancelled")


# --------------------------------------------------------------- storage ---
# Shared by the CLI and the GUI. The runtime has its own lookup (env, token dir, keychain).

def account(ref):
    return ref.removeprefix("secret://")


def client_secret_for(api, project_path="<project.json>"):
    """The stored client secret, None if the API doesn't use one. Raises if it's missing."""
    from runtime import read_keychain, secret_env_name
    import os
    ref = oauth_config(api).get("clientSecretRef")
    if not ref:
        return None
    value = os.environ.get(secret_env_name(ref)) or read_keychain(account(ref))
    if not value:
        raise OAuthError(f"{api['id']}: the client secret isn't stored yet. Run: "
                         f"uv run upstream_oauth.py client-secret --project {project_path} --api {api['id']}")
    return value


def save_tokens(api, bundle):
    import keyring
    from runtime import KEYCHAIN_SERVICE
    keyring.set_password(KEYCHAIN_SERVICE, account(api["auth"]["secretRef"]), dump_bundle(bundle))


def stored_tokens(api):
    from runtime import read_keychain
    return parse_bundle(read_keychain(account(api["auth"]["secretRef"])))


def describe(bundle, now=None):
    """One line about a stored sign-in, for status and the GUI. Never shows a token."""
    if not bundle:
        return "not signed in"
    now = now or time.time()
    parts = ["signed in"]
    if bundle.get("expires_at"):
        left = int(bundle["expires_at"] - now)
        parts.append(f"access token expires in {left // 3600}h {left % 3600 // 60}m" if left > 0
                     else "access token expired")
    if bundle.get("refresh_token"):
        parts.append("refreshes automatically")
    elif bundle.get("expires_at"):
        parts.append("no refresh token, so you'll need to sign in again when it expires")
    if bundle.get("scope"):
        parts.append(f"scopes: {bundle['scope']}")
    return ", ".join(parts)


def login(api, project_path, open_browser=True, out=print):
    check_config(api)  # a missing client ID first, before asking about its secret
    flow = LoginFlow(api, client_secret_for(api, project_path))
    url = flow.start()
    out(f"Listening for the sign-in on {flow.redirect_uri}")
    out(f"Opening the sign-in page. If no browser opens, paste this into one:\n\n  {url}\n")
    if open_browser:
        webbrowser.open(url)
    bundle = flow.wait()
    save_tokens(api, bundle)
    return bundle


# --------------------------------------------------------------------- cli ---

def pick_api(project, api_id):
    candidates = [a for a in project["apis"] if oauth_config(a)]
    if api_id:
        match = next((a for a in project["apis"] if a["id"] == api_id), None)
        if not match:
            raise OAuthError(f"No API with id {api_id!r} in this project.")
        if not oauth_config(match):
            raise OAuthError(f"{api_id} doesn't use OAuth.")
        return match
    if not candidates:
        raise OAuthError("No API in this project uses OAuth (auth.oauth2).")
    if len(candidates) > 1:
        raise OAuthError("More than one OAuth API. Pick one with --api: "
                         + ", ".join(a["id"] for a in candidates))
    return candidates[0]


def main():
    parser = argparse.ArgumentParser(description="Sign in to APIs that use OAuth")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("login", "Sign in in the browser and store the tokens"),
                       ("status", "Show which OAuth APIs are signed in"),
                       ("logout", "Delete the stored tokens"),
                       ("client-secret", "Store the OAuth app's client secret in the keychain")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--project", required=True, help="Path to project.json")
        p.add_argument("--api", help="API id, when the project has more than one OAuth API")
        if name == "login":
            p.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it")
    opts = parser.parse_args()

    from runtime import KEYCHAIN_SERVICE, load_project
    project = load_project(opts.project)
    try:
        if opts.command == "status":
            apis = [a for a in project["apis"] if oauth_config(a)]
            if not apis:
                print("No API in this project uses OAuth.")
            for api in apis:
                print(f"{api['id']}: {describe(stored_tokens(api))}")
            return

        api = pick_api(project, opts.api)
        if opts.command == "login":
            bundle = login(api, opts.project, open_browser=not opts.no_browser)
            print(f"{api['id']}: {describe(bundle)}. Tokens saved to the OS keychain.")
            print("Restart Claude (or the server) so the runtime picks them up.")
        elif opts.command == "logout":
            import keyring
            try:
                keyring.delete_password(KEYCHAIN_SERVICE, account(api["auth"]["secretRef"]))
                print(f"{api['id']}: tokens deleted. To revoke access too, remove the app in your "
                      "account settings on the provider's site.")
            except Exception:
                print(f"{api['id']}: nothing stored.")
        elif opts.command == "client-secret":
            import getpass
            import keyring
            ref = check_config(api).get("clientSecretRef")
            if not ref:
                raise OAuthError(f"{api['id']} has no oauth2.clientSecretRef in project.json, "
                                 "so it doesn't use a client secret.")
            value = getpass.getpass("Client secret (input hidden): ").strip()
            if not value:
                raise OAuthError("Nothing entered, nothing stored.")
            keyring.set_password(KEYCHAIN_SERVICE, account(ref), value)
            print(f"{api['id']}: client secret saved to the OS keychain.")
    except OAuthError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("Cancelled.")


if __name__ == "__main__":
    main()
