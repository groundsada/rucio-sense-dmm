import logging
from datetime import datetime

from dmm.core.config import config_get_int
from dmm.daemons.base import DaemonBase
from dmm.models.request import Request
from dmm.db.session import databased

from dmm.core.sense import (
    get_instance_status,
    affiliate_endpoints,
    is_affiliated_state,
    is_create_ready
)

class SENSEHandlerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs = Request.get_by_status(statuses=["STAGED", "PROVISIONED", "CANCELED", "STALE", "DECIDED", "FINISHED"], session=session)
        if not reqs:
            return
        
        for req in reqs:
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

            # TODO: if sense creation fails, should retry
            # at staging step, i.e. before create - committed: 
                # reasons could be failed vlan tag regex, in that case, maybe retry with default vlan tag - mark as allocated and let stager run again
            # at provisioned step, i.e. after create - committed:
                # should put in allocated state, so vlan allocation can be retried
            # at finished step, i.e. after cancel - committed: should force retry
