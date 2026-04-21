import logging
from datetime import datetime

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request, RequestStatus

from dmm.core.config import config_get_int
from dmm.core.sense import (
    cancel_link,
    is_cancel_ready,
    is_create_compiled,
    is_ready_for_cancel
)
from dmm.core.utils import release_endpoints_and_addresses

class SENSECancellerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
    
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_finished = Request.get_by_status(statuses=[RequestStatus.FINISHED], session=session)
        if reqs_finished == []:
            return

        # Guard: build the set of sense_uuids actively held by live requests.
        # A FINISHED request whose UUID is still referenced by a PROVISIONED request
        # means the circuit was reused — do not cancel or free its endpoints.
        live_reqs = Request.get_by_status(statuses=[RequestStatus.PROVISIONED, RequestStatus.STALE, RequestStatus.DECIDED, RequestStatus.STAGED], session=session)
        live_uuids = {r.sense_uuid for r in live_reqs if r.sense_uuid}
            
        for req in reqs_finished:
            try:
                # Safety check: don't cancel a circuit that's been taken over by another request
                if req.sense_uuid and req.sense_uuid in live_uuids:
                    logging.warning(
                        f"Request {req.rule_id} is FINISHED but its circuit {req.sense_uuid} is still "
                        f"in use by another live request — skipping cancellation, marking as DELETED"
                    )
                    req.set_status(status=RequestStatus.DELETED, session=session)
                    continue

                if req.sense_uuid is None:
                    logging.debug(f"Request {req.rule_id} has no SENSE UUID, marking endpoints as free")
                    release_endpoints_and_addresses(req, session)
                    req.set_status(status=RequestStatus.CANCELED, session=session)
                    continue
                    
                if req.rucio_finished_at is None:
                    logging.warning(
                        f"Request {req.rule_id} has no rucio_finished_at timestamp; "
                        "using immediate cancellation path"
                    )
                    time_since_update = config_get_int("sense", "sense_keep_alive_seconds", default=60, constraint="nonneg")
                else:
                    time_since_update = (datetime.now() - req.rucio_finished_at).total_seconds()
                if time_since_update < config_get_int("sense", "sense_keep_alive_seconds", default=60, constraint="nonneg"):
                    logging.debug(f"Request {req.rule_id} updated {time_since_update:.0f}s ago, waiting before cancellation")
                    continue

                logging.info(f"Cancelling SENSE link with uuid {req.sense_uuid} for request {req.rule_id}")
                status = req.sense_circuit_status
                
                if is_cancel_ready(status):
                    logging.debug(f"Request {req.sense_uuid} already in cancel-ready status, marking as canceled")
                    release_endpoints_and_addresses(req, session)
                    req.set_status(status=RequestStatus.CANCELED, session=session)
                    continue
                    
                if is_create_compiled(status):
                    logging.debug(f"Request {req.sense_uuid} in compiled status, safe to mark as canceled without cancellation")
                    release_endpoints_and_addresses(req, session)
                    req.set_status(status=RequestStatus.CANCELED, session=session)
                    continue
                    
                if not is_ready_for_cancel(status):
                    logging.debug(f"Cannot cancel instance {req.sense_uuid} in status '{status}', will try again later")
                    continue
                    
                cancel_link(req.sense_uuid, status)
                
                release_endpoints_and_addresses(req, session)
                req.set_status(status=RequestStatus.CANCELED, session=session)
                logging.info(f"Successfully cancelled SENSE link for request {req.rule_id}")
                
            except Exception as e:
                logging.error(f"Failed to cancel link for {req.rule_id}: {e}", exc_info=True)
