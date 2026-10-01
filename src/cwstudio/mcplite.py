"""A compact Model Context Protocol server (JSON-RPC 2.0) with stdio, streamable HTTP and legacy SSE transports.

It implements the parts of MCP that Studio uses: tools (input and output schemas generated from type hints, annotations, structured results), static resources and prompts, plus ping. Results, schemas and validation follow what the MCP Python SDK produced for the same functions, so clients see no difference; using it keeps Studio free of pydantic, jsonschema, httpx and friends. Every request runs on its own thread, so long tool calls (a capture with wait=true) never delay others such as capture_stop or ping.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import os
import queue
import sys
import threading
import types
import typing
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("cwstudio.mcp")

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class RPCError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _union_args(hint: Any) -> Optional[List[Any]]:
    """Members of Optional[X] / Union[...] / X | None, or None if the hint is not a union."""
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        return list(typing.get_args(hint))
    return None


def _schema(hint: Any) -> Dict[str, Any]:
    """JSON Schema for a type hint (the subset used by Studio's tools), shaped like pydantic's output."""
    if hint is Any or hint is inspect.Parameter.empty:
        return {}
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    members = _union_args(hint)
    if members is not None:
        rest = [a for a in members if a is not type(None)]
        inner = _schema(rest[0]) if len(rest) == 1 else {"anyOf": [_schema(a) for a in rest]}
        return {"anyOf": [inner, {"type": "null"}]} if len(rest) < len(members) else inner
    if origin is typing.Literal:
        out: Dict[str, Any] = {"enum": list(args)}
        if {type(a) for a in args} == {str}:
            out["type"] = "string"
        return out
    if origin in (list, tuple, set):
        return {"type": "array", "items": _schema(args[0]) if args else {}}
    if origin is dict:
        return {"type": "object", "additionalProperties": (_schema(args[1]) or True) if len(args) == 2 else True}
    return {str: {"type": "string"}, int: {"type": "integer"}, float: {"type": "number"}, bool: {"type": "boolean"}, dict: {"type": "object"}, list: {"type": "array"}}.get(hint, {})


def _coerce(value: Any, hint: Any) -> Any:
    """Validate and convert a JSON argument to the annotated type, as leniently as pydantic's default (lax) mode: "5" or 5.0 for an int, "true" for a bool, but never null for a required type."""
    if hint is Any or hint is inspect.Parameter.empty:
        return value
    members = _union_args(hint)
    if members is not None:
        if value is None and type(None) in members:
            return None
        errors = []
        for m in (a for a in members if a is not type(None)):
            try:
                return _coerce(value, m)
            except (TypeError, ValueError) as e:
                errors.append(str(e))
        raise ValueError(errors[0] if len(errors) == 1 else "; ".join(errors))
    if value is None:
        raise ValueError("must not be null")
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Literal:
        if value not in args or (isinstance(value, bool) and not any(isinstance(a, bool) for a in args)):
            raise ValueError(f"must be one of {', '.join(repr(a) for a in args)}")
        return value
    if hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on", "y", "t", "false", "0", "no", "off", "n", "f"):
            return value.strip().lower() in ("true", "1", "yes", "on", "y", "t")
        raise ValueError("must be a boolean")
    if hint is int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                try:
                    f = float(value)
                except ValueError:
                    f = math.nan
                if f.is_integer():
                    return int(f)
        raise ValueError("must be an integer")
    if hint is float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ValueError("must be a number")
    if hint is str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value
    if origin in (list, tuple, set) or hint is list:
        if not isinstance(value, list):
            raise ValueError("must be an array")
        if args:
            out = []
            for i, v in enumerate(value):
                try:
                    out.append(_coerce(v, args[0]))
                except ValueError as e:
                    raise ValueError(f"item {i} {e}") from None
            return out
        return value
    if origin is dict or hint is dict:
        if not isinstance(value, dict):
            raise ValueError("must be an object")
        return value
    return value


def _clean(o: Any) -> Any:
    """JSON-safe copy: inf / NaN floats become null (as the SDK sent them) and other values that are not JSON become strings."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k) if not isinstance(k, str) else k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if o is None or isinstance(o, (str, int, bool)):
        return o
    if hasattr(o, "item") and getattr(o, "ndim", 1) == 0:
        return _clean(o.item())
    if hasattr(o, "tolist"):
        return _clean(o.tolist())
    return str(o)


def _wire(obj: Any) -> str:
    """Serialise a protocol message: ASCII only, so a stray lone surrogate can never break the output stream."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)


class _Callable:
    def __init__(self, fn: Callable):
        self.fn = fn
        self.name = fn.__name__
        self.doc = inspect.getdoc(fn) or ""
        self.hints = typing.get_type_hints(fn)
        self.params = inspect.signature(fn).parameters
        self.returns = self.hints.get("return", Any)

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

    def output_schema(self) -> Optional[Dict[str, Any]]:
        """Like the SDK: typed results are wrapped as {"result": ...}; functions returning Any have no output schema."""
        if self.returns is Any or self.returns is inspect.Parameter.empty:
            return None
        return {"type": "object", "properties": {"result": dict(_schema(self.returns), title="Result")}, "required": ["result"], "title": self.name + "Output"}

    def call(self, arguments: Any) -> Any:
        if not isinstance(arguments, dict):
            raise RPCError(INVALID_PARAMS, "arguments must be an object")
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


class Server:
    """Register tools, resources and prompts with decorators, then ``run("stdio" | "streamable-http" | "sse")``."""

    def __init__(self, name: str, version: str, title: Optional[str] = None, instructions: Optional[str] = None):
        self.info = {"name": name, "version": version, **({"title": title} if title else {})}
        self.instructions = instructions
        self.tools: Dict[str, Any] = {}
        self.resources: Dict[str, Any] = {}
        self.prompts: Dict[str, _Callable] = {}

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
               "capabilities": {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False}, "prompts": {"listChanged": False}}}
        if self.instructions:
            res["instructions"] = self.instructions
        return res

    def _tools_list(self, _params) -> Dict[str, Any]:
        tools = []
        for name, (c, ann) in self.tools.items():
            t = {"name": name, "title": name.replace("_", " ").capitalize(), "description": c.doc, "inputSchema": c.input_schema()}
            out = c.output_schema()
            if out:
                t["outputSchema"] = out
            if ann:
                t["annotations"] = ann
            tools.append(t)
        return {"tools": tools}

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.tools:
            raise RPCError(INVALID_PARAMS, f"unknown tool: {name}")
        c = self.tools[name][0]
        try:
            value = _clean(c.call(params.get("arguments") if params.get("arguments") is not None else {}))
        except RPCError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # noqa: BLE001 (tool failures are reported to the agent, not as protocol errors)
            log.debug("tool %s failed", name, exc_info=True)
            return {"content": [{"type": "text", "text": f"Error executing tool {name}: {e}"}], "isError": True}
        # Content blocks as the SDK made them: a string as is, a list as one block per item, anything else as indented JSON.
        if isinstance(value, str):
            content = [value]
        elif isinstance(value, list):
            content = [v if isinstance(v, str) else _pretty(v) for v in value]
        else:
            content = [_pretty(value)]
        res: Dict[str, Any] = {"content": [{"type": "text", "text": t} for t in content], "isError": False}
        if c.output_schema():
            res["structuredContent"] = {"result": value}
        return res

    def _resources_list(self, _params) -> Dict[str, Any]:
        return {"resources": [{"uri": uri, "name": fn.__name__, "description": inspect.getdoc(fn) or "", "mimeType": mime} for uri, (fn, mime) in self.resources.items()]}

    def _resources_read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        uri = params.get("uri")
        if uri not in self.resources:
            raise RPCError(-32002, f"resource not found: {uri}")
        fn, mime = self.resources[uri]
        data = fn()
        return {"contents": [{"uri": uri, "mimeType": mime, "text": data if isinstance(data, str) else _pretty(_clean(data))}]}

    def _prompts_list(self, _params) -> Dict[str, Any]:
        out = []
        for name, c in self.prompts.items():
            args = [{"name": n, "required": p.default is inspect.Parameter.empty} for n, p in c.params.items()]
            out.append({"name": name, "description": c.doc, "arguments": args})
        return {"prompts": out}

    def _prompts_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in self.prompts:
            raise RPCError(INVALID_PARAMS, f"unknown prompt: {name}")
        c = self.prompts[name]
        args = params.get("arguments") or {}
        if isinstance(args, dict):
            unknown = set(args) - set(c.params)
            if unknown:
                raise RPCError(INVALID_PARAMS, f"unknown argument(s) for prompt {name}: {', '.join(sorted(unknown))}")
        text = c.call(args)
        return {"description": c.doc, "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC message and return the response (None for notifications and responses)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "invalid JSON-RPC message"}}
        if "method" not in msg:
            return None  # a response to a server request; Studio sends none
        if "id" not in msg:
            return None  # notifications: initialized, cancelled, progress
        mid, method, params = msg.get("id"), msg["method"], msg.get("params")
        if mid is None or isinstance(mid, bool) or not isinstance(mid, (str, int)):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "request id must be a string or an integer"}}
        handlers = {"initialize": self._initialize, "ping": lambda _p: {},
                    "tools/list": self._tools_list, "tools/call": self._tools_call,
                    "resources/list": self._resources_list, "resources/read": self._resources_read, "resources/templates/list": lambda _p: {"resourceTemplates": []},
                    "prompts/list": self._prompts_list, "prompts/get": self._prompts_get}
        try:
            if method not in handlers:
                raise RPCError(METHOD_NOT_FOUND, f"method not found: {method}")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise RPCError(INVALID_PARAMS, "params must be an object")
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
            if not msg:
                return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "empty batch"}}
            out = [r for r in (self.handle(m) for m in msg) if r is not None]
            return out or None
        return self.handle(msg)

    @staticmethod
    def _spawn(fn: Callable, *args) -> None:
        threading.Thread(target=fn, args=args, name="mcp-request", daemon=True).start()

    # ----- transports -------------------------------------------------------------
    def run(self, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8766) -> None:
        if transport == "stdio":
            self._run_stdio()
        else:
            self._run_http(host, port, sse=transport == "sse")

    @staticmethod
    def _private_stdout():
        """Return a binary stream on the real stdout and point fd 1, sys.stdout and (on Windows) the process's standard output handle at stderr, so nothing else can write into the protocol stream."""
        sys.stdout.flush()
        out = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
        if sys.stderr is None:  # pythonw has no console streams
            sys.stderr = open(os.devnull, "w")
        try:
            err_fd = sys.stderr.fileno()
        except (OSError, ValueError, AttributeError):
            err_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(err_fd, sys.stdout.fileno())
        if os.name == "nt":  # child processes inherit the Win32 standard handles, which dup2 does not change
            try:
                import ctypes
                import msvcrt
                ctypes.windll.kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(err_fd))  # STD_OUTPUT_HANDLE
            except Exception:  # noqa: BLE001
                log.debug("could not redirect the Windows stdout handle", exc_info=True)
        sys.stdout = sys.stderr
        return out

    def _run_stdio(self) -> None:
        out = self._private_stdout()
        lock = threading.Lock()

        def send(resp):
            if resp is None:
                return
            try:
                data = (_wire(resp) + "\n").encode("utf-8")
            except (TypeError, ValueError) as e:  # never let one bad result kill the session
                mid = resp.get("id") if isinstance(resp, dict) else None
                data = (_wire({"jsonrpc": "2.0", "id": mid, "error": {"code": INTERNAL_ERROR, "message": f"result could not be encoded: {e}"}}) + "\n").encode()
            with lock:
                try:
                    out.write(data)
                except OSError:
                    pass

        def work(raw):
            try:
                send(self.handle_raw(raw))
            except Exception:  # noqa: BLE001
                log.exception("MCP request failed")

        for line in sys.stdin.buffer:
            line = line.strip()
            if not line:
                continue
            if b'"initialize"' in line or b'"ping"' in line:
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and msg.get("method") in ("initialize", "ping"):
                    work(line)  # answered in order, immediately
                    continue
            self._spawn(work, line)
        # stdin closed: let running requests finish (they all have timeouts), then exit.
        for t in [t for t in threading.enumerate() if t.name == "mcp-request"]:
            t.join()

    def _run_http(self, host: str, port: int, sse: bool) -> None:
        server = self
        sse_queues: Dict[str, "queue.Queue[Optional[str]]"] = {}
        http_sessions: set = set()
        local_bind = host in _LOCAL_HOSTS

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                log.debug("mcp http: " + fmt, *args)

            def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", headers: Optional[Dict[str, str]] = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _error(self, code: int, message: str, rpc_code: int = INVALID_REQUEST):
                self._send(code, _wire({"jsonrpc": "2.0", "id": None, "error": {"code": rpc_code, "message": message}}).encode())

            def _guard(self) -> bool:
                """DNS rebinding protection: on a local bind, only local Host and Origin headers are accepted."""
                origin = self.headers.get("Origin")
                if origin and (urlparse(origin).hostname or "") not in _LOCAL_HOSTS + (host,):
                    self._error(403, "origin not allowed")
                    return False
                if local_bind:
                    hostname = urlparse("//" + (self.headers.get("Host") or "")).hostname or ""
                    if hostname not in _LOCAL_HOSTS:
                        self._error(421, "invalid Host header")
                        return False
                return True

            def _body(self) -> Optional[bytes]:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    self._error(411, "chunked request bodies are not supported; send Content-Length")
                    self.close_connection = True
                    return None
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > 64 * 1024 * 1024:
                    self._error(400, "invalid Content-Length")
                    self.close_connection = True
                    return None
                return self.rfile.read(n)

            def do_GET(self):
                url = urlparse(self.path)
                if not self._guard():
                    return
                if sse and url.path == "/sse":
                    return self._sse_stream()
                if not sse and url.path.rstrip("/") == "/mcp":
                    return self._send(405, b'{"error":"server-initiated streams are not offered; use POST"}', headers={"Allow": "POST, DELETE"})
                self._send(404, b'{"error":"not found"}')

            def _sse_stream(self):
                sid = uuid.uuid4().hex
                q: "queue.Queue[Optional[str]]" = queue.Queue()
                sse_queues[sid] = q
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
                    sse_queues.pop(sid, None)
                    self.close_connection = True

            def do_DELETE(self):
                if not self._guard():
                    return
                if sse or urlparse(self.path).path.rstrip("/") != "/mcp":
                    return self._send(404, b'{"error":"not found"}')
                sid = self.headers.get("Mcp-Session-Id")
                if not sid or sid not in http_sessions:
                    return self._error(404, "unknown session")
                http_sessions.discard(sid)
                self._send(200)

            def do_POST(self):
                url = urlparse(self.path)
                if not self._guard():
                    return
                if sse:
                    if url.path.rstrip("/") != "/messages":
                        return self._send(404, b'{"error":"not found"}')
                    q = sse_queues.get((parse_qs(url.query).get("session_id") or [""])[0])
                    if q is None:
                        return self._error(404, "unknown session")
                    raw = self._body()
                    if raw is None:
                        return
                    self._send(202, b"")
                    server._spawn(lambda: (lambda r: r is not None and q.put(_wire(r)))(server.handle_raw(raw)))
                    return
                if url.path.rstrip("/") != "/mcp":
                    return self._send(404, b'{"error":"not found"}')
                accept = (self.headers.get("Accept") or "").lower()
                if "application/json" not in accept and "*/*" not in accept:
                    return self._error(406, "Accept must include application/json")
                if "application/json" not in (self.headers.get("Content-Type") or "").lower():
                    return self._error(415, "Content-Type must be application/json")
                pv = self.headers.get("Mcp-Protocol-Version")
                if pv and pv not in PROTOCOL_VERSIONS:
                    return self._error(400, f"unsupported MCP-Protocol-Version: {pv}")
                raw = self._body()
                if raw is None:
                    return
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
                is_init = isinstance(msg, dict) and msg.get("method") == "initialize"
                headers = {}
                if is_init:
                    sid = uuid.uuid4().hex
                    http_sessions.add(sid)
                    headers["Mcp-Session-Id"] = sid
                elif msg is not None:
                    sid = self.headers.get("Mcp-Session-Id")
                    if not sid:
                        return self._error(400, "missing Mcp-Session-Id header")
                    if sid not in http_sessions:
                        return self._error(404, "unknown or ended session")
                resp = server.handle_raw(raw)
                if resp is None:
                    return self._send(202, b"", headers=headers)
                self._send(200, _wire(resp).encode("utf-8"), headers=headers)

        httpd = ThreadingHTTPServer((host, port), Handler)
        httpd.daemon_threads = True
        print(f"MCP server listening on http://{host}:{port}{'/sse' if sse else '/mcp'}", file=sys.stderr, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
