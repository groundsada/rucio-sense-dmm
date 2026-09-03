import logging
import os
import asyncio
from datetime import datetime
from json import JSONDecodeError
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from sqlmodel import select

from dmm.db.session import databased, get_session
from dmm.models.request import Request as DBRequest, RequestStatus
from dmm.models.site import Site
from dmm.models.mesh import Mesh
from dmm.core.config import config_get_int
from dmm.core.allocation import refresh_all_sites
from dmm.api.serializers import mesh_to_dict, request_to_dict, site_to_dict

from rucio.client import Client

current_directory = os.path.dirname(os.path.abspath(__file__))
templates_folder = os.path.join(current_directory, "templates")
static_folder = os.path.join(current_directory, "static")

templates = Jinja2Templates(directory=templates_folder)

api = FastAPI()
api.mount("/static", StaticFiles(directory=static_folder), name="static")


async def _parse_json_or_400(request: Request) -> dict:
    try:
        data = await request.json()
    except (JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="JSON payload must be an object")
    return data


def _require_rule_id(data: dict) -> str:
    rule_id = data.get("rule_id")
    if not rule_id or not isinstance(rule_id, str):
        raise HTTPException(status_code=400, detail="Missing or invalid 'rule_id'")
    return rule_id


def _get_request_or_404(rule_id: str, session):
    req = DBRequest.get_by_id(rule_id, session=session)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Request '{rule_id}' not found")
    return req


def _validate_sense_request(req):
    if req.transfer_status == RequestStatus.NOT_SENSE:
        raise HTTPException(status_code=400, detail="This is not a SENSE rule")


