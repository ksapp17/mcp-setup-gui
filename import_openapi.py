"""Import an OpenAPI 3.x spec into project.json.

Usage:
    uv run import_openapi.py SPEC [options]

SPEC is a file path or an http(s) URL, JSON or YAML.

Options:
    --out PATH         project file to create or add to (default: project.json)
    --name NAME        project name, used as the server name in Claude (new projects only)
    --api-id ID        id for this API inside the project (default: from the spec's title)
    --base-url URL     override the spec's server URL
    --filter TEXT      only offer operations whose path, name or tag contains TEXT
    --select WHICH     skip the picker: "readonly", "all", or comma-separated tool names
    --force            replace an API with the same id that's already in the project

What it does:
  1. Loads the spec and resolves its internal $refs
  2. Works out the base URL and the auth scheme
  3. Turns every operation into a candidate tool: path, query, header and JSON body
     parameters become one input schema; GET/HEAD are marked read-only, DELETE destructive
  4. Lets you pick which become tools (read-only ones are pre-selected)
  5. Writes or merges the API into project.json and tells you how to store the secret
"""

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
import yaml

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
KEYCHAIN_SERVICE = "mcp-setup-gui"
TOOL_WARN_COUNT = 25
WEAK_DESCRIPTION = 40  # characters; shorter descriptions make it hard for Claude to choose
MAX_DESCRIPTION = 1000
OPENAPI_ONLY_KEYS = {"example", "xml", "externalDocs", "discriminator", "nullable"}


class SpecError(Exception):
    pass


# --------------------------------------------------------------- load spec ---

def load_spec(source):
    if re.match(r"https?://", source):
        try:
            response = httpx.get(source, follow_redirects=True, timeout=30)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise SpecError(f"Couldn't download the spec: {e}")
        text = response.text
    else:
        path = Path(source)
        if not path.is_file():
            raise SpecError(f"No file at {path.resolve()}")
        text = path.read_text(encoding="utf-8")

    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise SpecError(f"The spec is neither valid JSON nor valid YAML: {e}")

    if not isinstance(spec, dict):
        raise SpecError("The spec isn't an OpenAPI document")
    if "swagger" in spec:
        raise SpecError("This is a Swagger 2.0 spec. Convert it to OpenAPI 3 first "
                           "(for example with the swagger2openapi tool), then import that.")
    if not str(spec.get("openapi", "")).startswith("3."):
        raise SpecError("Only OpenAPI 3.0 and 3.1 specs are supported")
    return spec


def resolve_pointer(root, ref):
    if not ref.startswith("#/"):
        raise SpecError(f"External reference {ref!r} isn't supported yet")
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        try:
            node = node[part]
        except (KeyError, TypeError):
            raise SpecError(f"Broken reference {ref!r}")
    return node


