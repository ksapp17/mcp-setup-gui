import pytest

import import_openapi as imp

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Demo", "version": "1"},
    "servers": [{"url": "https://api.demo.example.com/v1"}],
    "components": {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}},
        "schemas": {"Pet": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    },
    "security": [{"key": []}],
    "paths": {
        "/pets/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "getPet", "summary": "Get one pet by its id", "parameters": [
                {"name": "fields", "in": "query", "schema": {"type": "string"}}]},
            "delete": {"operationId": "deletePet", "summary": "Delete a pet"},
        },
        "/pets": {"post": {"operationId": "addPet", "summary": "Add a pet", "requestBody": {
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}},
    },
}


def tools():
    spec = imp.deref(SPEC, SPEC)
    found, skipped = imp.candidate_tools(spec, [])
    return {c["tool"]["name"]: c["tool"] for c in found}, skipped


def test_operations_become_tools():
    found, skipped = tools()
    assert not skipped
    get = next(t for n, t in found.items() if "get" in n.lower())
    assert get["paramLocations"]["petId"] == "path" and get["paramLocations"]["fields"] == "query"
    assert "petId" in get["inputSchema"]["required"]
    assert get["annotations"].get("readOnlyHint") is True


def test_delete_is_marked_destructive():
    found, _ = tools()
    delete = next(t for n, t in found.items() if "delete" in n.lower())
    assert delete["annotations"].get("destructiveHint") is True


def test_ref_body_resolved():
    found, _ = tools()
    add = next(t for n, t in found.items() if "add" in n.lower())
    assert "name" in add["inputSchema"]["properties"]


def test_auth_detection():
    auth, _ = imp.detect_auth(SPEC, "demo", "demo", [])
    assert auth["placement"] == {"in": "header", "name": "X-Key"}
    assert auth["secretRef"].startswith("secret://demo/demo/")


def test_oauth_flow_becomes_oauth2_block():
    spec = dict(SPEC, security=[{"gh": ["read:user"]}], components={"securitySchemes": {"gh": {
        "type": "oauth2", "flows": {"authorizationCode": {
            "authorizationUrl": "https://id.example.com/authorize", "tokenUrl": "https://id.example.com/token",
            "scopes": {"read:user": "", "repo": ""}}}}}})
    warnings = []
    auth, _ = imp.detect_auth(spec, "demo", "demo", warnings)
    assert auth["oauth2"]["scopes"] == ["read:user"] and auth["oauth2"]["clientId"] == ""
    assert any("OAuth" in w for w in warnings)


def test_relative_server_needs_base_url():
    spec = dict(SPEC, servers=[{"url": "/v1"}])
    with pytest.raises(imp.SpecError):
        imp.base_url(spec, "spec.json", None)
    assert imp.base_url(spec, "https://demo.example.com/openapi.json", None) == "https://demo.example.com/v1"