def _prometheus_escape_label(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _emit_gauge(lines: list[str], name: str, value, labels: dict | None = None):
    if value is None:
        return
    if labels:
        label_blob = ",".join(
            f'{k}="{_prometheus_escape_label(v)}"' for k, v in labels.items()
        )
        lines.append(f"{name}{{{label_blob}}} {value}")
    else:
        lines.append(f"{name} {value}")


@api.get("/metrics", response_class=PlainTextResponse)
@databased
async def metrics(session=None):
    reqs = DBRequest.get_all(session=session)

    lines: list[str] = []

    lines.append("# HELP dmm_requests_total Total number of requests tracked by DMM")
    lines.append("# TYPE dmm_requests_total gauge")
    lines.append(f"dmm_requests_total {len(reqs)}")

    status_counts: dict[str, int] = {}
    for req in reqs:
        status_key = str(req.transfer_status or "UNKNOWN")
        status_counts[status_key] = status_counts.get(status_key, 0) + 1

    lines.append("# HELP dmm_requests_by_status Number of requests by transfer status")
    lines.append("# TYPE dmm_requests_by_status gauge")
    for status_key, count in sorted(status_counts.items()):
        _emit_gauge(lines, "dmm_requests_by_status", count, {"status": status_key})

    lines.append("# HELP dmm_request_info Request state marker (always 1) with core identifying labels, including the SENSE instance uuid")
    lines.append("# TYPE dmm_request_info gauge")
    lines.append("# HELP dmm_request_sense_retries Number of SENSE retries for request")
    lines.append("# TYPE dmm_request_sense_retries gauge")
    lines.append("# HELP dmm_request_allocated_bandwidth_mbps Allocated bandwidth in Mbps")
    lines.append("# TYPE dmm_request_allocated_bandwidth_mbps gauge")
    lines.append("# HELP dmm_request_available_bandwidth_mbps Available bandwidth in Mbps")
    lines.append("# TYPE dmm_request_available_bandwidth_mbps gauge")
    lines.append("# HELP dmm_request_previous_bandwidth_mbps Previous bandwidth in Mbps")
    lines.append("# TYPE dmm_request_previous_bandwidth_mbps gauge")
    lines.append("# HELP dmm_request_fts_streams_current Current FTS streams")
    lines.append("# TYPE dmm_request_fts_streams_current gauge")
    lines.append("# HELP dmm_request_fts_streams_desired Desired FTS streams")
    lines.append("# TYPE dmm_request_fts_streams_desired gauge")
    lines.append("# HELP dmm_request_prometheus_throughput_gbps Measured throughput in Gbps")
    lines.append("# TYPE dmm_request_prometheus_throughput_gbps gauge")
    lines.append("# HELP dmm_request_prometheus_bytes Measured bytes from prometheus polling")
    lines.append("# TYPE dmm_request_prometheus_bytes gauge")
    lines.append("# HELP dmm_request_health Health status (1=healthy, 0=unhealthy, absent=unknown)")
    lines.append("# TYPE dmm_request_health gauge")
    lines.append("# HELP dmm_request_priority Rucio rule priority as submitted")
    lines.append("# TYPE dmm_request_priority gauge")
    lines.append("# HELP dmm_request_modified_priority Rule priority after operator override, absent when never overridden")
    lines.append("# TYPE dmm_request_modified_priority gauge")

    for req in reqs:
        labels = {
            "rule_id": req.rule_id,
            "transfer_status": str(req.transfer_status or "UNKNOWN"),
            "sense_circuit_status": str(req.sense_circuit_status or "UNKNOWN"),
            "src_site": req.src_site.name if req.src_site else "UNKNOWN",
            "dst_site": req.dst_site.name if req.dst_site else "UNKNOWN",
        }

        # Rucio's own site names go on the info metric only, to keep the label
        # sets of the numeric gauges unchanged.
        info_labels = dict(labels)
        info_labels["src_rse"] = req.src_logical_site or labels["src_site"]
        info_labels["dst_rse"] = req.dst_logical_site or labels["dst_site"]
        # The join key for rule_id -> SENSE instance -> rtmon dashboard. Safe
        # as a label: one bounded value per rule, on a series already keyed by
        # rule_id, so it adds no cardinality.
        info_labels["sense_uuid"] = req.sense_uuid or ""
        reason = req.failure_reason or ""
        # Keep label value bounded — full reason lives on the dashboard/details page.
        if len(reason) > 256:
            reason = reason[:253] + "..."
        info_labels["failure_reason"] = reason
        _emit_gauge(lines, "dmm_request_info", 1, info_labels)
        _emit_gauge(lines, "dmm_request_sense_retries", req.sense_retries if req.sense_retries is not None else 0, labels)
        _emit_gauge(lines, "dmm_request_allocated_bandwidth_mbps", req.allocated_bandwidth_mbps, labels)
        _emit_gauge(lines, "dmm_request_available_bandwidth_mbps", req.available_bandwidth_mbps, labels)
        _emit_gauge(lines, "dmm_request_previous_bandwidth_mbps", req.previous_bandwidth_mbps, labels)
        _emit_gauge(lines, "dmm_request_fts_streams_current", req.fts_streams_current, labels)
        _emit_gauge(lines, "dmm_request_fts_streams_desired", req.fts_streams_desired, labels)
        _emit_gauge(lines, "dmm_request_prometheus_throughput_gbps", req.prometheus_throughput, labels)
        _emit_gauge(lines, "dmm_request_prometheus_bytes", req.prometheus_bytes, labels)
        # _emit_gauge skips None, so modified_priority is simply absent for a
        # rule the operator never overrode, rather than reported as 0.
        _emit_gauge(lines, "dmm_request_priority", req.priority, labels)
        _emit_gauge(lines, "dmm_request_modified_priority", req.modified_priority, labels)

        if req.health is not None:
            if str(req.health) == "1":
                _emit_gauge(lines, "dmm_request_health", 1, labels)
            elif str(req.health) == "0":
                _emit_gauge(lines, "dmm_request_health", 0, labels)

    payload = "\n".join(lines) + "\n"
    return PlainTextResponse(payload, media_type="text/plain; version=0.0.4; charset=utf-8")

@api.get("/query/{rule_id}")
@databased
async def handle_client(rule_id: str, session=None):
    logging.info(f"Received request for rule_id: {rule_id}")
    max_retries = config_get_int("rucio", "max_retries", default=2)

    retry_count = 0
    
    while retry_count < max_retries:
        try:
            req = DBRequest.get_by_id(rule_id, session=session, use_lock=False)
            if req:
                if req.src_endpoint and req.dst_endpoint:
                    result = {"source": req.src_endpoint.hostname, "destination": req.dst_endpoint.hostname}
                    return JSONResponse(content=result)
                else:
                    retry_count += 1
                    if retry_count < max_retries:
                        logging.info(f"Request {rule_id} not yet allocated, retrying in 15 seconds (attempt {retry_count}/{max_retries})")
                        await asyncio.sleep(15)
                    else:
                        raise HTTPException(status_code=404, detail="Request not yet allocated after retries")
            else:
                raise HTTPException(status_code=404, detail="Request not found")
        except HTTPException:
            raise
        except Exception as e:
            logging.error(f"Error processing client request: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail="Internal server error")
    
    raise HTTPException(status_code=404, detail="Request not yet allocated")

# response_class on the three template handlers below is not cosmetic: without
# it FastAPI documents them as application/json, which is what the generated
# OpenAPI spec advertised while the handlers returned HTML.
@api.get("/", response_class=HTMLResponse)
@databased
async def get_dmm_status(request: Request, session=None):
    try:
        reqs = DBRequest.get_all(session=session)
        return templates.TemplateResponse(request, "index.html", {"data": reqs})
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@api.get("/sites", response_class=HTMLResponse)
@databased
async def get_sites(request: Request, session=None):
    try:
        sites = Site.get_all(session=session)
        return templates.TemplateResponse(request, "sites.html", {"data": sites})
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@api.get("/details/{rule_id}", response_class=HTMLResponse)
@databased
async def open_rule_details(request: Request, rule_id: str, session=None):
    try:
        req = DBRequest.get_by_id(rule_id, session=session, use_lock=False)
        return templates.TemplateResponse(request, "details.html", {"data": req})
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ---------------------------------------------------------------------------
# JSON API
#
# The HTML endpoints above stay exactly as they are. These expose the same
# objects to machines: the templates already receive every field, they were
# just never reachable except by parsing the rendered page.
# ---------------------------------------------------------------------------

@api.get("/api/requests")
@databased
async def api_list_requests(
    status: Optional[str] = Query(None, description="Filter by transfer status, exact match"),
    site: Optional[str] = Query(None, description="Filter by source or destination site"),
    sense_uuid: Optional[str] = Query(None, description="Filter by SENSE instance uuid"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    session=None,
):
    try:
        reqs = DBRequest.get_all(session=session)

        if status:
            reqs = [r for r in reqs if str(r.transfer_status or "") == status]
        if site:
            reqs = [
                r for r in reqs
                if site in (
                    r.src_site.name if r.src_site else None,
                    r.dst_site.name if r.dst_site else None,
                    r.src_logical_site,
                    r.dst_logical_site,
                )
            ]
        if sense_uuid:
            reqs = [r for r in reqs if r.sense_uuid == sense_uuid]

        # Stable order, so offset/limit paging cannot skip or repeat a row.
        reqs.sort(key=lambda r: r.rule_id)
        total = len(reqs)
        page = reqs[offset:offset + limit]

        return JSONResponse(content={
            "total": total,
            "count": len(page),
            "offset": offset,
            "limit": limit,
            "requests": [request_to_dict(r, detail=False) for r in page],
        })
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@api.get("/api/requests/{rule_id}")
@databased
async def api_get_request(rule_id: str, session=None):
    try:
        req = DBRequest.get_by_id(rule_id, session=session, use_lock=False)
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
    if req is None:
        raise HTTPException(status_code=404, detail=f"Request '{rule_id}' not found")
    return JSONResponse(content=request_to_dict(req, detail=True))


@api.get("/api/sites")
@databased
async def api_list_sites(session=None):
    try:
        sites = Site.get_all(session=session)
        return JSONResponse(content={
            "count": len(sites),
            "sites": [site_to_dict(s, detail=True) for s in sites],
        })
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@api.get("/api/mesh")
@databased
async def api_list_mesh(session=None):
    try:
        links = Mesh.get_all(session=session)
        return JSONResponse(content={
            "count": len(links),
            "links": [mesh_to_dict(link) for link in links],
        })
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@api.post("/mark_finished")
@databased
async def mark_finished(request: Request, session=None):
    try:
        data = await _parse_json_or_400(request)
        rule_id = _require_rule_id(data)
        req = _get_request_or_404(rule_id, session)
        _validate_sense_request(req)
        req.set_status(RequestStatus.FINISHED_R, session=session)
        req.update({"rucio_finished_at": datetime.now()}, session=session)
        return "Request marked as finished"
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to mark request as finished")

@api.post("/update_fts_limit")
@databased
async def update_fts_limit(request: Request, session=None):
    try:
        data = await _parse_json_or_400(request)
        rule_id = _require_rule_id(data)
        limit = data.get("limit")
        if limit is None or not isinstance(limit, int) or limit < 0:
            raise HTTPException(status_code=400, detail="Missing or invalid 'limit' (must be non-negative integer)")

        req = _get_request_or_404(rule_id, session)
        _validate_sense_request(req)
        if req.transfer_status not in [RequestStatus.CANCELED, RequestStatus.FINISHED, RequestStatus.DELETED]:
            req.set_fts_streams(desired=limit, session=session)
            return "FTS limit updated"
        else:
            raise HTTPException(status_code=400, detail="Cannot update FTS limit for cancelled, finished or deleted requests")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to update FTS limit")

@api.post("/reinitialize_sense")
@databased
async def reinitialize_sense(request: Request, session=None):
    try:
        data = await _parse_json_or_400(request)
        rule_id = _require_rule_id(data)
        req = _get_request_or_404(rule_id, session)
        _validate_sense_request(req)
        req.set_status(RequestStatus.ALLOCATED, session=session)
        return "Request reinitialized"
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to reinitialize request")

@api.post("/reinitialize_request")
@databased
async def reinitialize_request(request: Request, session=None):
    try:
        data = await _parse_json_or_400(request)
        rule_id = _require_rule_id(data)
        req = _get_request_or_404(rule_id, session)
        _validate_sense_request(req)
        req.set_status(RequestStatus.INIT, session=session)
        return "Request reinitialized"
    except HTTPException:
        raise
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to reinitialize request")


@api.post("/refresh_sites")
@databased
async def refresh_sites(session=None):
    try:
        client = Client()
        refresh_all_sites(client, session)
        return "Sites refreshed"
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to refresh sites")
    
@api.get("/logs", response_class=PlainTextResponse)
async def get_logs(
    lines: int = Query(2000, ge=1, le=20000, description="Number of trailing lines"),
    rule_id: Optional[str] = Query(None, description="Only lines mentioning this rule_id"),
    level: Optional[str] = Query(None, description="Only lines at this level, e.g. ERROR"),
):
    log_path = "dmm.log"
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        with open(log_path, "r") as f:
            content = f.readlines()

        # Filter before tailing, so `lines` counts matching lines rather than
        # whichever ones happen to survive the filter.
        if rule_id:
            content = [ln for ln in content if rule_id in ln]
        if level:
            needle = level.upper()
            content = [ln for ln in content if needle in ln.upper()]

        return PlainTextResponse("".join(content[-lines:]))
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to read log file")

@api.get("/health/live")
async def liveness_check():
    """Can this process serve? Nothing more.

    The daemons run in the parent process, so if that dies the container goes
    with it and this stops answering anyway. Checking anything else here would
    restart the pod, and its in-flight circuits, for a transient dependency
    blip.
    """
    return {"status": "alive"}


@api.get("/health")
async def health_check():
    """Readiness: is the database actually reachable?

    Deliberately not @databased. That decorator commits on the way out and
    re-raises, which would turn an unreachable database into a 500 rather than
    the 503 a probe can act on -- an unreachable database is a health answer,
    not a health failure.

    Daemon liveness is not checked. The frontend is a separate process from the
    daemons, so it cannot see their state without a heartbeat mechanism; that
    lives on the otel branch and is out of scope here. This endpoint reports
    only what it can actually observe.
    """
    checks = []
    healthy = True

    try:
        with get_session() as session:
            session.exec(select(Site)).first()
        checks.append({"name": "database", "status": "ok"})
    except Exception as e:
        logging.error(f"health check could not reach the database: {e}", exc_info=True)
        checks.append({"name": "database", "status": "failed", "error": str(e)})
        healthy = False

    return JSONResponse(
        content={"status": "healthy" if healthy else "unhealthy", "checks": checks},
        status_code=200 if healthy else 503,
    )