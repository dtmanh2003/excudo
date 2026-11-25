"""The dev utility's tools (from branch loc): dependencies, SQLite databases, Docker, API
requests and security scans. Enabled with EXCUDO_UTILITIES=...,dev.

Every handler goes through `_wrap`: arguments starting with "_" (loc's internal switches such
as `_allow_sensitive`) are dropped so the model can never set them, and the structured result
becomes text for the model. Risks are set in permissions.TOOL_RISK_MAP (reads SAFE, running
programs EXECUTE, network EXTERNAL).
"""

import json

from ...tools import Tool
from . import api_testing, containers, database, dependencies, scans
from .results import ToolResult


# Call a loc handler with only public arguments and turn its ToolResult into text.
def _wrap(handler):
    def run(**args):
        public = {key: value for key, value in args.items() if not str(key).startswith("_")}
        try:
            result = handler(**public)
        except TypeError as error:
            return f"Error: invalid arguments ({error})."
        if isinstance(result, ToolResult):
            return result.to_model_message(20_000)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    return run


TOOLS: dict[str, Tool] = {}

_SCAN_DESCRIPTIONS = {
    "security_scan": "Run bounded local static checks of source, config, Docker files and secrets without executing project code. No network lookups, no automatic fixes.",
    "dependency_vulnerability_scan": "Look up the project's resolved dependency versions in the public OSV vulnerability database (network; package names and versions leave the computer). Never updates dependencies.",
    "security_check": "Re-scan one structured security assertion deterministically. No exploit testing.",
}
for _name, _schema in scans.schemas().items():
    if _name in _SCAN_DESCRIPTIONS:  # zun_loc's own secret_scan is kept
        TOOLS[_name] = Tool(name=_name, description=_SCAN_DESCRIPTIONS[_name], parameters=_schema, handler=_wrap(getattr(scans, _name)))

_DOCKER_DESCRIPTIONS = {
    "docker_info": "Inspect local Linux Docker availability and the project's Docker / Compose configuration (static); never starts Compose.",
    "docker_list": "List containers / images created by this Excudo process in this workspace only.",
    "docker_inspect": "Inspect selected state of a container managed by this session.",
    "docker_build": "Build a workspace-local image (approved). RUN steps have no network; pulling base images needs network. No secrets, SSH, mounts or remote contexts.",
    "docker_run": "Create a development container (approved) with fixed limits, no writable host mounts and loopback-only ports. Stop / remove it when done.",
    "docker_logs": "Read bounded, masked logs of a managed container.",
    "docker_exec": "Run a command (argument list, no shell) in a running managed container (approved).",
    "docker_stop": "Stop a container managed by this session (approved).",
    "docker_remove": "Remove a stopped container managed by this session (approved); volumes, images and networks are kept.",
    "container_check": "Verify a managed container's status, exit code, health, a log substring or a loopback port with explicit assertions.",
}
for _name, _schema in containers.schemas().items():
    TOOLS[_name] = Tool(name=_name, description=_DOCKER_DESCRIPTIONS[_name], parameters=_schema, handler=_wrap(getattr(containers, _name)))

_DB_DESCRIPTIONS = {
    "database_info": "Detect the project's database setup and inspect a configured SQLite target without importing application code. Production targets are blocked.",
    "database_schema": "Show a SQLite database's tables, columns, keys and indexes (bounded).",
    "database_query": "Run one bounded read-only SELECT / WITH ... SELECT / EXPLAIN on a SQLite database (other databases cannot be attached).",
    "database_check": "Verify a bounded read-only query against explicit assertions.",
    "database_execute": "Run one INSERT / UPDATE / DELETE on a SQLite profile configured for development or test (EXCUDO_DATABASE_PROFILES), in a transaction (approved). Rows cannot be restored by rollback.",
    "migration_status": "Show migration files and SQLite migration bookkeeping. Never applies migrations.",
}
for _name, _schema in database.schemas().items():
    TOOLS[_name] = Tool(name=_name, description=_DB_DESCRIPTIONS[_name], parameters=_schema, handler=_wrap(getattr(database, _name)))

for _name in sorted(api_testing.API_TOOLS):
    _properties = {
        "url": {"type": "string"},
        "headers": {"type": "object", "description": "Explicit headers; credential values are masked in results"},
        "query": {"type": "object", "description": "Query parameter names and values"},
        "timeout": {"type": ["number", "null"]},
        "target_scope": {"type": "string", "enum": ["public", "loopback"],
                         "description": "public: internet APIs; loopback: an API running on this computer (localhost only)"},
    }
    if _name != "inspect_openapi":
        _properties.update({"method": {"type": "string", "enum": list(api_testing.METHODS)}, "json_body": {}, "text_body": {"type": "string"}})
    if _name == "api_check":
        _properties.update({
            "expected_status": {"type": ["integer", "array"], "items": {"type": "integer"}},
            "expected_headers": {"type": "object"},
            "body_contains": {"type": "string"},
            "json_assertions": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "operator": {"type": "string", "enum": ["exists", "equals"]}, "value": {}},
                "required": ["path", "operator"], "additionalProperties": False}},
        })
    TOOLS[_name] = Tool(
        name=_name,
        description={
            "api_request": "Send one bounded HTTP request to a public API or to an API on this computer (loopback). No redirects or retries; the request is approved first. Company-network addresses are blocked.",
            "api_check": "Send one approved API request and verify status / headers / body / JSON fields against explicit assertions.",
            "inspect_openapi": "Read one OpenAPI JSON document from an approved URL and summarize its operations (bounded).",
        }[_name],
        parameters={"type": "object", "properties": _properties,
                    "required": ["url"] if _name == "inspect_openapi" else ["method", "url"], "additionalProperties": False},
        handler=_wrap(getattr(api_testing, _name)),
    )

for _name in sorted(dependencies.DEPENDENCY_TOOLS):
    _properties = {"cwd": {"type": "string", "description": "Project folder inside the workspace"}}
    if _name != "dependency_info":
        _properties["timeout"] = {"type": ["number", "null"]}
    if _name == "dependency_tree":
        _properties["depth"] = {"type": "integer", "description": "Maximum depth, 0 to 10 (default 3)"}
    _mutation = _name in dependencies.DEPENDENCY_MUTATIONS
    if _mutation:
        _properties.update({"name": {"type": "string", "description": "One registry package name; no URLs, paths or flags"},
                            "group": {"type": "string", "enum": ["main", "dev"]}})
        if _name != "remove_dependency":
            _properties["version"] = {"type": ["string", "null"], "description": "Concrete version or bounded constraint"}
        if _name == "update_dependency":
            _properties["strategy"] = {"type": "string", "enum": ["latest-compatible"]}
    TOOLS[_name] = Tool(
        name=_name,
        description={
            "dependency_info": "Read declared dependencies and lockfile versions without running code.",
            "dependency_tree": "Read a bounded offline dependency graph or installed package list.",
            "check_dependencies": "Check local dependency health without installing or rewriting lockfiles.",
            "outdated_dependencies": "Ask the package registry for newer versions (network).",
            "add_dependency": "Add one registry dependency to the main / dev group (network; approved; install scripts disabled).",
            "remove_dependency": "Remove one declared dependency (approved).",
            "update_dependency": "Update one declared dependency (network; approved; install scripts disabled).",
        }[_name] + " No global installs or arbitrary flags; a Python project needs its own .venv.",
        parameters={"type": "object", "properties": _properties, "required": ["name"] if _mutation else [], "additionalProperties": False},
        handler=_wrap(getattr(dependencies, _name)),
    )
