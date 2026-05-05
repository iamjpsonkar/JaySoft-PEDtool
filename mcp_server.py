"""
MCP (Model Context Protocol) Server for PED Tools.

Implements MCP over SSE (Server-Sent Events) as a Flask Blueprint.
Exposes proxy/mock management tools for AI assistants (Claude, etc.).

Transport: SSE at /mcp/sse (event stream) + POST /mcp/messages (client messages)
Protocol: JSON-RPC 2.0 per MCP specification

Usage in Claude Desktop config:
{
  "mcpServers": {
    "ped-tools": {
      "url": "https://your-server.com/mcp/sse"
    }
  }
}
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid

from flask import Blueprint, Response, request, jsonify

logger = logging.getLogger("pedapp.mcp")

mcp_bp = Blueprint("mcp", __name__, url_prefix="/mcp")

# ---------------------------------------------------------------------------
# Session management — each SSE connection is a session
# ---------------------------------------------------------------------------

_sessions: dict[str, queue.Queue] = {}
_sessions_lock = threading.Lock()

SERVER_INFO = {
    "name": "ped-tools",
    "version": "1.0.0",
}

CAPABILITIES = {
    "tools": {},
}

# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "list_proxies",
        "description": "List all registered proxy servers with their API domains and mock counts.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_proxy",
        "description": "Get a proxy's configuration and all its mocked endpoints.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "The proxy identifier (e.g. 'ajiocashwallet')"}
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "create_proxy",
        "description": "Register a new proxy server with an API domain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "api_domain": {"type": "string", "description": "Base URL of the real API (e.g. https://api.example.com)"},
                "identifier": {"type": "string", "description": "Optional custom identifier. Auto-generated if omitted."},
            },
            "required": ["api_domain"],
        },
    },
    {
        "name": "create_mock",
        "description": "Create or update a mock response for a proxy endpoint. Supports static JSON, conditional mocks, sequences, and dynamic resolvers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "proxy_identifier": {"type": "string", "description": "The proxy to add the mock to"},
                "endpoint": {"type": "string", "description": "The endpoint path (e.g. /users/123 or /orders/<id>)"},
                "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, PATCH, DELETE, or * for any", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "*"]},
                "mock": {"description": "The mock response body. Can be a JSON object, array (sequence), or conditional structure."},
            },
            "required": ["proxy_identifier", "endpoint", "method", "mock"],
        },
    },
    {
        "name": "delete_mock",
        "description": "Delete a specific mock from a proxy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "proxy_identifier": {"type": "string"},
                "endpoint": {"type": "string"},
                "method": {"type": "string"},
            },
            "required": ["proxy_identifier", "endpoint", "method"],
        },
    },
    {
        "name": "get_state",
        "description": "Get the current per-proxy state (used by dbget() resolver in mocks).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Proxy identifier"}
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "set_state",
        "description": "Replace the entire per-proxy state with new data.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "state": {"type": "object", "description": "The new state object"},
            },
            "required": ["identifier", "state"],
        },
    },
    {
        "name": "merge_state",
        "description": "Shallow-merge data into the existing per-proxy state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "patch": {"type": "object", "description": "Keys to merge into existing state"},
            },
            "required": ["identifier", "patch"],
        },
    },
    {
        "name": "get_history",
        "description": "Get recent request history for a proxy. Supports filtering by method, endpoint, status, and source.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"},
                "limit": {"type": "integer", "description": "Max entries (default 20)"},
                "method": {"type": "string", "description": "Filter by HTTP method"},
                "endpoint": {"type": "string", "description": "Filter by endpoint substring"},
                "source": {"type": "string", "description": "Filter by source: mock, forward, redirect, mock_miss"},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "validate_mock",
        "description": "Validate a mock payload without deploying it. Optionally dry-run against a test request.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mock": {"description": "The mock response to validate"},
                "proxy_identifier": {"type": "string", "description": "Proxy context for dbget/state resolvers"},
                "test_request": {
                    "type": "object",
                    "description": "Optional test request: {method, headers, body, params}",
                },
            },
            "required": ["mock"],
        },
    },
    {
        "name": "proxy_health",
        "description": "Check the upstream health of a proxy's API domain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string"}
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "list_templates",
        "description": "List all available mock templates.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "storage_info",
        "description": "Get database storage usage info (size, row counts).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_curl",
        "description": "Generate the correct curl command to call a proxy mock endpoint. Use this when the user asks for a curl command to test a mock.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "proxy_identifier": {"type": "string", "description": "The proxy identifier"},
                "endpoint": {"type": "string", "description": "The endpoint path (e.g. /ajiocash/v1/giftcard/getBalance)"},
                "method": {"type": "string", "description": "HTTP method", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "body": {"description": "Request body JSON (for POST/PUT/PATCH)"},
                "headers": {"type": "object", "description": "Custom headers to include"},
                "base_url": {"type": "string", "description": "Base URL of the PED Tools server (default: https://jsonkar.pythonanywhere.com)"},
            },
            "required": ["proxy_identifier", "endpoint", "method"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Execution — calls into the main app functions
# ---------------------------------------------------------------------------

def _execute_tool(name: str, arguments: dict) -> dict:
    """Execute an MCP tool and return the result."""
    # Import app functions lazily to avoid circular imports
    from app import (
        db_list_proxies, db_get_proxy, db_create_proxy, db_upsert_mock,
        db_delete_mock, db_get_state, db_set_state, db_merge_state,
        db_get_request_history, db_get_proxy_domain, db_list_templates,
        _get_db, DB_PATH,
    )
    import os

    logger.info("[MCP] Executing tool=%s", name)

    if name == "list_proxies":
        with app_context():
            return {"proxies": db_list_proxies()}

    if name == "get_proxy":
        with app_context():
            proxy = db_get_proxy(arguments["identifier"])
            if not proxy:
                return {"error": f"Proxy '{arguments['identifier']}' not found"}
            return proxy

    if name == "create_proxy":
        with app_context():
            identifier = arguments.get("identifier") or None
            from app import shortuuid
            if not identifier:
                identifier = shortuuid.uuid()
            old = db_create_proxy(identifier, arguments["api_domain"])
            return {"identifier": identifier, "api_domain": arguments["api_domain"], "replaced_existing": old is not None}

    if name == "create_mock":
        with app_context():
            old = db_upsert_mock(
                arguments["proxy_identifier"],
                arguments["endpoint"],
                arguments["method"],
                arguments["mock"],
            )
            return {
                "status": "created",
                "proxy_identifier": arguments["proxy_identifier"],
                "endpoint": arguments["endpoint"],
                "method": arguments["method"],
                "replaced_existing": old is not None,
            }

    if name == "delete_mock":
        with app_context():
            deleted = db_delete_mock(
                arguments["proxy_identifier"],
                arguments["endpoint"],
                arguments["method"],
            )
            if deleted is None:
                return {"error": "Mock not found"}
            return {"status": "deleted", "deleted_mock": deleted}

    if name == "get_state":
        with app_context():
            return {"identifier": arguments["identifier"], "state": db_get_state(arguments["identifier"])}

    if name == "set_state":
        with app_context():
            db_set_state(arguments["identifier"], arguments["state"])
            return {"status": "replaced", "identifier": arguments["identifier"]}

    if name == "merge_state":
        with app_context():
            merged = db_merge_state(arguments["identifier"], arguments["patch"])
            return {"status": "merged", "state": merged}

    if name == "get_history":
        with app_context():
            history = db_get_request_history(
                arguments["identifier"],
                limit=arguments.get("limit", 20),
                method=arguments.get("method"),
                endpoint=arguments.get("endpoint"),
                source=arguments.get("source"),
            )
            # Parse JSON strings
            for h in history:
                for field in ("request_headers", "request_body", "response_body"):
                    if h.get(field) and isinstance(h[field], str):
                        try:
                            h[field] = json.loads(h[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
            return {"identifier": arguments["identifier"], "count": len(history), "history": history}

    if name == "validate_mock":
        with app_context():
            from app import resolve_mock_data
            import copy as copy_mod
            mock = arguments["mock"]
            test_req = arguments.get("test_request", {})
            errors = []
            resolved = None
            try:
                mock_copy = copy_mod.deepcopy(mock)
                t_headers = test_req.get("headers", {})
                t_body = test_req.get("body", {})
                t_params = test_req.get("params", {})
                proxy_id = arguments.get("proxy_identifier", "__validate__")

                if isinstance(mock_copy, dict):
                    mock_copy.pop("_store", None)
                    mock_copy.pop("_delay_ms", None)
                    mock_copy.pop("_delay_profile", None)
                    mock_copy.pop("_callback", None)
                    mock_copy.pop("_cache_ttl", None)

                if isinstance(mock_copy, dict) and "status_code" in mock_copy and "body" in mock_copy:
                    resolved = resolve_mock_data(
                        copy_mod.deepcopy(mock_copy["body"]),
                        header=t_headers, json_body=t_body, params=t_params,
                        url="/test", proxy_id=proxy_id,
                    )
                else:
                    resolved = resolve_mock_data(
                        copy_mod.deepcopy(mock_copy),
                        header=t_headers, json_body=t_body, params=t_params,
                        url="/test", proxy_id=proxy_id,
                    )
            except Exception as exc:
                errors.append(str(exc))

            return {"valid": len(errors) == 0, "errors": errors, "resolved_output": resolved}

    if name == "proxy_health":
        with app_context():
            import requests as http_requests
            domain = db_get_proxy_domain(arguments["identifier"])
            if not domain:
                return {"error": "Proxy not found"}
            try:
                from urllib.parse import urlparse
                parsed = urlparse(domain if "://" in domain else f"https://{domain}")
                url = f"{parsed.scheme}://{parsed.netloc}/"
                start = time.time()
                resp = http_requests.head(url, timeout=5, allow_redirects=True)
                latency = int((time.time() - start) * 1000)
                status = "healthy" if resp.status_code < 400 else ("degraded" if resp.status_code < 500 else "unhealthy")
                return {"identifier": arguments["identifier"], "status": status, "latency_ms": latency, "domain": domain}
            except Exception as exc:
                return {"identifier": arguments["identifier"], "status": "unhealthy", "error": str(exc), "domain": domain}

    if name == "list_templates":
        with app_context():
            return {"templates": db_list_templates()}

    if name == "storage_info":
        with app_context():
            db_size = 0
            try:
                db_size = os.path.getsize(DB_PATH)
            except OSError:
                pass
            db = _get_db()
            return {
                "db_size_mb": round(db_size / (1024 * 1024), 2),
                "history_count": db.execute("SELECT COUNT(*) as c FROM request_history").fetchone()["c"],
                "mock_count": db.execute("SELECT COUNT(*) as c FROM mocks").fetchone()["c"],
                "proxy_count": db.execute("SELECT COUNT(*) as c FROM proxies").fetchone()["c"],
            }

    if name == "get_curl":
        base = arguments.get("base_url", "https://jsonkar.pythonanywhere.com").rstrip("/")
        ep = arguments["endpoint"]
        if ep.startswith("/"):
            ep = ep[1:]
        url = f"{base}/proxy/{arguments['proxy_identifier']}/{ep}"
        method = arguments["method"]
        parts = [f'curl -X {method} "{url}"']
        # Headers
        hdrs = arguments.get("headers", {})
        hdrs.setdefault("Content-Type", "application/json")
        for k, v in hdrs.items():
            parts.append(f'  -H "{k}: {v}"')
        # Body
        body = arguments.get("body")
        if body and method in ("POST", "PUT", "PATCH"):
            body_str = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
            parts.append(f"  -d '{body_str}'")
        curl_cmd = " \\\n".join(parts)
        return {"curl": curl_cmd, "note": "This calls the proxy endpoint, which returns the mock response."}

    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Flask app context helper
# ---------------------------------------------------------------------------

from contextlib import contextmanager

@contextmanager
def app_context():
    """Run code within the Flask app context (needed for _get_db etc.)."""
    from flask import has_app_context
    if has_app_context():
        yield
    else:
        from app import app
        with app.app_context():
            yield


# ---------------------------------------------------------------------------
# MCP Protocol Handlers
# ---------------------------------------------------------------------------

def _handle_jsonrpc(msg: dict, session_id: str) -> dict | None:
    """Handle a single JSON-RPC message and return a response (or None for notifications)."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    logger.debug("[MCP] method=%s id=%s session=%s", method, msg_id, session_id[:8])

    # --- Initialize ---
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": SERVER_INFO,
                "capabilities": CAPABILITIES,
            },
        }

    # --- Ping ---
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    # --- List Tools ---
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": MCP_TOOLS},
        }

    # --- Call Tool ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = _execute_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
                },
            }
        except Exception as exc:
            logger.exception("[MCP] Tool execution error: %s", exc)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }

    # --- Notifications (no response needed) ---
    if method == "notifications/initialized":
        logger.info("[MCP] Client initialized session=%s", session_id[:8])
        return None

    if method == "notifications/cancelled":
        logger.info("[MCP] Request cancelled session=%s", session_id[:8])
        return None

    # --- Unknown method ---
    if msg_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


