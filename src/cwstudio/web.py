"""A small routing layer on Starlette: FastAPI-style decorators without pydantic.

Endpoints are plain ``async def`` functions. Parameters are filled by name and type hint: ``Request`` gets the request, ``UploadFile`` gets an uploaded file (multipart form field of that name, or the raw request body with ``?filename=``), names that appear in the path come from the path and everything else from the query string, converted to the annotated type (int, float, bool, str, Optional[...]). A returned ``Response`` is sent as is; anything else is sent as JSON. Errors keep FastAPI's shapes: ``HTTPException`` becomes ``{"detail": "..."}`` and invalid or missing parameters give 422 with a list of ``{"type", "loc", "msg", "input"}`` items, so existing clients see the same responses.
"""
from __future__ import annotations

import datetime
import enum
import html
import inspect
import json
import math
import re
import types
import typing
from typing import Any, Callable, Dict, List, Optional, Tuple

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response  # noqa: F401 (re-exported for app.py)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect  # noqa: F401 (re-exported for app.py)

__all__ = ["App", "HTTPException", "Request", "Response", "FileResponse", "UploadFile", "WebSocket", "WebSocketDisconnect"]

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}
_PATH_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")
_HEADER_PARAM = re.compile(r';\s*([\w*-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^;]*)')


class UploadFile:
    """An uploaded file held in memory (firmware images, trace sets and notebooks are small)."""

    def __init__(self, filename: Optional[str], content: bytes, content_type: str = "application/octet-stream"):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self) -> bytes:
        return self._content


class ValidationError(Exception):
    """A missing or invalid request parameter, reported as FastAPI's 422 response."""

    def __init__(self, kind: str, loc: List[str], msg: str, value: Any = None):
        super().__init__(msg)
        self.item = {"type": kind, "loc": loc, "msg": msg, "input": value}


def _default(o: Any) -> Any:
    """JSON fallback for values the session returns: numpy scalars and arrays, sets, tuples, bytes, dates, enums, paths."""
    if hasattr(o, "item") and getattr(o, "ndim", 1) == 0:
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
        return o.isoformat()
    if isinstance(o, enum.Enum):
        return o.value
    return str(o)


