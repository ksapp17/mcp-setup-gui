import json

import httpx
import pytest

import runtime


def api(auth=None, base="https://api.example.com/v1"):
    a = {"id": "ex", "baseUrl": base, "tools": []}
    if auth:
        a["auth"] = auth
    return a


def tool(path="/things/{id}", method="GET", locations=None, **extra):
    return {"name": "t", "method": method, "path": path, "paramLocations": locations or {"id": "path"},
            "inputSchema": {"type": "object"}, **extra}


# ------------------------------------------------------------- project.json ---

def write(tmp_path, project):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(project))
    return str(path)


def test_rejects_literal_secret(tmp_path):
    project = {"name": "x", "apis": [api({"secretRef": "sk-live-123", "placement": {"in": "query", "name": "k"}})]}
    with pytest.raises(SystemExit, match="secret://"):
        runtime.load_project(write(tmp_path, project))


def test_rejects_duplicate_tool_names(tmp_path):
    a = api()
    a["tools"] = [tool(), tool()]
    with pytest.raises(SystemExit, match="Duplicate"):
        runtime.load_project(write(tmp_path, {"name": "x", "apis": [a]}))


@pytest.mark.parametrize("extra", [{"responseFields": "name"}, {"responseFields": [""]}, {"responseMaxItems": 0}])
def test_rejects_bad_response_settings(tmp_path, extra):
    a = api()
    a["tools"] = [tool(**extra)]
    with pytest.raises(SystemExit):
        runtime.load_project(write(tmp_path, {"name": "x", "apis": [a]}))


# ----------------------------------------------------------------- secrets ---

def test_env_beats_keychain(keychain, monkeypatch):
    keychain[("mcp-setup-gui", "w/key")] = "from-keychain"
    assert runtime.read_secret("secret://w/key") == ("from-keychain", "keychain")
    monkeypatch.setenv("MCP_SECRET_W_KEY", "from-env")
    assert runtime.read_secret("secret://w/key") == ("from-env", "env")


def test_newer_token_file_beats_env(keychain, monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_TOKEN_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_SECRET_W_OAUTH", json.dumps({"access_token": "old", "issued_at": 100}))
    (tmp_path / "MCP_SECRET_W_OAUTH.json").write_text(json.dumps({"access_token": "new", "issued_at": 200}))
    assert json.loads(runtime.read_secret("secret://w/oauth")[0])["access_token"] == "new"
    # A fresh sign-in pasted into the environment is newer than the file
    monkeypatch.setenv("MCP_SECRET_W_OAUTH", json.dumps({"access_token": "fresh", "issued_at": 300}))
    assert json.loads(runtime.read_secret("secret://w/oauth")[0])["access_token"] == "fresh"


def test_missing_secret_names_ref_not_value(keychain):
    project = {"name": "x", "apis": [api({"secretRef": "secret://x/k", "placement": {"in": "query", "name": "k"}})]}
    with pytest.raises(SystemExit, match="secret://x/k"):
        runtime.load_secrets(project)


# ---------------------------------------------------------------- upstream ---

@pytest.mark.anyio
async def test_builds_request(upstream):
    upstream.handler = lambda r: httpx.Response(200, json={"ok": True})
    t = tool(path="/things/{id}", method="POST",
             locations={"id": "path", "q": "query", "x_trace": {"in": "header", "name": "X-Trace"},
                        "title": "body", "body_id": {"in": "body", "name": "id"}})
    text = await runtime.call_upstream(api(), t, {"id": "a b/c", "q": "s", "x_trace": "1", "title": "T", "body_id": 7},
                                       None, 10_000)
    req = upstream.requests[0]
    assert json.loads(text) == {"ok": True}
    assert req.url.raw_path.split(b"?")[0] == b"/v1/things/a%20b%2Fc"  # path values can't add segments
    assert req.url.params["q"] == "s" and req.headers["x-trace"] == "1"
    assert json.loads(req.content) == {"title": "T", "id": 7}


@pytest.mark.anyio
async def test_dot_dot_path_value_blocked(upstream):
    with pytest.raises(ValueError, match="Bad value"):
        await runtime.call_upstream(api(), tool(), {"id": ".."}, None, 10_000)
    assert not upstream.requests


@pytest.mark.anyio
async def test_cannot_leave_api_host(upstream):
    upstream.handler = lambda r: httpx.Response(200, text="ok")
    await runtime.call_upstream(api(), tool(), {"id": "//evil.example.com"}, None, 10_000)
    assert upstream.requests[0].url.host == "api.example.com"


@pytest.mark.anyio
async def test_auth_in_query_and_header_wins(upstream):
    upstream.handler = lambda r: httpx.Response(200, text="ok")
    q = api({"secretRef": "secret://x", "placement": {"in": "query", "name": "appid"}})
    await runtime.call_upstream(q, tool(locations={"appid": "query"}, path="/w"), {"appid": "evil"}, "KEY", 10_000)
    assert upstream.requests[-1].url.params["appid"] == "KEY"

    h = api({"secretRef": "secret://x", "placement": {"in": "header", "name": "Authorization", "prefix": "Bearer "}})
    await runtime.call_upstream(h, tool(locations={"authorization": "header"}, path="/w"),
                                {"authorization": "Bearer evil"}, "KEY", 10_000)
    assert upstream.requests[-1].headers.get_list("authorization") == ["Bearer KEY"]


@pytest.mark.anyio
async def test_upstream_error_status(upstream):
    upstream.handler = lambda r: httpx.Response(404, text="nope")
    with pytest.raises(ValueError, match="404"):
        await runtime.call_upstream(api(), tool(), {"id": "1"}, None, 10_000)


@pytest.mark.anyio
async def test_network_error_hides_url_with_key(upstream):
    def boom(request):
        raise httpx.ConnectError(f"failed {request.url}")
    upstream.handler = boom
    q = api({"secretRef": "secret://x", "placement": {"in": "query", "name": "appid"}})
    with pytest.raises(ValueError) as err:
        await runtime.call_upstream(q, tool(path="/w", locations={}), {}, "SECRETKEY", 10_000)
    assert "SECRETKEY" not in str(err.value) and "ConnectError" in str(err.value)


# --------------------------------------------------------- response shaping ---

POKEMON = {"name": "pikachu", "moves": [{"move": {"name": f"m{i}", "url": "u"}} for i in range(100)],
           "stats": [{"base_stat": 35, "stat": {"name": "hp", "url": "u"}}], "types": [{"type": {"name": "electric"}}]}


def test_keep_fields_walks_lists():
    t = {"responseFields": ["name", "stats.base_stat", "stats.stat.name", "types.type.name", "nope"]}
    assert json.loads(runtime.shape_response(json.dumps(POKEMON), t)) == {
        "name": "pikachu", "stats": [{"base_stat": 35, "stat": {"name": "hp"}}], "types": [{"type": {"name": "electric"}}]}


def test_cap_lists():
    out = json.loads(runtime.shape_response(json.dumps(POKEMON), {"responseFields": ["moves.move.name"], "responseMaxItems": 2}))
    assert out["moves"] == [{"move": {"name": "m0"}}, {"move": {"name": "m1"}}, "... 98 more"]


def test_shaping_leaves_other_responses_alone():
    assert runtime.shape_response("plain text", {"responseFields": ["a"]}) == "plain text"
    assert runtime.shape_response('{"a": 1}', {}) == '{"a": 1}'


def test_truncate_marks_cut():
    assert runtime.truncate("é" * 10, 5).endswith("bytes]")
    assert runtime.truncate("short", 100) == "short"
