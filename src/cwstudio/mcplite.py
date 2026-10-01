"""A compact Model Context Protocol server (JSON-RPC 2.0) with stdio, streamable HTTP and legacy SSE transports.

It implements the parts of MCP that Studio uses: tools (input schemas generated from type hints, annotations, structured results), static resources and prompts, plus ping and logging/setLevel. Using it instead of the MCP SDK keeps Studio free of pydantic, jsonschema, httpx and friends. Requests run on a thread pool so a long tool call (a capture with wait=true) does not block others such as capture_stop.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import queue
import sys
import threading
import typing
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("cwstudio.mcp")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _schema(hint: Any) -> Dict[str, Any]:
    """JSON Schema for a type hint (the subset used by Studio's tools)."""
    if hint is Any or hint is inspect.Parameter.empty:
        return {}
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Union:
        rest = [a for a in args if a is not type(None)]
        inner = _schema(rest[0]) if len(rest) == 1 else {"anyOf": [_schema(a) for a in rest]}
        return {"anyOf": [inner, {"type": "null"}]} if len(rest) < len(args) else inner
    if origin is typing.Literal:
        types = {type(a) for a in args}
        out: Dict[str, Any] = {"enum": list(args)}
        if types == {str}:
            out["type"] = "string"
        return out
    if origin in (list, List, tuple, set):
        return {"type": "array", "items": _schema(args[0]) if args else {}}
    if origin in (dict, Dict):
        return {"type": "object", "additionalProperties": (_schema(args[1]) or True) if len(args) == 2 else True}
    return {str: {"type": "string"}, int: {"type": "integer"}, float: {"type": "number"}, bool: {"type": "boolean"}, dict: {"type": "object"}, list: {"type": "array"}}.get(hint, {})


def _coerce(value: Any, hint: Any) -> Any:
    """Lenient conversion of JSON arguments to the annotated type (agents sometimes send "2000" for 2000)."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if value is None:
        return None
    if origin is typing.Union:
        rest = [a for a in args if a is not type(None)]
        return _coerce(value, rest[0]) if len(rest) == 1 else value
    if origin is typing.Literal:
        if value not in args:
            raise ValueError(f"must be one of {', '.join(repr(a) for a in args)}")
        return value
    if hint is bool:
        if isinstance(value, str):
            if value.strip().lower() in ("true", "1", "yes", "on"):
                return True
            if value.strip().lower() in ("false", "0", "no", "off"):
                return False
            raise ValueError("must be a boolean")
        return bool(value)
    if hint is int:
        if isinstance(value, bool):
            raise ValueError("must be an integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("must be an integer")
        return int(value)
    if hint is float:
        if isinstance(value, bool):
            raise ValueError("must be a number")
        return float(value)
    if hint is str and not isinstance(value, str):
        raise ValueError("must be a string")
    if origin in (list, List) and not isinstance(value, list):
        raise ValueError("must be an array")
    if origin in (dict, Dict) and not isinstance(value, dict):
        raise ValueError("must be an object")
    return value


class _Callable:
    def __init__(self, fn: Callable):
        self.fn = fn
        self.name = fn.__name__
        self.doc = inspect.getdoc(fn) or ""
        self.hints = typing.get_type_hints(fn)
        self.params = inspect.signature(fn).parameters

    def input_schema(self) -> Dict[str, Any]:
        props, required = {}, []
        for name, p in self.params.items():
            s = _schema(self.hints.get(name, Any))
            if p.default is inspect.Parameter.empty:
                required.append(name)
            else:
                s = dict(s, default=p.default)
            props[name] = dict(s, title=name.replace("_", " ").title())
        out: Dict[str, Any] = {"type": "object", "properties": props, "title": self.name + "Arguments"}
        if required:
            out["required"] = required
        return out

    def call(self, arguments: Dict[str, Any]) -> Any:
        kwargs = {}
        for name, p in self.params.items():
            if name in arguments:
                try:
                    kwargs[name] = _coerce(arguments[name], self.hints.get(name, Any))
                except (TypeError, ValueError) as e:
                    raise RPCError(INVALID_PARAMS, f"invalid argument '{name}': {e}") from None
            elif p.default is inspect.Parameter.empty:
                raise RPCError(INVALID_PARAMS, f"missing required argument '{name}'")
        return self.fn(**kwargs)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class Server:
    """Register tools, resources and prompts with decorators, then ``run("stdio" | "streamable-http" | "sse")``."""

    def __init__(self, name: str, version: str, title: Optional[str] = None, instructions: Optional[str] = None):
        self.info = {"name": name, "version": version, **({"title": title} if title else {})}
        self.instructions = instructions
        self.tools: Dict[str, Any] = {}
        self.resources: Dict[str, Any] = {}
        self.prompts: Dict[str, _Callable] = {}
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mcp")

    # ----- registration -------------------------------------------------------
    def tool(self, annotations: Optional[Dict[str, Any]] = None) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.tools[fn.__name__] = (_Callable(fn), annotations or {})
            return fn
        return deco

    def resource(self, uri: str, mime_type: str = "text/plain") -> Callable:
        def deco(fn: Callable) -> Callable:
            self.resources[uri] = (fn, mime_type)
            return fn
        return deco

    def prompt(self) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.prompts[fn.__name__] = _Callable(fn)
            return fn
        return deco

    # ----- protocol -------------------------------------------------------------
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        asked = params.get("protocolVersion")
        res = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0], "serverInfo": self.info,
               "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}, "prompts": {"listChanged": False}, "logging": {}}}
        if self.instructions:
            res["instructions"] = self.instructions
        return res

    def _tools_list(self, _params) -> Dict[str, Any]:
        tools = []
        for name, (c, ann) in self.tools.items():
            tools.append({"name": name, "title": name.replace("_", " ").capitalize(), "description": c.doc, "inputSchema": c.input_schema(), **({"annotations": ann} if ann else {})})
        return {"tools": tools}

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if name not in self.tools:
            raise RPCError(INVALID_PARAMS, f"unknown tool: {name}")
        c = self.tools[name][0]
        try:
            value = c.call(params.get("arguments") or {})
        except RPCError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # noqa: BLE001 (tool failures are reported to the agent, not as protocol errors)
            log.debug("tool %s failed", name, exc_info=True)
            return {"content": [{"type": "text", "text": f"Error executing tool {name}: {e}"}], "isError": True}
        text = value if isinstance(value, str) else _dumps(value)
        structured = value if isinstance(value, dict) else {"result": value}
        return {"content": [{"type": "text", "text": text}], "structuredContent": json.loads(_dumps(structured)), "isError": False}

    def _resources_list(self, _params) -> Dict[str, Any]:
        return {"resources": [{"uri": uri, "name": fn.__name__, "description": inspect.getdoc(fn) or "", "mimeType": mime} for uri, (fn, mime) in self.resources.items()]}

    def _resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if uri not in self.resources:
            raise RPCError(-32002, f"resource not found: {uri}")
        fn, mime = self.resources[uri]
        data = fn()
        return {"contents": [{"uri": uri, "mimeType": mime, "text": data if isinstance(data, str) else _dumps(data)}]}

    def _prompts_list(self, _params) -> Dict[str, Any]:
        out = []
        for name, c in self.prompts.items():
            args = [{"name": n, "required": p.default is inspect.Parameter.empty} for n, p in c.params.items()]
            out.append({"name": name, "description": c.doc, "arguments": args})
        return {"prompts": out}

    def _prompts_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if name not in self.prompts:
            raise RPCError(INVALID_PARAMS, f"unknown prompt: {name}")
        c = self.prompts[name]
        text = c.call(params.get("arguments") or {})
        return {"description": c.doc, "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC message and return the response (None for notifications and responses)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "invalid JSON-RPC message"}}
        if "method" not in msg:
            return None  # a response to a server request; Studio sends none
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        handlers = {"initialize": self._initialize, "ping": lambda _p: {}, "logging/setLevel": lambda _p: {},
                    "tools/list": self._tools_list, "tools/call": self._tools_call,
                    "resources/list": self._resources_list, "resources/read": self._resources_read, "resources/templates/list": lambda _p: {"resourceTemplates": []},
                    "prompts/list": self._prompts_list, "prompts/get": self._prompts_get}
        if "id" not in msg:
            return None  # notifications: initialized, cancelled, progress
        try:
            if method not in handlers:
                raise RPCError(METHOD_NOT_FOUND, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": mid, "result": handlers[method](params)}
        except RPCError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:  # noqa: BLE001
            log.exception("MCP request %s failed", method)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": INTERNAL_ERROR, "message": str(e)}}

    def handle_raw(self, raw: bytes) -> Optional[Any]:
        """Handle a message or batch given as JSON text."""
        try:
            msg = json.loads(raw)
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": f"parse error: {e}"}}
        if isinstance(msg, list):
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        return self.handle(msg)

    # ----- transports -------------------------------------------------------------
    def run(self, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8766) -> None:
        if transport == "stdio":
            self._run_stdio()
        else:
            self._run_http(host, port, sse=transport == "sse")

    def _run_stdio(self) -> None:
        # Keep the protocol stream private: anything else printed to stdout (by Python or C code) goes to stderr instead.
        out = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        sys.stdout.flush()
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        lock = threading.Lock()

        def send(resp):
            if resp is not None:
                data = (_dumps(resp) + "\n").encode("utf-8")
                with lock:
                    out.write(data)

        for line in sys.stdin.buffer:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                send(self.handle_raw(line))
                continue
            if isinstance(msg, dict) and msg.get("method") == "initialize":
                send(self.handle(msg))  # answer initialize before anything that follows it
            else:
                self.pool.submit(lambda m=msg: send(self.handle(m) if isinstance(m, dict) else self.handle_raw(json.dumps(m).encode())))
        self.pool.shutdown(wait=True)

    def _run_http(self, host: str, port: int, sse: bool) -> None:
        server = self
        sessions: Dict[str, "queue.Queue[Optional[str]]"] = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                log.debug("mcp http: " + fmt, *args)

            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                return (urlparse(origin).hostname or "") in ("127.0.0.1", "localhost", "::1", host)

            def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers: Optional[Dict[str, str]] = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                if not self._origin_ok():
                    return self._send(403, b'{"error":"origin not allowed"}')
                if sse and url.path == "/sse":
                    sid = uuid.uuid4().hex
                    q: "queue.Queue[Optional[str]]" = queue.Queue()
                    sessions[sid] = q
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        self.wfile.write(f"event: endpoint\ndata: /messages/?session_id={sid}\n\n".encode())
                        self.wfile.flush()
                        while True:
                            try:
                                item = q.get(timeout=15)
                            except queue.Empty:
                                self.wfile.write(b": ping\n\n")
                                self.wfile.flush()
                                continue
                            if item is None:
                                break
                            self.wfile.write(f"event: message\ndata: {item}\n\n".encode())
                            self.wfile.flush()
                    except OSError:
                        pass
                    finally:
                        sessions.pop(sid, None)
                        self.close_connection = True
                    return
                self._send(405, b'{"error":"use POST"}', headers={"Allow": "POST"})

            def do_DELETE(self):
                self._send(200 if urlparse(self.path).path == "/mcp" else 404)

            def do_POST(self):
                url = urlparse(self.path)
                if not self._origin_ok():
                    return self._send(403, b'{"error":"origin not allowed"}')
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if sse and url.path.rstrip("/") == "/messages":
                    q = sessions.get((parse_qs(url.query).get("session_id") or [""])[0])
                    if q is None:
                        return self._send(404, b'{"error":"unknown session"}')
                    self._send(202, b"")
                    server.pool.submit(lambda: (lambda r: r is not None and q.put(_dumps(r)))(server.handle_raw(raw)))
                    return
                if url.path.rstrip("/") != "/mcp":
                    return self._send(404, b'{"error":"not found"}')
                resp = server.handle_raw(raw)
                headers = {}
                if b'"initialize"' in raw:
                    headers["Mcp-Session-Id"] = uuid.uuid4().hex
                if resp is None:
                    return self._send(202, b"", headers=headers)
                self._send(200, _dumps(resp).encode("utf-8"), headers=headers)

        httpd = ThreadingHTTPServer((host, port), Handler)
        httpd.daemon_threads = True
        print(f"MCP server listening on http://{host}:{port}{'/sse' if sse else '/mcp'}", file=sys.stderr, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
