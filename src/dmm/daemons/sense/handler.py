import logging
from datetime import datetime

from dmm.core.config import config_get_int
from dmm.daemons.base import DaemonBase
from dmm.models.request import Request, RequestStatus
from dmm.db.session import databased

from dmm.core.sense import (
    get_instance_status,
    delete_instance,
    affiliate_endpoints,
    is_affiliated_state,
    is_create_ready,
    is_create_failed
)
from dmm.core.utils import release_endpoints_and_addresses

class SENSEHandlerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @staticmethod
    def _retry_target_status(req) -> RequestStatus:
        """
        Decide where to resume after RETRY.
        - If a SENSE instance already exists, resume from DECIDED (re-provision path)
        - Otherwise resume from ALLOCATED (re-stage path)
        """
        return RequestStatus.DECIDED if req.sense_uuid else RequestStatus.ALLOCATED

    @databased
    def run_once(self, session=None):
        reqs = Request.get_by_status(statuses=[RequestStatus.RETRY, RequestStatus.STAGED, RequestStatus.PROVISIONED, RequestStatus.CANCELED, RequestStatus.STALE, RequestStatus.DECIDED], session=session)
        if not reqs:
            return
        
        for req in reqs:
            if req.transfer_status == RequestStatus.RETRY:
                if req.sense_retries < config_get_int("sense", "max_retries", default=3):
                    logging.info(f"Request {req.rule_id} has {req.sense_retries} retries, less than max retries. Retrying.")
                    req.increment_sense_retries(session=session)
                    target_status = self._retry_target_status(req)
                    req.set_status(target_status, session=session)
                else:
                    logging.warning(f"Request {req.rule_id} has reached max SENSE retries. Marking as failed.")
                    req.set_status(RequestStatus.FAILED, session=session)
                    release_endpoints_and_addresses(req, session)
            
            if req.sense_uuid is None:
                continue

            status = get_instance_status(req.sense_uuid)
            req.set_sense_circuit_status(status=status, session=session)

            # Affiliate endpoints when ready
            if not req.sense_affiliated and is_affiliated_state(status):
                logging.debug(f"Request {req.rule_id} is not affiliated with SENSE instance {req.sense_uuid}, affiliating now.")
                affiliate_endpoints(
                    sense_uuid=req.sense_uuid,
                    src_site_name=req.src_site.name,
                    dst_site_name=req.dst_site.name,
                    src_ip_range=req.src_endpoint.ip_range,
                    dst_ip_range=req.dst_endpoint.ip_range,
                    sense_src_uri=req.sense_src_uri,
                    sense_dst_uri=req.sense_dst_uri
                )
                req.update({"sense_affiliated": True}, session=session)

            # Update sense_provisioned_at if the status is READY for monitoring
            if not req.sense_provisioned_at and is_create_ready(status):
                logging.debug(f"Request {req.rule_id} is ready, updating sense_provisioned_at to current time.")
                req.update({"sense_provisioned_at": datetime.now()}, session=session)
            
                fts_limit = config_get_int("fts-streams", f"{req.src_site.name}-{req.dst_site.name}", default=200)
                req.set_fts_streams(desired=fts_limit, session=session)

            elif req.transfer_status in [RequestStatus.PROVISIONED] and is_create_failed(status):
                logging.warning(
                    f"Request {req.rule_id} reached CREATE_FAILED after being PROVISIONED; "
                    "marking as RETRY to re-enter SENSE retry flow"
                )
                req.set_status(RequestStatus.RETRY, session=session)
                