def deref(node, root, chain=()):
    """Replace every {"$ref": ...} with a copy of what it points to.
    A ref that points back into its own chain (recursive schema) becomes a plain object."""
    if isinstance(node, list):
        return [deref(item, root, chain) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        if ref in chain:
            return {"type": "object", "description": "(recursive structure, not expanded)"}
        target = deref(resolve_pointer(root, ref), root, chain + (ref,))
        siblings = {k: v for k, v in node.items() if k != "$ref"}  # allowed in 3.1
        if siblings and isinstance(target, dict):
            target = {**target, **deref(siblings, root, chain)}
        return target
    return {k: deref(v, root, chain) for k, v in node.items()}


# ----------------------------------------------------------- spec -> project ---

def slug(text, sep="-"):
    text = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", str(text))  # camelCase -> camel_Case
    text = re.sub(r"[^A-Za-z0-9]+", sep, text).strip(sep).lower()
    return text or "api"


def base_url(spec, source, override):
    if override:
        return override.rstrip("/")
    servers = spec.get("servers") or [{"url": "/"}]
    server = servers[0]
    url = server.get("url", "/")
    for name, var in (server.get("variables") or {}).items():
        url = url.replace("{" + name + "}", str(var.get("default", "")))
    if url.startswith("http://") or url.startswith("https://"):
        return url.rstrip("/")
    if re.match(r"https?://", source):
        return urljoin(source, url).rstrip("/")
    raise SpecError(f"The spec's server URL is relative ({url!r}). Give the full base URL (--base-url on the command line).")


def detect_auth(spec, project_name, api_id, warnings):
    schemes = (spec.get("components") or {}).get("securitySchemes") or {}
    if not schemes:
        return None, None

    # Prefer the first scheme the spec says applies globally, else the first defined
    chosen = None
    for requirement in spec.get("security") or []:
        for name in requirement:
            if name in schemes:
                chosen = name
                break
        if chosen:
            break
    chosen = chosen or next(iter(schemes))
    scheme = schemes[chosen]
    kind = scheme.get("type")

    if kind == "apiKey" and scheme.get("in") in ("header", "query"):
        placement = {"in": scheme["in"], "name": scheme["name"]}
    elif kind == "apiKey":
        warnings.append(f"Auth scheme '{chosen}' puts the key in a cookie, which isn't supported. "
                        "No auth was configured.")
        return None, chosen
    elif kind == "http" and str(scheme.get("scheme", "")).lower() == "bearer":
        placement = {"in": "header", "name": "Authorization", "prefix": "Bearer "}
    elif kind == "oauth2" and (scheme.get("flows") or {}).get("authorizationCode"):
        flow = scheme["flows"]["authorizationCode"]
        placement = {"in": "header", "name": "Authorization", "prefix": "Bearer "}
        # Scopes: the ones the spec's global security requirement asks for, else none (edit later)
        wanted = next((list(r[chosen]) for r in spec.get("security") or [] if r.get(chosen)), [])
        base = slug(project_name) + "/" + slug(api_id)
        oauth2 = {
            "authorizeUrl": flow.get("authorizationUrl", ""),
            "tokenUrl": flow.get("tokenUrl", ""),
            "clientId": "",
            "clientSecretRef": f"secret://{base}/clientSecret",
            "scopes": wanted,
        }
        warnings.append(f"Auth scheme '{chosen}' is OAuth. Register an OAuth app with the provider "
                        "(redirect URL http://127.0.0.1/callback), fill in its client ID, store its "
                        "client secret, then sign in.")
        if not wanted and flow.get("scopes"):
            warnings.append("No scopes were chosen. Available: " + ", ".join(list(flow["scopes"])[:15])
                            + ". Add the ones you need to auth.oauth2.scopes.")
        if len(schemes) > 1:
            warnings.append(f"The spec defines {len(schemes)} auth schemes; using '{chosen}' for every tool.")
        return {"secretRef": f"secret://{base}/oauth", "placement": placement, "oauth2": oauth2}, chosen
    elif kind in ("oauth2", "openIdConnect"):
        warnings.append(f"Auth scheme '{chosen}' is OAuth without an authorization code flow, which "
                        "isn't supported. Configured as a bearer token instead: store a token you got "
                        "from the provider.")
        placement = {"in": "header", "name": "Authorization", "prefix": "Bearer "}
    else:
        warnings.append(f"Auth scheme '{chosen}' ({kind} {scheme.get('scheme', '')}) isn't supported yet. "
                        "No auth was configured.")
        return None, chosen

    if len(schemes) > 1:
        warnings.append(f"The spec defines {len(schemes)} auth schemes; using '{chosen}' for every tool.")
    secret_ref = f"secret://{slug(project_name)}/{slug(api_id)}/{slug(chosen)}"
    return {"secretRef": secret_ref, "placement": placement}, chosen


def clean_schema(schema, for_request=False):
    """Turn an OpenAPI schema into plain JSON Schema."""
    if isinstance(schema, list):
        return [clean_schema(s, for_request) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in OPENAPI_ONLY_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {
                name: clean_schema(prop, for_request)
                for name, prop in value.items()
                # readOnly fields (like a server-generated id) can't be sent in a request
                if not (for_request and isinstance(prop, dict) and prop.get("readOnly"))
            }
        elif key in ("items", "additionalProperties", "not") and isinstance(value, dict):
            out[key] = clean_schema(value, for_request)
        elif key in ("allOf", "anyOf", "oneOf"):
            out[key] = clean_schema(value, for_request)
        else:
            out[key] = value
    if schema.get("nullable") and "type" in out and isinstance(out["type"], str):
        out["type"] = [out["type"], "null"]  # OpenAPI 3.0 nullable -> JSON Schema
    if "required" in out and "properties" in out:
        out["required"] = [r for r in out["required"] if r in out["properties"]]
        if not out["required"]:
            del out["required"]
    return out


def json_body_schema(request_body):
    for media, content in (request_body.get("content") or {}).items():
        if media == "application/json" or media.endswith("+json"):
            return content.get("schema") or {}
    return None


def describe(op, method, path):
    summary = (op.get("summary") or "").strip()
    detail = (op.get("description") or "").strip()
    if summary and detail and not detail.startswith(summary):
        text = f"{summary}. {detail}"
    else:
        text = summary or detail or f"{method.upper()} {path}"
    text = re.sub(r"\s+", " ", text)
    if len(text) > MAX_DESCRIPTION:
        text = text[: MAX_DESCRIPTION - 3].rsplit(" ", 1)[0] + "..."
    return text


def tool_name(op, method, path, taken):
    base = slug(op.get("operationId") or f"{method}_{path}", sep="_")[:64]
    name, n = base, 2
    while name in taken:
        suffix = f"_{n}"
        name = base[: 64 - len(suffix)] + suffix
        n += 1
    taken.add(name)
    return name


def build_tool(op, method, path, path_params, taken, warnings):
    properties, required, locations = {}, [], {}

    # Path-level parameters apply to every operation; operation-level ones override them
    params = {(p.get("name"), p.get("in")): p for p in path_params}
    params.update({(p.get("name"), p.get("in")): p for p in op.get("parameters") or []})

    for (name, where), param in params.items():
        if where == "cookie":
            warnings.append(f"{method.upper()} {path}: cookie parameter '{name}' skipped")
            continue
        if where not in ("path", "query", "header"):
            continue
        arg = name if name not in properties else f"{where}_{name}"
        schema = clean_schema(param.get("schema") or {"type": "string"})
        if param.get("description") and "description" not in schema:
            schema["description"] = param["description"].strip()
        properties[arg] = schema
        locations[arg] = where if arg == name else {"in": where, "name": name}
        if param.get("required") or where == "path":
            required.append(arg)

    request_body = op.get("requestBody")
    if request_body:
        body = json_body_schema(request_body)
        if body is None:
            kinds = ", ".join(request_body.get("content", {}).keys()) or "unknown"
            return None, f"{method.upper()} {path}: request body isn't JSON ({kinds})"
        body = clean_schema(body, for_request=True)
        body_required = bool(request_body.get("required"))
        if body.get("type", "object") == "object" and body.get("properties"):
            for field, schema in body["properties"].items():
                arg = field if field not in properties else f"body_{field}"
                properties[arg] = schema
                locations[arg] = "body" if arg == field else {"in": "body", "name": field}
                if body_required and field in body.get("required", []):
                    required.append(arg)
        else:
            properties["body"] = body
            locations["body"] = "bodyRaw"
            if body_required:
                required.append("body")

    input_schema = {"type": "object", "properties": properties}
    if required:
        input_schema["required"] = required

    if method in ("get", "head"):
        annotations = {"readOnlyHint": True}
    elif method == "delete":
        annotations = {"readOnlyHint": False, "destructiveHint": True}
    else:
        annotations = {"readOnlyHint": False}

    tool = {
        "name": tool_name(op, method, path, taken),
        "description": describe(op, method, path),
        "method": method.upper(),
        "path": path,
        "paramLocations": locations,
        "inputSchema": input_schema,
        "annotations": annotations,
        "enabled": True,
    }
    return tool, None


def candidate_tools(spec, warnings):
    taken, tools, skipped = set(), [], []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        path_params = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            tool, problem = build_tool(op, method, path, path_params, taken, warnings)
            if problem:
                skipped.append(problem)
                continue
            tools.append({"tool": tool, "tags": op.get("tags") or [],
                          "deprecated": bool(op.get("deprecated")),
                          "operationId": op.get("operationId")})
    return tools, skipped


# ------------------------------------------------------------------ picker ---

def parse_numbers(text, count):
    picked = set()
    for part in text.replace(",", " ").split():
        if "-" in part:
            lo, hi = part.split("-", 1)
            picked.update(range(int(lo), int(hi) + 1))
        else:
            picked.add(int(part))
    if any(n < 1 or n > count for n in picked):
        raise ValueError
    return picked


def pick(candidates, select):
    def is_read_only(c):
        return c["tool"]["annotations"].get("readOnlyHint", False)

    if select == "all":
        return candidates
    if select == "readonly":
        return [c for c in candidates if is_read_only(c)]
    if select:
        wanted = {s.strip() for s in select.split(",") if s.strip()}
        chosen = [c for c in candidates if c["tool"]["name"] in wanted or c["operationId"] in wanted]
        missing = wanted - {c["tool"]["name"] for c in chosen} - {c["operationId"] for c in chosen}
        if missing:
            raise SpecError(f"No operation named: {', '.join(sorted(missing))}")
        return chosen

    if not sys.stdin.isatty():
        print("No terminal to ask in, so only read-only operations were selected. Use --select to choose.")
        return [c for c in candidates if is_read_only(c)]

    selected = {i for i, c in enumerate(candidates, 1) if is_read_only(c) and not c["deprecated"]}
    while True:
        print()
        for i, c in enumerate(candidates, 1):
            t = c["tool"]
            mark = "x" if i in selected else " "
            flags = ""
            if t["annotations"].get("destructiveHint"):
                flags += "  [destructive]"
            elif not is_read_only(c):
                flags += "  [writes]"
            if c["deprecated"]:
                flags += "  [deprecated]"
            print(f"[{mark}] {i:>3}. {t['name']:<40} {t['method']:<6} {t['path']}{flags}")
        print(f"\n{len(selected)} of {len(candidates)} selected.", end="")
        if len(selected) > TOOL_WARN_COUNT:
            print(f" More than {TOOL_WARN_COUNT} tools makes it harder for Claude to pick the right one.", end="")
        answer = input("\nToggle numbers (e.g. 3 5-7), a=all, r=read-only, n=none, Enter=done: ").strip().lower()
        if answer == "":
            if not selected:
                print("Select at least one operation.")
                continue
            return [c for i, c in enumerate(candidates, 1) if i in selected]
        if answer == "a":
            selected = set(range(1, len(candidates) + 1))
        elif answer == "r":
            selected = {i for i, c in enumerate(candidates, 1) if is_read_only(c)}
        elif answer == "n":
            selected = set()
        else:
            try:
                selected ^= parse_numbers(answer, len(candidates))
            except ValueError:
                print(f"Use numbers from 1 to {len(candidates)}.")


# ------------------------------------------------------------- write project ---

def write_project(out_path, api, project_name, force):
    if out_path.is_file():
        project = json.loads(out_path.read_text(encoding="utf-8"))
        existing = [a for a in project.get("apis", []) if a.get("id") == api["id"]]
        if existing and not force:
            raise SpecError(f"{out_path.name} already has an API with id '{api['id']}'. "
                               f"Use --force to replace it, or --api-id to add it alongside.")
        project["apis"] = [a for a in project.get("apis", []) if a.get("id") != api["id"]]
        # Tool names must be unique across the whole project
        others = {t["name"] for a in project["apis"] for t in a.get("tools", [])}
        for tool in api["tools"]:
            if tool["name"] in others:
                tool["name"] = f"{slug(api['id'], '_')}_{tool['name']}"[:64]
        project["apis"].append(api)
    else:
        project = {"name": project_name, "responseByteCap": 50000, "apis": [api]}

    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(project, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, out_path)
    return project


def main():
    parser = argparse.ArgumentParser(description="Import an OpenAPI 3 spec into project.json")
    parser.add_argument("spec", help="Path or URL of the OpenAPI spec (JSON or YAML)")
    parser.add_argument("--out", default="project.json")
    parser.add_argument("--name", help="Project name (new projects only)")
    parser.add_argument("--api-id")
    parser.add_argument("--base-url")
    parser.add_argument("--filter")
    parser.add_argument("--select")
    parser.add_argument("--force", action="store_true")
    opts = parser.parse_args()

    try:
        raw = load_spec(opts.spec)
        spec = deref(raw, raw)
        title = (spec.get("info") or {}).get("title") or "api"
        out_path = Path(opts.out).resolve()

        if out_path.is_file():
            project_name = json.loads(out_path.read_text(encoding="utf-8")).get("name")
        else:
            project_name = opts.name or slug(title)
        api_id = opts.api_id or slug(title)

        warnings = []
        url = base_url(spec, opts.spec, opts.base_url)
        auth, scheme_name = detect_auth(spec, project_name, api_id, warnings)
        candidates, skipped = candidate_tools(spec, warnings)

        if opts.filter:
            needle = opts.filter.lower()
            candidates = [c for c in candidates
                          if needle in c["tool"]["path"].lower()
                          or needle in c["tool"]["name"]
                          or any(needle in str(tag).lower() for tag in c["tags"])]
        if not candidates:
            raise SpecError("No operations to import" + (" match the filter" if opts.filter else ""))

        print(f"{title}: {len(candidates)} operation(s), base URL {url}")
        if auth:
            p = auth["placement"]
            print(f"Auth: '{scheme_name}', sent in the {p['in']} '{p['name']}'"
                  + (f" as '{p['prefix']}<token>'" if p.get("prefix") else ""))
        else:
            print("Auth: none")

        chosen = pick(candidates, opts.select)
        api = {"id": api_id, "baseUrl": url, "source": "openapi"}
        if auth:
            api["auth"] = auth
        api["tools"] = [c["tool"] for c in chosen]

        write_project(out_path, api, project_name, opts.force)

        print(f"\nWrote {len(api['tools'])} tool(s) to {out_path}")
        weak = [t["name"] for t in api["tools"] if len(t["description"]) < WEAK_DESCRIPTION]
        if weak:
            print("\nThese descriptions are short. Claude decides when to call a tool from its "
                  "description, so improve them in project.json:")
            for name in weak:
                print(f"  - {name}")
        for message in warnings + skipped:
            print(f"Note: {message}")
        if skipped:
            print(f"({len(skipped)} operation(s) skipped)")

        print("\nNext:")
        if auth and auth.get("oauth2"):
            print(f"  1. Register an OAuth app on {auth['oauth2']['authorizeUrl'].split('/')[2]} with redirect URL "
                  "http://127.0.0.1/callback, and put its client ID in auth.oauth2.clientId")
            print(f"  2. uv run upstream_oauth.py client-secret --project {out_path.name}")
            print(f"  3. uv run upstream_oauth.py login --project {out_path.name}")
            print("  4. Quit Claude Desktop, then install:")
        elif auth:
            account = auth["secretRef"].removeprefix("secret://")
            print("  1. Store the secret (input is hidden):")
            print(f"     uv run python -c \"import keyring, getpass; keyring.set_password("
                  f"'{KEYCHAIN_SERVICE}', '{account}', getpass.getpass('Value: '))\"")
            print("  2. Quit Claude Desktop, then install:")
        else:
            print("  1. Quit Claude Desktop, then install:")
        print(f"     uv run install.py add --project {out_path.name}")
    except SpecError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was written.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
