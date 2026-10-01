"""Studio web application: REST API + WebSocket + static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from starlette.staticfiles import StaticFiles

from cwstudio.web import App, FileResponse, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect

from cwstudio import __version__, hardware
from cwstudio.analysis import MODELS
from cwstudio import tools
from cwstudio.firmware import CRYPTO_TARGETS, SS_VERSIONS
from cwstudio.session import Session

log = logging.getLogger("cwstudio.app")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def create_app(session: Session) -> App:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        session.bus.attach_loop(asyncio.get_running_loop())
        yield
        await asyncio.get_running_loop().run_in_executor(None, session.close)

    app = App(title="ChipWhisperer Studio", version=__version__, docs_url="/api/docs", lifespan=lifespan)
    app.state.session = session

    async def run(fn, *args, **kwargs):
        """Run a blocking session method off the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def err(e: Exception, code: int = 400):
        log.debug("API error", exc_info=True)
        raise HTTPException(status_code=code, detail=f"{type(e).__name__}: {e}")

    async def body(req: Request) -> Dict[str, Any]:
        try:
            data = await req.json()
        except Exception:  # noqa: BLE001
            data = {}
        return data or {}

    # ----- status / meta ------------------------------------------------------
    @app.get("/api/status")
    async def status():
        return session.status()

    @app.get("/api/meta")
    async def meta():
        return {
            "version": __version__,
            "scope_kinds": hardware.SCOPE_KINDS,
            "target_kinds": hardware.TARGET_KINDS,
            "programmers": hardware.PROGRAMMERS,
            "cpa_models": MODELS,
            "platform": hardware.platform_help(),
            "simulate_default": session.simulate_default,
            "data_dir": session.data_dir,
            "host": session.toolchains.host,
            "crypto_targets": CRYPTO_TARGETS,
            "ss_versions": SS_VERSIONS,
        }

    @app.get("/api/devices")
    async def devices():
        return await run(session.worker.call, hardware.list_devices, timeout=30)

    @app.get("/api/logs")
    async def logs(since: int = 0):
        return session.bus.history_since(since, kinds=["log", "capture", "glitch"])

    @app.post("/api/shutdown")
    async def shutdown():
        server = getattr(app.state, "server", None)
        if server is not None:
            server.should_exit = True
        else:
            import signal
            os.kill(os.getpid(), signal.SIGINT)
        return {"ok": True}

    # ----- scope --------------------------------------------------------------
    @app.post("/api/scope/connect")
    async def scope_connect(req: Request):
        p = await body(req)
        try:
            return await run(session.connect_scope, p.get("kind", "auto"), p.get("sn") or None,
                             bool(p.get("force", False)), bool(p.get("default_setup", True)))
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/scope/disconnect")
    async def scope_disconnect():
        await run(session.disconnect_scope)
        return {"ok": True}

    @app.get("/api/scope/settings")
    async def scope_settings():
        try:
            return await run(session.scope_settings)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.put("/api/scope/settings")
    async def scope_set(req: Request):
        p = await body(req)
        try:
            v = await run(session.set_scope_setting, p["path"], p.get("value"))
            return {"path": p["path"], "value": v}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/scope/action/{action}")
    async def scope_action(action: str):
        try:
            return await run(session.scope_action, action)
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- target -------------------------------------------------------------
    @app.post("/api/target/connect")
    async def target_connect(req: Request):
        p = await body(req)
        kind = p.pop("kind", "SimpleSerial2")
        try:
            return await run(session.connect_target, kind, **{k: v for k, v in p.items() if v not in (None, "")})
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/target/disconnect")
    async def target_disconnect():
        await run(session.disconnect_target)
        return {"ok": True}

    @app.get("/api/target/settings")
    async def target_settings():
        try:
            return await run(session.target_settings)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.put("/api/target/settings")
    async def target_set(req: Request):
        p = await body(req)
        try:
            v = await run(session.set_target_setting, p["path"], p.get("value"))
            return {"path": p["path"], "value": v}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/target/program")
    async def target_program(req: Request):
        p = await body(req)
        try:
            return await run(session.program, p.get("programmer", "STM32F"), p["path"])
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/target/program/upload")
    async def target_program_upload(file: UploadFile, programmer: str = "STM32F"):
        try:
            content = await file.read()
            path = session.save_upload(file.filename or "firmware.hex", content)
            res = await run(session.program, programmer, path)
            res["path"] = path
            return res
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/target/serial/write")
    async def serial_write(req: Request):
        p = await body(req)
        try:
            n = await run(session.serial_write, p.get("data", ""), bool(p.get("hex", False)),
                          bool(p.get("newline", True)))
            return {"written": n}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/target/serial")
    async def serial_log(since: float = 0):
        with session._serial_lock:
            return [r for r in session.serial_buffer if r["t"] > since]

    @app.post("/api/target/simpleserial")
    async def simpleserial(req: Request):
        p = await body(req)
        try:
            return await run(session.simpleserial, p.get("cmd", "p"), p.get("data", ""), p.get("read_cmd", "r"),
                             p.get("read_len"))
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- capture ------------------------------------------------------------
    @app.post("/api/capture/start")
    async def capture_start(req: Request):
        p = await body(req)
        try:
            return await run(session.start_capture, p)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/capture/single")
    async def capture_single(req: Request):
        p = await body(req)
        try:
            return await run(session.capture_single, p)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/capture/stop")
    async def capture_stop():
        return await run(session.stop_job)

    # ----- traces -------------------------------------------------------------
    @app.get("/api/traces")
    async def traces():
        return session.store.summary()

    @app.delete("/api/traces")
    async def traces_clear():
        session.store.clear()
        session.bus.publish("traces", session.store.summary())
        return session.store.summary()

    @app.get("/api/traces/stats")
    async def traces_stats(start: int = 0, end: Optional[int] = None):
        data = await run(session.stats_bytes, start, end)
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/api/traces/block")
    async def traces_block(start: int = 0, end: int = 0, step: int = 1):
        data = await run(session.traces_block, start, end, step)
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/api/traces/{index}")
    async def trace_one(index: int):
        try:
            data = session.trace_bytes(index)
        except IndexError:
            raise HTTPException(status_code=404, detail="no such trace")
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/api/traces/{index}/meta")
    async def trace_meta(index: int):
        try:
            wave, tin, tout, key = session.store.get(index)
        except IndexError:
            raise HTTPException(status_code=404, detail="no such trace")
        return {"index": index, "n": int(len(wave)), "textin": tin.hex(), "textout": tout.hex(), "key": key.hex(),
                "min": float(wave.min()) if len(wave) else 0, "max": float(wave.max()) if len(wave) else 0}

    @app.post("/api/traces/export")
    async def traces_export(req: Request):
        p = await body(req)
        try:
            path = await run(session.export_traces, p.get("path", "traces"), p.get("format", "npz"))
            return {"path": path, "count": len(session.store)}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/traces/import")
    async def traces_import(req: Request):
        p = await body(req)
        try:
            n = await run(session.import_traces, p["path"], bool(p.get("replace", True)))
            return {"imported": n, **session.store.summary()}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/traces/import/upload")
    async def traces_import_upload(file: UploadFile, replace: bool = True):
        try:
            content = await file.read()
            d = os.path.join(session.data_dir, "imports")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, os.path.basename(file.filename or "traces.npz"))
            with open(path, "wb") as f:
                f.write(content)
            n = await run(session.import_traces, path, replace)
            return {"imported": n, "path": path, **session.store.summary()}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/traces/download/{fmt}")
    async def traces_download(fmt: str):
        try:
            path = await run(session.export_traces, os.path.join(session.data_dir, "exports", "traces"), fmt)
            if "*" in path:  # the .npy set is several files: send them as one zip
                import glob
                import zipfile
                zpath = path.replace("_*.npy", "_npy.zip")
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in sorted(glob.glob(path)):
                        z.write(f, os.path.basename(f))
                path = zpath
            return FileResponse(path, filename=os.path.basename(path))
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- analysis -----------------------------------------------------------
    @app.post("/api/analysis/cpa/start")
    async def cpa_start(req: Request):
        p = await body(req)
        try:
            return await run(session.start_cpa, p)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/analysis/cpa/stop")
    async def cpa_stop():
        session.stop_cpa()
        return {"ok": True}

    @app.get("/api/analysis/cpa")
    async def cpa_result():
        return session.cpa_result() or {}

    @app.get("/api/analysis/cpa/corr/{b}")
    async def cpa_corr(b: int):
        try:
            return Response(content=session.cpa_corr_bytes(b), media_type="application/octet-stream")
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- glitch -------------------------------------------------------------
    @app.post("/api/glitch/start")
    async def glitch_start(req: Request):
        p = await body(req)
        try:
            return await run(session.start_glitch, p)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/glitch/results")
    async def glitch_results():
        return session.glitch_results()

    @app.post("/api/glitch/export")
    async def glitch_export(req: Request):
        p = await body(req)
        try:
            return {"path": await run(session.export_glitch, p.get("path", "glitch_results.csv"))}
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- toolchains -----------------------------------------------------------
    @app.get("/api/toolchains")
    async def toolchains():
        return await run(session.toolchains.list)

    @app.post("/api/toolchains/refresh")
    async def toolchains_refresh():
        try:
            return await run(session.toolchains.refresh_registry)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/toolchains/custom")
    async def toolchain_add_custom(req: Request):
        p = await body(req)
        try:
            return await run(session.toolchains.add_custom, p)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.delete("/api/toolchains/custom/{tc_id}")
    async def toolchain_remove_custom(tc_id: str):
        try:
            await run(session.toolchains.remove_custom, tc_id)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/toolchains/{tc_id}/install")
    async def toolchain_install(tc_id: str, force: bool = False):
        try:
            return await run(session.toolchains.install, tc_id, False, force)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/toolchains/{tc_id}/cancel")
    async def toolchain_cancel(tc_id: str):
        try:
            return await run(session.toolchains.cancel, tc_id)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.delete("/api/toolchains/{tc_id}")
    async def toolchain_remove(tc_id: str):
        try:
            return await run(session.toolchains.remove, tc_id)
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- firmware ------------------------------------------------------------
    @app.get("/api/firmware")
    async def firmware_catalogue():
        return await run(session.firmware.catalogue)

    @app.put("/api/firmware/sources")
    async def firmware_set_root(req: Request):
        p = await body(req)
        try:
            return await run(session.firmware.set_root, p.get("root") or None)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.put("/api/firmware/channel")
    async def firmware_channel(req: Request):
        p = await body(req)
        try:
            return await run(session.firmware.set_channel, p.get("channel", ""))
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/firmware/sources/check")
    async def firmware_check():
        try:
            return await run(session.firmware.check_updates)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/firmware/sources/fetch")
    async def firmware_fetch(req: Request):
        p = await body(req)
        return await run(session.firmware.fetch_sources, p.get("ref") or None)

    @app.post("/api/firmware/sources/cancel")
    async def firmware_fetch_cancel():
        return await run(session.firmware.cancel_sources)

    @app.post("/api/firmware/plan")
    async def firmware_plan(req: Request):
        p = await body(req)
        try:
            plan = await run(session.firmware.plan, p)
            return {k: v for k, v in plan.items() if k != "env"}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/firmware/build")
    async def firmware_build(req: Request):
        p = await body(req)
        try:
            return await run(session.firmware.build, p)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/firmware/build")
    async def firmware_build_status():
        return session.firmware.build_status()

    @app.get("/api/firmware/build/log")
    async def firmware_build_log(since: int = 0):
        return session.firmware.build_log(since)

    @app.post("/api/firmware/build/cancel")
    async def firmware_build_cancel():
        return await run(session.firmware.cancel_build)

    @app.get("/api/firmware/builds")
    async def firmware_builds():
        return await run(session.firmware.list_builds)

    @app.get("/api/firmware/builds/{name}")
    async def firmware_build_download(name: str):
        path = os.path.join(session.firmware.builds_dir, os.path.basename(name))
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="no such build")
        return FileResponse(path, filename=os.path.basename(path))

    @app.post("/api/firmware/program")
    async def firmware_program(req: Request):
        p = await body(req)
        try:
            return await run(session.program_build, p.get("path") or None, p.get("programmer") or None)
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- notebooks --------------------------------------------------------------
    @app.get("/api/notebooks")
    async def notebooks_list():
        return {"root": session.notebooks.root, "notebooks": await run(session.notebooks.list), "tutorials": session.tutorials.status()}

    @app.get("/api/notebooks/file")
    async def notebook_get(path: str):
        try:
            return await run(session.notebooks.load, path)
        except Exception as e:  # noqa: BLE001
            err(e, 404 if isinstance(e, FileNotFoundError) else 400)

    @app.put("/api/notebooks/file")
    async def notebook_put(req: Request):
        p = await body(req)
        try:
            return await run(session.notebooks.save, p["path"], p["notebook"])
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.delete("/api/notebooks/file")
    async def notebook_delete(path: str):
        try:
            await run(session.notebooks.delete, path)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/notebooks/new")
    async def notebook_new(req: Request):
        p = await body(req)
        try:
            return await run(session.notebooks.create, p.get("name") or "Untitled")
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/notebooks/import")
    async def notebook_import(file: UploadFile):
        try:
            return await run(session.notebooks.import_bytes, file.filename or "imported.ipynb", await file.read())
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/notebooks/download")
    async def notebook_download(path: str):
        try:
            full = session.notebooks.path(path)
        except Exception as e:  # noqa: BLE001
            err(e)
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail="no such notebook")
        return FileResponse(full, filename=os.path.basename(full), media_type="application/x-ipynb+json")

    @app.get("/api/notebooks/asset")
    async def notebook_asset(path: str):
        """Images referenced by notebook markdown (relative to the notebooks folder)."""
        try:
            full = session.notebooks.path(path)
        except Exception as e:  # noqa: BLE001
            err(e)
        if not os.path.isfile(full) or os.path.splitext(full)[1].lower() not in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"):
            raise HTTPException(status_code=404, detail="no such image")
        return FileResponse(full)

    @app.post("/api/notebooks/tutorials/fetch")
    async def tutorials_fetch():
        return await run(session.tutorials.fetch)

    @app.get("/api/kernel")
    async def kernel_status():
        return session.kernel.status()

    @app.get("/api/kernel/variables")
    async def kernel_variables():
        return session.kernel.variables()

    @app.post("/api/kernel/execute")
    async def kernel_execute(req: Request):
        p = await body(req)
        cells = p.get("cells") or ([{"id": p.get("id"), "code": p.get("code", "")}] if "code" in p else [])
        if not cells:
            err(ValueError("nothing to run"))
        return session.kernel.execute(cells, p.get("path"))

    @app.post("/api/kernel/run")
    async def kernel_run(req: Request):
        """Run one cell and wait for its outputs (for scripts and the MCP server)."""
        p = await body(req)
        try:
            return await run(session.kernel.execute_wait, p.get("code", ""), p.get("path"), float(p.get("timeout") or 600))
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/notebooks/run")
    async def notebook_run(req: Request):
        p = await body(req)
        try:
            return await run(session.run_notebook, p["path"], float(p.get("timeout") or 1800), bool(p.get("stop_on_error", True)))
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/kernel/interrupt")
    async def kernel_interrupt():
        return session.kernel.interrupt()

    @app.post("/api/kernel/restart")
    async def kernel_restart():
        return await run(session.kernel.restart)

    # ----- notes and calculator -----------------------------------------------------------
    @app.get("/api/notes")
    async def notes_list():
        return await run(session.notes.list)

    @app.post("/api/notes")
    async def notes_create(req: Request):
        p = await body(req)
        try:
            return await run(session.notes.create, p.get("name"))
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/notes/{name}")
    async def note_get(name: str):
        try:
            return await run(session.notes.get, name)
        except Exception as e:  # noqa: BLE001
            err(e, 404)

    @app.put("/api/notes/{name}")
    async def note_put(name: str, req: Request):
        p = await body(req)
        try:
            if p.get("rename") and p["rename"] != name:
                name = (await run(session.notes.rename, name, p["rename"]))["name"]
            return await run(session.notes.put, name, p.get("text", ""))
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.delete("/api/notes/{name}")
    async def note_delete(name: str):
        try:
            await run(session.notes.delete, name)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.post("/api/calc")
    async def calc(req: Request):
        p = await body(req)
        try:
            return tools.calc(p.get("expr", ""), session.calc_vars)
        except Exception as e:  # noqa: BLE001
            err(e)

    @app.get("/api/calc/variables")
    async def calc_vars():
        return {k: (v if isinstance(v, (int, float, str, list)) else str(v)) for k, v in session.calc_vars.items()}

    @app.post("/api/calc/stats")
    async def calc_stats(req: Request):
        """Statistics of explicit values, of samples start..end of one stored trace, or of one sample across stored traces."""
        p = await body(req)
        try:
            if "values" in p:
                return tools.stats(p["values"])
            if p.get("source") == "trace":
                wave, *_ = session.store.get(int(p.get("index", -1)) if int(p.get("index", -1)) >= 0 else len(session.store) - 1)
                a, b = int(p.get("start") or 0), p.get("end")
                return tools.stats(wave[a:None if b is None else int(b)])
            if p.get("source") == "sample":
                waves = session.store.as_arrays(int(p.get("trace_start") or 0), p.get("trace_end"))[0]
                s0, s1 = int(p["sample"]), int(p.get("sample_end") or int(p["sample"]) + 1)
                return tools.stats(waves[:, s0:s1].mean(axis=1) if s1 - s0 > 1 else waves[:, s0])
            raise ValueError("give values, or source=trace/sample")
        except Exception as e:  # noqa: BLE001
            err(e)

    # ----- websocket ----------------------------------------------------------
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        sub = session.bus.subscribe()
        try:
            await sock.send_text(json.dumps({"type": "hello", "version": __version__,
                                                           "status": session.status()}, default=str))

            async def reader():
                try:
                    while True:
                        await sock.receive_text()  # pings / ignored
                except Exception:  # noqa: BLE001
                    pass

            rtask = asyncio.ensure_future(reader())
            try:
                while True:
                    # Sleep until an event arrives or the client goes away, instead of waking up on a timer.
                    get = asyncio.ensure_future(sub.queue.get())
                    await asyncio.wait({get, rtask}, return_when=asyncio.FIRST_COMPLETED)
                    if not get.done():
                        get.cancel()
                        break
                    frame = get.result().frame()
                    if isinstance(frame, (bytes, bytearray)):
                        await sock.send_bytes(frame)
                    else:
                        await sock.send_text(frame)
            finally:
                rtask.cancel()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("ws closed: %s", e)
        finally:
            session.bus.unsubscribe(sub)

    # ----- static -------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