def _finite(o: Any) -> Any:
    """Copy of a JSON value with numpy keys made plain and inf / NaN floats replaced by null (JSON has no such numbers)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {(_default(k) if not isinstance(k, (str, int, float, bool)) and k is not None else k): _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    if hasattr(o, "tolist") or hasattr(o, "item"):
        return _finite(_default(o))
    return o


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        try:
            text = json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=_default)
        except (ValueError, TypeError):  # inf / NaN or numpy dict keys somewhere: sanitise and retry (rare, so the fast path stays fast)
            text = json.dumps(_finite(content), ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=_default)
        return text.encode("utf-8")


def _header_params(value: str) -> Dict[str, str]:
    """Parameters of a header such as Content-Disposition, honouring quoted strings (which may contain ';')."""
    out = {}
    for k, v in _HEADER_PARAM.findall(value):
        if v.startswith('"'):
            v = re.sub(r"\\(.)", r"\1", v[1:-1])
        out[k.lower()] = v.strip()
    return out


def _multipart(body: bytes, content_type: str) -> Dict[str, Tuple[Optional[str], bytes, str]]:
    """Split a multipart/form-data body (RFC 7578) into {field: (filename, content, content type)}."""
    boundary = _header_params(content_type).get("boundary")
    if not boundary:
        raise HTTPException(400, "multipart body without boundary")
    delim = b"--" + boundary.encode("latin-1")
    view = memoryview(body)
    out: Dict[str, Tuple[Optional[str], bytes, str]] = {}
    # The first delimiter may start the body; every later one must start a line.
    pos = 0 if body.startswith(delim) else body.find(b"\r\n" + delim)
    if pos < 0:
        raise HTTPException(400, "invalid multipart body: no boundary found")
    pos = pos + len(delim) if pos == 0 else pos + 2 + len(delim)
    while True:
        if body.startswith(b"--", pos):
            break  # closing delimiter
        while body[pos:pos + 1] in (b" ", b"\t"):
            pos += 1  # transport padding after a delimiter
        if not body.startswith(b"\r\n", pos):
            raise HTTPException(400, "invalid multipart body: bad delimiter")
        head_end = body.find(b"\r\n\r\n", pos + 2)
        if head_end < 0:
            raise HTTPException(400, "invalid multipart body: part without headers")
        nxt = body.find(b"\r\n" + delim, head_end + 4)
        while nxt >= 0 and body[nxt + 2 + len(delim):nxt + 4 + len(delim)].rstrip(b" \t") not in (b"\r\n", b"--", b"\r", b""):
            nxt = body.find(b"\r\n" + delim, nxt + 1)  # boundary text inside the content, not a real delimiter
        if nxt < 0:
            raise HTTPException(400, "invalid multipart body: missing closing boundary")
        name, filename, ctype = None, None, "application/octet-stream"
        for line in body[pos + 2:head_end].decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key == "content-disposition":
                params = _header_params(value)
                name, filename = params.get("name"), params.get("filename")
                if filename:
                    filename = re.split(r"[\\/]", filename)[-1]  # browsers send a bare name, some clients a full path
            elif key == "content-type":
                ctype = value.strip()
        if name is not None:
            out[name] = (filename, bytes(view[head_end + 4:nxt]), ctype)
        pos = nxt + 2 + len(delim)
    return out


def _hint_args(hint: Any) -> Tuple[Any, bool]:
    """(inner type, optional) for X, Optional[X], Union[X, None] and X | None."""
    if typing.get_origin(hint) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        return (args[0] if len(args) == 1 else str), True
    return hint, False


def _convert(raw: str, hint: Any) -> Any:
    hint, _ = _hint_args(hint)
    if hint is bool:
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValidationError("bool_parsing", [], "Input should be a valid boolean, unable to interpret input", raw)
    if hint is int:
        try:
            return int(raw)
        except ValueError:
            try:
                f = float(raw)
            except ValueError:
                f = math.nan
            if f.is_integer():
                return int(f)  # "3.0" is a whole number, as pydantic accepts
            raise ValidationError("int_parsing", [], "Input should be a valid integer, unable to parse string as an integer", raw) from None
    if hint is float:
        try:
            return float(raw)
        except ValueError:
            raise ValidationError("float_parsing", [], "Input should be a valid number, unable to parse string as a number", raw) from None
    return raw


class App(Starlette):
    """Starlette app with ``@app.get/post/put/delete/websocket`` decorators, a generated ``/api/docs`` page and ``/openapi.json``."""

    def __init__(self, title: str = "", version: str = "", docs_url: Optional[str] = "/api/docs", lifespan=None):
        super().__init__(lifespan=lifespan, exception_handlers={HTTPException: self._http_error})
        self.title, self.version = title, version
        self._docs: List[Tuple[str, str, List[Tuple[str, Any, bool, str]], str]] = []
        if docs_url:
            self.router.routes.append(Route(docs_url, self._docs_page, methods=["GET"]))
            self.router.routes.append(Route("/openapi.json", self._openapi, methods=["GET"]))

    @staticmethod
    async def _http_error(_request: Request, exc: HTTPException) -> Response:
        return JSON({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    def _endpoint(self, fn: Callable, path: str) -> Tuple[Callable, List[Tuple[str, Any, bool, str]]]:
        hints = typing.get_type_hints(fn)
        path_names = set(_PATH_PARAM.findall(path))
        params = []
        for name, p in inspect.signature(fn).parameters.items():
            hint = hints.get(name, str)
            where = "request" if hint is Request else "file" if hint is UploadFile else "path" if name in path_names else "query"
            params.append((name, hint, p.default, where))

        async def endpoint(request: Request) -> Response:
            kwargs: Dict[str, Any] = {}
            errors: List[Dict[str, Any]] = []
            form = None
            for name, hint, default, where in params:
                if where == "request":
                    kwargs[name] = request
                    continue
                if where == "file":
                    ctype = request.headers.get("content-type", "")
                    body = await request.body()
                    if ctype.lower().startswith("multipart/form-data"):
                        form = form if form is not None else _multipart(body, ctype)
                        part = form.get(name)
                        if part is not None and part[0] is not None:
                            kwargs[name] = UploadFile(*part)
                            continue
                    elif body:
                        kwargs[name] = UploadFile(request.query_params.get("filename"), body, ctype or "application/octet-stream")
                        continue
                    errors.append({"type": "missing", "loc": ["body", name], "msg": "Field required", "input": None})
                    continue
                raw = request.path_params.get(name) if where == "path" else request.query_params.get(name)
                if raw is None:
                    if default is inspect.Parameter.empty:
                        errors.append({"type": "missing", "loc": [where, name], "msg": "Field required", "input": None})
                    else:
                        kwargs[name] = default
                    continue
                try:
                    kwargs[name] = _convert(raw, hint)
                except ValidationError as e:
                    errors.append(dict(e.item, loc=[where, name]))
            if errors:
                return JSON({"detail": errors}, status_code=422)
            result = await fn(**kwargs)
            return result if isinstance(result, Response) else JSON(result)

        return endpoint, params

    def _add(self, methods: List[str], path: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            endpoint, params = self._endpoint(fn, path)
            route = Route(path, endpoint, methods=methods, name=fn.__name__)
            route.methods.discard("HEAD")  # Starlette adds HEAD to GET routes; HEAD would run the whole endpoint (an export, a USB scan)
            self.router.routes.append(route)
            doc = [(n, h, d is inspect.Parameter.empty, w) for n, h, d, w in params if w in ("query", "file", "path")]
            self._docs.append((methods[0], path, doc, inspect.getdoc(fn) or ""))
            return fn
        return deco

    def get(self, path: str) -> Callable:
        return self._add(["GET"], path)

    def post(self, path: str) -> Callable:
        return self._add(["POST"], path)

    def put(self, path: str) -> Callable:
        return self._add(["PUT"], path)

    def delete(self, path: str) -> Callable:
        return self._add(["DELETE"], path)

    def websocket(self, path: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.router.routes.append(WebSocketRoute(path, fn))
            self._docs.append(("WS", path, [], inspect.getdoc(fn) or ""))
            return fn
        return deco

    async def _docs_page(self, _request: Request) -> Response:
        def args(ps):
            return ", ".join(n + ("" if req else "?") for n, _h, req, w in ps if w != "path")
        rows = "".join(f"<tr><td class=m>{m}</td><td><code>{html.escape(p)}</code></td><td>{html.escape(args(a))}</td><td>{html.escape(d)}</td></tr>" for m, p, a, d in self._docs if p.startswith("/api") or m == "WS")
        page = (f"<!doctype html><meta charset=utf-8><title>{html.escape(self.title)} API</title>"
                "<style>body{font:14px system-ui,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}.m{font-weight:600;color:#2a7fd6}code{font-size:13px}</style>"
                f"<h1>{html.escape(self.title)} {html.escape(self.version)} HTTP API</h1><p>JSON in and out unless noted; errors return <code>{{\"detail\": ...}}</code>. Parameters marked ? are optional. A machine-readable list is at <a href=\"/openapi.json\">/openapi.json</a>; the wiki page HTTP-API describes every endpoint in detail.</p>"
                f"<table><tr><th>Method</th><th>Path</th><th>Query / form</th><th>Notes</th></tr>{rows}</table>")
        return HTMLResponse(page)

    async def _openapi(self, _request: Request) -> Response:
        """A minimal OpenAPI 3 description: routes, methods, parameters and summaries (no body schemas)."""
        kinds = {int: "integer", float: "number", bool: "boolean"}
        paths: Dict[str, Dict[str, Any]] = {}
        for method, path, ps, doc in self._docs:
            if method == "WS":
                continue
            op: Dict[str, Any] = {"operationId": f"{method.lower()}_{path.strip('/').replace('/', '_').replace('{', '').replace('}', '')}"}
            if doc:
                op["summary"] = doc.splitlines()[0]
            params = [{"name": n, "in": w, "required": req or w == "path", "schema": {"type": kinds.get(_hint_args(h)[0], "string")}} for n, h, req, w in ps if w in ("query", "path")]
            if params:
                op["parameters"] = params
            files = [n for n, _h, _r, w in ps if w == "file"]
            if files:
                op["requestBody"] = {"content": {"multipart/form-data": {"schema": {"type": "object", "properties": {f: {"type": "string", "format": "binary"} for f in files}}}, "application/octet-stream": {}}}
            op["responses"] = {"200": {"description": "Successful response"}}
            paths.setdefault(path, {})[method.lower()] = op
        return JSON({"openapi": "3.1.0", "info": {"title": self.title, "version": self.version}, "paths": paths})
