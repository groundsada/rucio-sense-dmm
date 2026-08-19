import logging
import os
import asyncio
from datetime import datetime
from json import JSONDecodeError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST

from dmm.db.session import databased, get_session
from dmm.models.request import Request as DBRequest, RequestStatus
from dmm.models.site import Site
from dmm.core.config import config_get_int
from dmm.core.allocation import refresh_all_sites
from dmm.core.health import health_report
from dmm.core.metrics import render_requests
from dmm.core.timeutil import utcnow

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


@api.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(render_requests(), media_type=CONTENT_TYPE_LATEST)


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

@api.get("/")
@databased
async def get_dmm_status(request: Request, session=None):
    try:
        reqs = DBRequest.get_all(session=session)
        return templates.TemplateResponse(request, "index.html", {"data": reqs})
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@api.get("/sites")
@databased
async def get_sites(request: Request, session=None):
    try:
        sites = Site.get_all(session=session)
        return templates.TemplateResponse(request, "sites.html", {"data": sites})
    except Exception as e:
        logging.error(e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@api.get("/details/{rule_id}")
@databased
async def open_rule_details(request: Request, rule_id: str, session=None):
    try:
        req = DBRequest.get_by_id(rule_id, session=session, use_lock=False)
        return templates.TemplateResponse(request, "details.html", {"data": req})
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
        req.update({"rucio_finished_at": utcnow()}, session=session)
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
async def get_logs():
    log_path = "dmm.log"
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="Log file not found")
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()
        return PlainTextResponse("".join(lines[-2000:]))
    except Exception as e:
        logging.error(e)
        raise HTTPException(status_code=500, detail="Failed to read log file")

@api.get("/health/live")
async def liveness_check():
    """
    Is this process able to serve? Nothing more. The daemons run in the parent
    process, so if that one dies the container goes with it and this endpoint
    stops answering anyway — checking them here would restart the pod, and its
    in-flight circuits, for a single slow daemon.
    """
    return {"status": "alive"}


@api.get("/health")
async def health_check():
    site_count, database_error = None, None
    # Deliberately not @databased: that decorator commits on the way out and
    # re-raises, which would turn an unreachable database into a 500 instead of
    # the 503 a probe can act on. An unreachable database is a health answer,
    # not a health failure.
    try:
        with get_session() as session:
            site_count = Site.count(session=session)
    except Exception as e:
        logging.error(f"health check could not reach the database: {e}", exc_info=True)
        database_error = e

    healthy, body = health_report(site_count=site_count, database_error=database_error)
    return JSONResponse(content=body, status_code=200 if healthy else 503)