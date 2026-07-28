"""HTTP surface: JSON for the data, server-sent events for liveness."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

import orjson
import tomlkit
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import aggregate as A, portable
from .config import PACKAGE_ROOT, PROJECT_ROOT, Settings, settings as default_settings
from .engine import Engine
from .selfupdate import Updater


#: The distribution name, which is not the import name -- ``version("ccm")``
#: always answered "dev", so exports and the build banner never carried a real
#: version.
DIST_NAME = "codex-cache-monitor"


def version(name: str = DIST_NAME) -> str:
    try:
        return _pkg_version(name)
    except PackageNotFoundError:
        # Running from a checkout with nothing installed.
        return "dev"


log = logging.getLogger("ccm.server")


def web_dist() -> Path | None:
    """The built dashboard: the checkout's copy first, then the wheel's.

    Preferring ``web/dist`` means a developer running ``pnpm build`` sees the
    result without also having to refresh the copy baked into the package.
    """
    for candidate in (PROJECT_ROOT / "web" / "dist", PACKAGE_ROOT / "_web"):
        if (candidate / "index.html").is_file():
            return candidate
    return None


def build_identity() -> dict:
    """What code this process is running, as one comparable string.

    A long-lived dashboard tab outlives upgrades: ``just update`` reinstalls the
    wheel and restarts the unit while the browser is still holding the old
    bundle, whose asset URLs the new build no longer serves. The tab needs to
    know to reload itself, so every snapshot carries this and the client compares
    it with the one it started on.

    The digest covers the built UI -- its ``index.html`` names the hashed asset
    files, so any rebuild changes it -- and the contents of the Python sources,
    which is what moves when a server-only change is installed under an unchanged
    version. Contents rather than mtimes: wheel installs take their timestamps
    from the zip entries, which reproducible builds pin, so a real upgrade can
    land with the mtimes it replaced. It also means the opposite mistake is
    avoided -- a touch, a reinstall of identical files, or a branch switched away
    and back is not an update. Deliberately not the process start time either: a
    plain restart on identical code serves the same bundle, and prompting for
    that would train people to ignore the prompt.

    The whole package is a few hundred kilobytes and this is read once per
    process, so hashing it outright is cheaper than being clever about it.
    """
    h = hashlib.sha256()
    h.update(version().encode())
    dist = web_dist()
    if dist is not None:
        try:
            h.update((dist / "index.html").read_bytes())
        except OSError:  # pragma: no cover -- raced with a rebuild
            pass
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        try:
            # Path included, and relative to the package: two modules can hold
            # identical bytes, and one moving between subpackages is a change.
            h.update(path.relative_to(PACKAGE_ROOT).as_posix().encode())
            h.update(path.read_bytes())
        except OSError:  # pragma: no cover -- raced with an install
            pass
    return {"version": version(), "id": h.hexdigest()[:16]}


class ORJSONResponse(JSONResponse):
    """Series payloads are large and numeric; orjson serialises them far faster."""

    media_type = "application/json"

    def render(self, content) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_SERIALIZE_NUMPY)


def parse_list(raw: list[str] | None) -> list[str]:
    """Accept both repeated params and comma-separated values."""
    if not raw:
        return []
    out: list[str] = []
    for item in raw:
        out.extend(part for part in item.split(",") if part != "")
    return out


def build_filters(
    start: int | None,
    end: int | None,
    model: list[str] | None,
    provider: list[str] | None,
    repo: list[str] | None,
    subagent: str,
    source: list[str] | None = None,
    origin: list[str] | None = None,
) -> A.Filters:
    return A.Filters(
        start_ms=start,
        end_ms=end,
        # "local" names this machine, whose origin is the empty string -- which
        # a query parameter cannot carry, the same trick as "direct" below.
        origins=["" if o == "local" else o for o in parse_list(origin)],
        sources=parse_list(source),
        models=parse_list(model),
        # An empty-string provider means "direct", which parse_list would eat,
        # so it is passed through as the sentinel "direct".
        providers=["" if p == "direct" else p for p in parse_list(provider)],
        repos=parse_list(repo),
        subagent=subagent if subagent in ("all", "main", "sub") else "all",
    )


def create_app(settings: Settings | None = None, *, watch: bool = True) -> FastAPI:
    settings = settings or default_settings
    engine = Engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine.start(watch=watch)
        try:
            yield
        finally:
            engine.stop()

    app = FastAPI(
        title="Agent Cache Monitor",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
    )
    app.state.engine = engine
    updater = Updater(settings)
    app.state.updater = updater
    # Read once: the answer only changes when the process is replaced, and this
    # rides along on every snapshot and every stream reconnect.
    app.state.build = build_identity()

    @app.middleware("http")
    async def refuse_cross_site_writes(request: Request, call_next):
        """No page from another site gets to change anything here.

        Found while thinking about the update endpoint, but it is not special:
        the dashboard has no authentication, so a page in any tab could POST to
        localhost and delete an imported machine or rewrite the rate table.
        Reads are left alone -- they are already public to anything that can
        reach the port, and this is not authentication.
        """
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            refusal = Updater.cross_site_problem(request.headers)
            if refusal:
                return JSONResponse({"detail": refusal}, status_code=403)
        return await call_next(request)

    def snapshot() -> dict:
        """The engine's view plus which build served it."""
        return {**engine.snapshot(), "build": app.state.build}

    def filters_from(request: Request) -> A.Filters:
        q = request.query_params
        return build_filters(
            int(q["start"]) if q.get("start") else None,
            int(q["end"]) if q.get("end") else None,
            q.getlist("model"),
            q.getlist("provider"),
            q.getlist("repo"),
            q.get("subagent", "all"),
            q.getlist("source"),
            q.getlist("origin"),
        )

    # -- state ------------------------------------------------------------

    @app.get("/api/state")
    def get_state() -> dict:
        return snapshot()

    @app.get("/api/totals")
    def get_totals(request: Request) -> dict:
        return A.totals(engine.store, engine.pricing, filters_from(request))

    @app.get("/api/series")
    def get_series(
        request: Request,
        bucket: str = "hour",
        group: str | None = None,
        limit_groups: int = 12,
    ) -> dict:
        if bucket not in A.BUCKET_SECONDS:
            raise HTTPException(400, f"unknown bucket {bucket!r}")
        if group and group not in A.GROUPABLE:
            raise HTTPException(400, f"cannot group by {group!r}")
        return A.series(
            engine.store,
            engine.pricing,
            filters_from(request),
            bucket=bucket,
            group=group or None,
            limit_groups=limit_groups,
        )

    @app.get("/api/breakdown/{dimension}")
    def get_breakdown(dimension: str, request: Request) -> list[dict]:
        if dimension not in A.GROUPABLE:
            raise HTTPException(400, f"cannot group by {dimension!r}")
        return A.breakdown(engine.store, engine.pricing, filters_from(request), dimension)

    @app.get("/api/heatmap")
    def get_heatmap(request: Request, tz_offset: int = 0) -> dict:
        return A.heatmap(engine.store, engine.pricing, filters_from(request), tz_offset)

    @app.get("/api/calendar")
    def get_calendar(request: Request, tz_offset: int = 0) -> dict:
        return A.calendar(engine.store, engine.pricing, filters_from(request), tz_offset)

    @app.get("/api/scatter")
    def get_scatter(request: Request, bins: int = 40) -> dict:
        return A.context_scatter(
            engine.store, engine.pricing, filters_from(request), bins=max(4, min(bins, 80))
        )

    @app.get("/api/events")
    def get_events(request: Request) -> list[dict]:
        return A.event_markers(engine.store, filters_from(request))

    @app.get("/api/quota")
    def get_quota(request: Request) -> list[dict]:
        return A.quota_series(engine.store, filters_from(request))

    @app.get("/api/sessions")
    def get_sessions(request: Request, limit: int = 2000, offset: int = 0) -> dict:
        rows = A.sessions(engine.store, engine.pricing, filters_from(request))
        return {"total": len(rows), "rows": rows[offset : offset + limit]}

    @app.get("/api/sessions/{rollout_id}")
    def get_session(rollout_id: str) -> dict:
        detail = A.session_detail(engine.store, engine.pricing, rollout_id)
        if detail["meta"] is None:
            raise HTTPException(404, "unknown rollout")
        return detail

    @app.get("/api/quality")
    def get_quality() -> dict:
        return A.data_quality(engine.store, engine.pricing)

    # -- pricing -----------------------------------------------------------

    @app.get("/api/pricing")
    def get_pricing() -> dict:
        return engine.pricing.as_dict()

    @app.put("/api/pricing")
    def put_pricing(payload: dict = Body(...)) -> dict:
        """Edit rates in place, preserving comments and layout in the TOML."""
        models = payload.get("models")
        if not isinstance(models, dict):
            raise HTTPException(400, "expected {'models': {...}}")
        path = engine.pricing.path
        doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
        table = doc.setdefault("models", tomlkit.table())
        for name, spec in models.items():
            entry = table.get(name)
            if entry is None:
                entry = tomlkit.table()
                table[name] = entry
            for field in (
                "input",
                "cached_input",
                "output",
                "cache_write",
                "cache_write_1h",
                "long_context_threshold",
            ):
                if field in spec and spec[field] is not None:
                    entry[field] = spec[field]
            long_spec = spec.get("long")
            if isinstance(long_spec, dict):
                sub = entry.get("long")
                if sub is None:
                    sub = tomlkit.table()
                    entry["long"] = sub
                for field in ("input", "cached_input", "output"):
                    if field in long_spec and long_spec[field] is not None:
                        sub[field] = long_spec[field]
        path.write_text(tomlkit.dumps(doc))
        engine.pricing.reload()
        # Only a threshold change can invalidate the rollup; the refresh below
        # detects that via the fingerprint and rebuilds only when needed.
        engine._refresh_derived(force=True)
        return engine.pricing.as_dict()

    # -- reference prices ---------------------------------------------------

    @app.get("/api/reference-prices")
    def get_reference_prices() -> dict:
        return {
            **engine.reference.status(),
            "models": engine.reference.compare(
                engine.pricing, A.token_volumes(engine.store)
            ),
        }

    @app.post("/api/reference-prices/refresh")
    def post_reference_refresh() -> dict:
        engine.reference.refresh()
        return get_reference_prices()

    # -- machines: export and import ----------------------------------------

    @app.get("/api/machines")
    def get_machines() -> dict:
        return {
            "local_label": engine.local_label,
            "machines": portable.list_origins(engine.store, engine.pricing, A),
        }

    @app.put("/api/machines/local-label")
    def put_local_label(payload: dict = Body(...)) -> dict:
        engine.set_local_label(str(payload.get("label") or ""))
        return get_machines()

    @app.get("/api/export")
    def get_export(request: Request) -> Response:
        q = request.query_params
        label = q.get("label") or engine.local_label
        raw = parse_list(q.getlist("origin"))
        origins = None if not raw else ["" if o == "local" else o for o in raw]
        bundle = portable.export_bundle(
            engine.store,
            engine.pricing,
            label=label,
            origins=origins,
            tool_version=version(),
        )
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", bundle["label"]).strip("-") or "export"
        return Response(
            orjson.dumps(bundle),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="ccm-{name}.json"'
            },
        )

    @app.post("/api/import/preview")
    def post_import_preview(payload: dict = Body(...)) -> dict:
        try:
            return portable.preview(engine.store, payload)
        except portable.BundleError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/import")
    def post_import(payload: dict = Body(...)) -> dict:
        bundle = payload.get("bundle")
        if not isinstance(bundle, dict):
            raise HTTPException(400, "expected {'bundle': {...}, 'label': '...'}")
        try:
            result = portable.import_bundle(engine.store, bundle, payload.get("label"))
        except portable.BundleError as exc:
            raise HTTPException(400, str(exc)) from exc
        engine.refresh_now()
        return {**result, "machines": portable.list_origins(engine.store, engine.pricing, A)}

    @app.patch("/api/machines/{origin}")
    def patch_machine(origin: str, payload: dict = Body(...)) -> dict:
        label = str(payload.get("label") or "").strip()
        if not label:
            raise HTTPException(400, "label is required")
        if not origin:
            engine.set_local_label(label)
        else:
            portable.rename_import(engine.store, origin, label)
        engine.refresh_now()
        return get_machines()

    @app.delete("/api/machines/{origin}")
    def delete_machine(origin: str) -> dict:
        if not portable.delete_import(engine.store, origin):
            raise HTTPException(404, "unknown machine")
        engine.refresh_now()
        return get_machines()

    @app.post("/api/rescan")
    def post_rescan(full: bool = False) -> dict:
        # Refused rather than queued while paused: a full rescan drops the
        # derived tables, so honouring it now would empty the dashboard and
        # leave nothing running to refill it.
        if engine.paused:
            raise HTTPException(409, "scanning is paused")
        if full:
            engine.rescan_from_scratch()
        else:
            engine.request_scan()
        return {"ok": True, "full": full}

    @app.post("/api/scan/pause")
    def post_pause() -> dict:
        return {"paused": engine.set_paused(True)}

    @app.post("/api/scan/resume")
    def post_resume() -> dict:
        return {"paused": engine.set_paused(False)}

    # -- self-update --------------------------------------------------------

    @app.get("/api/update")
    def get_update(request: Request) -> dict:
        status = updater.status()
        # The address is part of the answer, not just of the decision: the button
        # has to be able to say "not from here" before it is pressed.
        refusal = updater.may_start_from(request.client.host if request.client else None)
        if refusal:
            # The transcript and the checkout path are withheld, not just the
            # verdict: together they name a filesystem path (so the username),
            # the commits being deployed, and whatever paths a failing build
            # printed -- reconnaissance handed to exactly the caller we just
            # refused.
            return {
                **status.as_dict(),
                "available": False,
                "reason": refusal,
                "log": "",
                "checkout": None,
            }
        return status.as_dict()

    @app.post("/api/update")
    def post_update(request: Request) -> dict:
        refusal = (
            updater.may_start_from(request.client.host if request.client else None)
            or updater.cross_site_problem(request.headers)
            or updater.host_header_problem(request.headers)
        )
        if refusal:
            # 403 rather than 409: this is about who is asking, and no amount of
            # retrying from the same place will change the answer.
            raise HTTPException(403, refusal)
        try:
            return updater.start().as_dict()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    # -- live stream --------------------------------------------------------

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        loop = asyncio.get_running_loop()
        sub = engine.subscribe(loop)

        async def gen():
            try:
                yield sse("hello", snapshot())
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        message = await asyncio.wait_for(sub.queue.get(), timeout=15.0)
                    except TimeoutError:
                        # Keeps proxies and browsers from closing an idle stream.
                        yield b": keepalive\n\n"
                        continue
                    yield sse(message["event"], message["data"])
            finally:
                engine.unsubscribe(sub)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- static -------------------------------------------------------------

    dist = web_dist()
    if dist is not None:
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    else:

        @app.get("/")
        def missing_ui() -> Response:
            return Response(
                "The web UI has not been built yet.\n\n  just build-web\n",
                media_type="text/plain",
                status_code=503,
            )

    return app


def sse(event: str, data) -> bytes:
    body = orjson.dumps(data)
    return b"event: " + event.encode() + b"\ndata: " + body + b"\n\n"
