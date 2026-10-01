"""A small routing layer on Starlette: FastAPI-style decorators without pydantic.

Endpoints are plain ``async def`` functions. Parameters are filled by name and type hint: ``Request`` gets the request, ``UploadFile`` gets an uploaded file (multipart form field of that name, or the raw request body with ``?filename=``), names that appear in the path come from the path and everything else from the query string, converted to the annotated type (int, float, bool, str, Optional[...]). A returned ``Response`` is sent as is; anything else is sent as JSON. ``HTTPException`` becomes ``{"detail": ...}`` like FastAPI, so clients see the same errors.
"""
from __future__ import annotations

import html
import inspect
import json
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


class UploadFile:
    """An uploaded file held in memory (firmware images, trace sets and notebooks are small)."""

    def __init__(self, filename: Optional[str], content: bytes, content_type: str = "application/octet-stream"):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self) -> bytes:
        return self._content


def _default(o: Any) -> Any:
    """JSON fallback for values the session returns: numpy scalars and arrays, sets, tuples, bytes, paths."""
    if hasattr(o, "item") and getattr(o, "ndim", 1) == 0:
        return o.item()
    if hasattr(o, "tolist"):
        return o.tolist()
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    return str(o)


class JSON(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=_default).encode("utf-8")


def _multipart(body: bytes, content_type: str) -> Dict[str, Tuple[Optional[str], bytes, str]]:
    """Split a multipart/form-data body into {field: (filename, content, content type)}."""
    boundary = None
    for part in content_type.split(";")[1:]:
        k, _, v = part.strip().partition("=")
        if k.lower() == "boundary":
            boundary = v.strip('"')
    if not boundary:
        raise HTTPException(400, "multipart body without boundary")
    out: Dict[str, Tuple[Optional[str], bytes, str]] = {}
    for chunk in body.split(b"--" + boundary.encode("latin-1"))[1:]:
        if chunk.startswith(b"--"):
            break
        head, sep, content = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        name, filename, ctype = None, None, "application/octet-stream"
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-disposition":
                for item in value.split(";")[1:]:
                    pk, _, pv = item.strip().partition("=")
                    pv = pv.strip().strip('"')
                    if pk.lower() == "name":
                        name = pv
                    elif pk.lower() == "filename":
                        filename = pv.replace("\\", "/").rsplit("/", 1)[-1]
            elif key.strip().lower() == "content-type":
                ctype = value.strip()
        if name is not None:
            out[name] = (filename, content, ctype)
    return out


def _convert(raw: str, hint: Any) -> Any:
    origin = typing.get_origin(hint)
    if origin is typing.Union:  # Optional[X]
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        hint = args[0] if len(args) == 1 else str
    if hint is bool:
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    if hint is int:
        return int(raw)
    if hint is float:
        return float(raw)
    return raw


class App(Starlette):
    """Starlette app with ``@app.get/post/put/delete/websocket`` decorators and a generated ``/api/docs`` page."""

    def __init__(self, title: str = "", version: str = "", docs_url: Optional[str] = "/api/docs", lifespan=None):
        super().__init__(lifespan=lifespan, exception_handlers={HTTPException: self._http_error})
        self.title, self.version = title, version
        self._docs: List[Tuple[str, str, List[str], str]] = []
        if docs_url:
            self.router.routes.append(Route(docs_url, self._docs_page, methods=["GET"]))

    @staticmethod
    async def _http_error(_request: Request, exc: HTTPException) -> Response:
        return JSON({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    def _endpoint(self, fn: Callable, path: str) -> Callable:
        hints = typing.get_type_hints(fn)
        params = []
        for name, p in inspect.signature(fn).parameters.items():
            params.append((name, hints.get(name, str), p.default, "{" + name in path))

        async def endpoint(request: Request) -> Response:
            kwargs: Dict[str, Any] = {}
            form = None
            for name, hint, default, in_path in params:
                if hint is Request:
                    kwargs[name] = request
                    continue
                if hint is UploadFile:
                    ctype = request.headers.get("content-type", "")
                    body = await request.body()
                    if ctype.startswith("multipart/form-data"):
                        form = form if form is not None else _multipart(body, ctype)
                        if name not in form:
                            raise HTTPException(422, f"missing file field '{name}'")
                        kwargs[name] = UploadFile(*form[name])
                    else:
                        kwargs[name] = UploadFile(request.query_params.get("filename"), body, ctype or "application/octet-stream")
                    continue
                raw = request.path_params.get(name) if in_path else request.query_params.get(name)
                if raw is None:
                    if default is inspect.Parameter.empty:
                        raise HTTPException(422, f"missing parameter '{name}'")
                    kwargs[name] = default
                    continue
                try:
                    kwargs[name] = _convert(raw, hint)
                except ValueError as e:
                    raise HTTPException(422, f"invalid parameter '{name}': {e}") from None
            result = await fn(**kwargs)
            return result if isinstance(result, Response) else JSON(result)

        return endpoint

    def _add(self, methods: List[str], path: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.router.routes.append(Route(path, self._endpoint(fn, path), methods=methods, name=fn.__name__))
            args = [n for n, p in inspect.signature(fn).parameters.items() if n not in ("req", "request") and "{" + n not in path]
            self._docs.append((methods[0], path, args, inspect.getdoc(fn) or ""))
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
        rows = "".join(f"<tr><td class=m>{m}</td><td><code>{html.escape(p)}</code></td><td>{html.escape(', '.join(a))}</td><td>{html.escape(d)}</td></tr>" for m, p, a, d in self._docs if p.startswith("/api") or m == "WS")
        page = (f"<!doctype html><meta charset=utf-8><title>{html.escape(self.title)} API</title>"
                "<style>body{font:14px system-ui,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}.m{font-weight:600;color:#2a7fd6}code{font-size:13px}</style>"
                f"<h1>{html.escape(self.title)} {html.escape(self.version)} HTTP API</h1><p>JSON in and out unless noted; errors return <code>{{\"detail\": \"...\"}}</code>. The wiki page HTTP-API describes every endpoint in detail.</p>"
                f"<table><tr><th>Method</th><th>Path</th><th>Query / form</th><th>Notes</th></tr>{rows}</table>")
        return HTMLResponse(page)
