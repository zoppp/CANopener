"""REST routes and MCP tools for dashboards and quick signals (the decoder itself is svc.raw).

server.create_app calls register(app, svc); build_mcp calls register_mcp(mcp, svc, run).
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI

from . import dashboards
from .raw_signals import normalize_all, preview

Out = dict[str, Any]


def register(app: FastAPI, svc) -> None:
    # ---------------------------------------------------------------- dashboards
    @app.get("/api/dashboards")
    async def dash_list():
        return {"dashboards": dashboards.list_all(svc)}

    @app.get("/api/dashboards/{dash_id}")
    async def dash_get(dash_id: str):
        d = dashboards.find(svc, dash_id, by_name=False)      # REST (the UI) addresses dashboards by id
        if d is None:
            raise KeyError(f"no dashboard {dash_id!r}")
        return d

    @app.post("/api/dashboards")
    async def dash_save(body: dict = Body(...)):
        return dashboards.save(svc, body)

    @app.delete("/api/dashboards/{dash_id}")
    async def dash_delete(dash_id: str):
        dashboards.delete(svc, dash_id, by_name=False)
        return {"ok": True}

    # ---------------------------------------------------------------- quick signals (raw frame signals)
    @app.get("/api/raw_signals")
    async def raw_list():
        return {"signals": svc.settings.get("raw_signals") or []}

    @app.post("/api/raw_signals")
    async def raw_set(body: dict = Body(...)):
        svc.settings["raw_signals"] = normalize_all(body.get("signals"))    # a new list: the decoder reloads
        svc.save_settings()
        return {"signals": svc.settings["raw_signals"]}

    @app.post("/api/raw_signals/preview")
    def raw_preview(body: dict = Body(...)):
        return {"results": preview(svc, body.get("signals") or [])}


def register_mcp(mcp, svc, run) -> None:
    @mcp.tool()
    def list_dashboards() -> Out:
        """Saved dashboards: id, name, number of widgets."""
        return {"dashboards": [{"id": d["id"], "name": d["name"], "widgets": len(d.get("widgets", []))}
                               for d in dashboards.list_all(svc)]}

    @mcp.tool()
    def get_dashboard(name: str) -> Out:
        """One dashboard (by name or id) with its widgets: type, grid position x/y/w/h (12 columns), signal keys."""
        return dashboards.find(svc, name) or {"error": f"no dashboard {name!r}"}

    @mcp.tool()
    def save_dashboard(dashboard: dict) -> Out:
        """Create or replace a dashboard: {id?, name, widgets: [{type: plot|xy|gauge|number|lamp, x, y, w, h,
        title, ...}]}. plot: series [{key, color}], window (s); xy: x_key, y_key; gauge/number/lamp: key.
        Signal keys come from get_signals."""
        return run(dashboards.save, svc, dashboard)

    @mcp.tool()
    def delete_dashboard(name: str) -> Out:
        """Delete a dashboard (by name or id)."""
        return run(lambda: dashboards.delete(svc, name) or {"ok": True})
