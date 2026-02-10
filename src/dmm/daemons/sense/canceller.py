import logging
from datetime import datetime

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request

from dmm.core.config import config_get_int
from dmm.core.sense import (
    cancel_link,
    is_cancel_ready,
    is_create_compiled,
    is_ready_for_cancel
)

class SENSECancellerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
    
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_finished = Request.get_by_status(statuses=["FINISHED"], session=session)
        if reqs_finished == []:
            return
            
        for req in reqs_finished:
            try:
                if req.sense_uuid is None:
                    logging.debug(f"Request {req.rule_id} has no SENSE UUID, marking endpoints as free")
                    if req.src_endpoint:
                        req.src_endpoint.set_allocated(is_allocated=False, session=session)
                    if req.dst_endpoint:
                        req.dst_endpoint.set_allocated(is_allocated=False, session=session)
                    req.set_status(status="CANCELED", session=session)
                    continue
                    
                time_since_update = (datetime.now() - req.rucio_finished_at).total_seconds()
                if time_since_update < config_get_int("sense", "sense_keep_alive_seconds", default=60, constraint="nonneg"):
                    logging.debug(f"Request {req.rule_id} updated {time_since_update:.0f}s ago, waiting before cancellation")
                    continue

                logging.info(f"Cancelling SENSE link with uuid {req.sense_uuid} for request {req.rule_id}")
                status = req.sense_circuit_status
                
                if is_cancel_ready(status):
                    logging.debug(f"Request {req.sense_uuid} already in cancel-ready status, marking as canceled")
                    req.src_endpoint.set_allocated(is_allocated=False, session=session)
                    req.dst_endpoint.set_allocated(is_allocated=False, session=session)
                    req.set_status(status="CANCELED", session=session)
                    continue
                    
                if is_create_compiled(status):
                    logging.debug(f"Request {req.sense_uuid} in compiled status, safe to mark as canceled without cancellation")
                    req.src_endpoint.set_allocated(is_allocated=False, session=session)
                    req.dst_endpoint.set_allocated(is_allocated=False, session=session)
                    req.set_status(status="CANCELED", session=session)
                    continue
                    
                if not is_ready_for_cancel(status):
                    logging.debug(f"Cannot cancel instance {req.sense_uuid} in status '{status}', will try again later")
                    continue
                    
                cancel_link(req.sense_uuid, status)
                
                req.src_endpoint.set_allocated(is_allocated=False, session=session)
                req.dst_endpoint.set_allocated(is_allocated=False, session=session)
                req.set_status(status="CANCELED", session=session)
                logging.info(f"Successfully cancelled SENSE link for request {req.rule_id}")
                
            except Exception as e:
                logging.error(f"Failed to cancel link for {req.rule_id}: {e}", exc_info=True)