# ---------------------------------------------------------------------------
# SSE Endpoint — /mcp/sse
# ---------------------------------------------------------------------------

@mcp_bp.route("/sse", methods=["GET"])
def mcp_sse():
    """SSE endpoint — client connects here to receive server messages."""
    session_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()

    with _sessions_lock:
        _sessions[session_id] = q

    logger.info("[MCP] SSE connection opened session=%s", session_id[:8])

    # Send the endpoint URL for the client to POST messages to
    messages_url = f"/mcp/messages?session_id={session_id}"

    def generate():
        # First event: tell client where to send messages
        yield f"event: endpoint\ndata: {messages_url}\n\n"

        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    if msg is None:
                        break  # Session closed
                    yield f"event: message\ndata: {json.dumps(msg)}\n\n"
                except queue.Empty:
                    # Send keepalive ping
                    yield ": keepalive\n\n"
        finally:
            with _sessions_lock:
                _sessions.pop(session_id, None)
            logger.info("[MCP] SSE connection closed session=%s", session_id[:8])

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# Messages Endpoint — /mcp/messages
# ---------------------------------------------------------------------------

@mcp_bp.route("/messages", methods=["POST"])
def mcp_messages():
    """Client sends JSON-RPC messages here. Responses are pushed to the SSE stream."""
    session_id = request.args.get("session_id", "")

    with _sessions_lock:
        q = _sessions.get(session_id)

    if not q:
        return jsonify({"error": "Invalid or expired session"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    logger.debug("[MCP] Received message session=%s: %s", session_id[:8], json.dumps(data)[:200])

    response = _handle_jsonrpc(data, session_id)
    if response:
        q.put(response)

    return "", 202


# ---------------------------------------------------------------------------
# Info Endpoint — /mcp/
# ---------------------------------------------------------------------------

@mcp_bp.route("/", methods=["GET"])
def mcp_info():
    """MCP server info and available tools list."""
    return jsonify({
        "name": SERVER_INFO["name"],
        "version": SERVER_INFO["version"],
        "protocol": "MCP over SSE",
        "note": "This is an MCP server for AI assistants. To create mocks via curl, use POST /proxy/mock/create/. To call mocks, use ANY /proxy/<identifier>/<endpoint>.",
        "endpoints": {
            "sse": "/mcp/sse",
            "messages": "/mcp/messages?session_id=<id>",
        },
        "rest_api": {
            "create_mock": "POST /proxy/mock/create/",
            "call_mock": "ANY /proxy/<identifier>/<endpoint>",
            "list_proxies": "GET /proxy/list/",
            "docs": "GET /proxy/helper",
        },
        "tools": [{"name": t["name"], "description": t["description"]} for t in MCP_TOOLS],
        "tool_count": len(MCP_TOOLS),
    })
