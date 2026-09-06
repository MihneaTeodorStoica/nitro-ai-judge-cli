"""OpenAPI 3.1 contract for the manager's versioned API (not proxy traffic)."""
from __future__ import annotations
import re
from ..play_protocol import ACTION_NAMES, BASE_PATH, MANAGER_VERSION


def contract() -> dict:
    string = {"type": "string"}
    boolean = {"type": "boolean"}
    number = {"type": "number"}
    obj = {"type": "object", "additionalProperties": True}
    def ref(name): return {"$ref": f"#/components/schemas/{name}"}
    def array(item): return {"type": "array", "items": item}
    def model(properties, required=()):
        return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": True}
    schemas = {
        "Error": model({"type": string, "message": string, "stage": {"type": ["string", "null"]}, "logs": array(string)}, ("type", "message")),
        "ErrorResponse": model({"error": ref("Error")}, ("error",)),
        "Info": model({"identity": string, "manager_version": string, "api_version": {"type": "integer"}, "minimum_cli_version": string}, ("identity", "manager_version", "api_version", "minimum_cli_version")),
        "Health": model({"status": {"enum": ["healthy", "unhealthy"]}}, ("status",)),
        "Image": model({"name": string, "state": string, "id": string}),
        "Competition": model({"reference": string, "organization": string, "competition": string, "title": string,
            "workspace_state": string, "image_state": string, "service_health": string,
            "jupyter_url": {"type": ["string", "null"]}, "proxy_url": {"type": ["string", "null"]},
            "images": {"type": "object", "additionalProperties": ref("Image")},
            "operation": ref("Operation"), "explicit_stopped": boolean}, ("reference",)),
        "Event": model({"sequence": {"type": "integer"}, "stage": string, "message": string, "created_at": number}, ("sequence", "stage", "message", "created_at")),
        "Operation": model({"id": string, "competition": string, "action": {"enum": sorted(ACTION_NAMES)},
            "status": {"enum": ["queued", "running", "complete", "failed", "cancelled", "interrupted"]},
            "stage": string, "message": string, "created_at": number, "updated_at": number,
            "options": obj, "result": {"anyOf": [ref("Competition"), {"type": "null"}]},
            "error": {"anyOf": [ref("Error"), {"type": "null"}]}, "events": array(ref("Event"))},
            ("id", "competition", "action", "status", "stage", "message", "created_at", "updated_at")),
        "ActionOptions": model({"gpu": {"type": ["boolean", "null"]}, "pull": {"enum": ["always", "missing", "never"]},
            "wait_timeout": {"type": "integer", "minimum": 1}, "force": boolean, "confirm_ref": string}),
        "Accepted": model({"operation_id": string, "operation": ref("Operation")}, ("operation_id", "operation")),
        "Credentials": model({"access_token": string, "refresh_token": string, "username": string,
            "access_token_exp": number, "refresh_token_exp": number, "api_base_url": string}, ("access_token", "refresh_token")),
        "Login": model({"username": string, "password": string}, ("username", "password")),
        "Adoption": model({"organization": string, "competition": string, "reference": string,
                            "project": string, "container_id": string, "running": boolean,
                            "workspace_kind": string, "workspace_volume": string,
                            "notebook_image": string, "proxy_image": string, "verified": boolean}),
    }
    schemas["Operation"]["properties"].update({
        "started_at": {"type": ["number", "null"]}, "finished_at": {"type": ["number", "null"]},
        "duration": {"type": "number", "minimum": 0}, "failure": ref("Error"),
    })
    paths = {}
    def operation(path, method, summary, schema, *, body=None, public=False, code="200", query=(), media="application/json"):
        parameters = []
        for name in re.findall(r"\{(\w+)\}", path):
            parameters.append({"name": name, "in": "path", "required": True,
                "schema": {"enum": sorted(ACTION_NAMES)} if name == "action" else string})
        for name, spec in query:
            parameters.append({"name": name, "in": "query", "required": False, "schema": spec})
        if method not in {"get", "head"}:
            parameters.append({"name": "Origin", "in": "header", "required": False,
                               "description": "Required for browser sessions; must equal the configured manager origin", "schema": string})
        security = [] if public else [{"cliBearer": []}, {"browserSession": [], **({"csrfHeader": []} if method not in {"get", "head"} else {})}]
        value = {"operationId": method + "_" + re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_"),
                 "summary": summary, "parameters": parameters, "security": security,
                 "responses": {code: {"description": "Success", "content": {media: {"schema": schema}}},
                               "default": {"description": "Structured error; Host validation applies to all routes", "content": {"application/json": {"schema": ref("ErrorResponse")}}}}}
        for status_code in ("400", "401", "403", "404", "409", "500"):
            value["responses"][status_code] = value["responses"]["default"]
        if body is not None:
            value["requestBody"] = {"required": not path.endswith("/actions/{action}"), "content": {"application/json": {"schema": body}}}
        paths.setdefault(path, {})[method] = value
    base = "/api/v1"
    operation(base+"/info", "get", "Manager identity and compatibility", ref("Info"), public=True)
    operation(base+"/health", "get", "Runtime health (503 when unhealthy)", ref("Health"), public=True)
    paths[base+"/health"]["get"]["responses"]["503"] = {"description": "Unhealthy runtime", "content": {"application/json": {"schema": ref("Health")}}}
    operation(base+"/openapi.json", "get", "This OpenAPI contract", obj, public=True)
    operation(base+"/competitions", "get", "List environments (optionally merge Nitro competition catalogue)",
              model({"competitions": array(ref("Competition")), "login_sync_required": boolean}, ("competitions", "login_sync_required")),
              query=(("cached", {"enum": ["true", "false"]}), ("refresh", {"enum": ["true", "false"]})))
    scope = base+"/competitions/{org}/{competition}"
    operation(scope, "get", "Refresh one environment snapshot", ref("Competition"))
    operation(scope+"/images", "get", "Competition image availability", obj)
    operation(scope+"/open", "get", "Stable browser routes; 409 when not running", model({"jupyter_url": string, "proxy_url": string}, ("jupyter_url", "proxy_url")))
    operation(scope+"/actions/{action}", "post", "Queue an idempotent operation; 409 on conflict or unconfirmed workspace deletion", ref("Accepted"), body=ref("ActionOptions"), code="202")
    operation(scope+"/logs", "get", "Redacted recent logs", model({"logs": string, "tail": {"type": "integer"}}, ("logs", "tail")), query=(("tail", {"type": "integer", "minimum": 1, "maximum": 2000, "default": 80}),))
    operation(scope+"/logs/follow", "get", 'Redacted NDJSON stream: each line is {"line": string}', string, media="application/x-ndjson")
    operation(base+"/events", "get", "SSE sync/refresh events with heartbeat comments", string, media="text/event-stream")
    operation(base+"/operations", "get", "Redacted recent operation summaries", model({"operations": array(ref("Operation")), "next_offset": {"type": ["integer", "null"]}}, ("operations",)),
              query=(("limit", {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}), ("offset", {"type": "integer", "minimum": 0}), ("competition", string), ("status", string), ("action", {"enum": sorted(ACTION_NAMES)})))
    operation(base+"/operations/{operation_id}", "get", "Operation state and sequenced events", ref("Operation"))
    operation(base+"/operations/{operation_id}/cancel", "post", "Cancel exact operation; terminal operations are returned unchanged", ref("Operation"))
    operation(base+"/credentials", "put", "Synchronize Nitro tokens (never returned)", model({"synchronized": boolean}, ("synchronized",)), body=ref("Credentials"))
    operation(base+"/credentials", "delete", "Remove manager Nitro credentials only", model({"synchronized": boolean}, ("synchronized",)))
    operation(base+"/login", "post", "Connect Nitro account", model({"authenticated": boolean, "username": string}, ("authenticated", "username")), body=ref("Login"))
    operation(base+"/logout", "post", "Expire the current browser session only", model({"logged_out": boolean}))
    operation(base+"/legacy-adoptions", "post", "Accept verified sanitized migration manifests", model({"adopted": {"type": "integer", "minimum": 0}}, ("adopted",)), body=model({"manifests": {**array(ref("Adoption")), "maxItems": 500}}, ("manifests",)))
    return {"openapi": "3.1.0", "info": {"title": "NAIJ Play Manager API", "version": MANAGER_VERSION,
            "description": "API v1. CLI uses bearer auth. Browser mutations require session, same Origin and CSRF. Every route requires an allowed Host. Proxy/browser assets are outside this API."},
            "servers": [{"url": BASE_PATH}], "paths": paths,
            "components": {"schemas": schemas, "securitySchemes": {
                "cliBearer": {"type": "http", "scheme": "bearer"},
                "browserSession": {"type": "apiKey", "in": "cookie", "name": "naij_manager_session"},
                "csrfHeader": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"}}}}
