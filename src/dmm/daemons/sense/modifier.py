import logging

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request
from dmm.models.mesh import Mesh

from dmm.core.config import config_get
from dmm.core.sense import modify_link, is_ready_for_modify, is_being_modified

class SENSEModifierDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        self.profile_uuid = config_get("sense", "profile_uuid")
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_stale = Request.get_by_status(statuses=["STALE"], session=session)
        if reqs_stale == []:
            return
        
        # Sort requests by previous bandwidth to prioritize modifications which reduce bandwidth
        # This ensures that we make bandwidth available for other requests
        reqs_stale = sorted(reqs_stale, key=lambda x: (x.allocated_bandwidth_mbps or 0) - (x.previous_bandwidth_mbps or 0))

        # Track which requests are currently being modified (not just if ANY modification is in progress)
        # Build a set of rule_ids that are currently in a modifying state
        all_reqs = Request.get_by_status(statuses=["STALE", "PROVISIONED"], session=session)
        modifying_rule_ids = set()
        for req_ in all_reqs:
            if is_being_modified(req_.sense_circuit_status):
                modifying_rule_ids.add(req_.rule_id)
        
        # Only skip requests that are already being modified, allow others to proceed
        if modifying_rule_ids:
            logging.debug(f"Requests currently being modified: {modifying_rule_ids}")

        for req in reqs_stale:
            # Skip this specific request if it's already being modified
            if req.rule_id in modifying_rule_ids:
                logging.debug(f"Request {req.rule_id} is already being modified, skipping")
                continue
                
            if req.sense_uuid is None:
                logging.warning(f"Request {req.rule_id} has no SENSE UUID, skipping modification")
                continue
                
            try:
                status = req.sense_circuit_status
                if not is_ready_for_modify(status):
                    logging.debug(f"Cannot modify request {req.rule_id} in status '{status}', will try again later")
                    continue
                    
                vlan_range = Mesh.get_vlan_range(site_1=req.src_site, site_2=req.dst_site, session=session)
                if not vlan_range:
                    logging.error(f"No VLAN range found for {req.rule_id}")
                    continue
                    
                modify_link(
                    sense_uuid=req.sense_uuid,
                    profile_uuid=self.profile_uuid,
                    bandwidth_mbps=req.allocated_bandwidth_mbps,
                    src_site=req.src_site,
                    dst_site=req.dst_site,
                    src_ip_range=req.src_endpoint.ip_range,
                    dst_ip_range=req.dst_endpoint.ip_range,
                    vlan_range=vlan_range,
                    rule_id=req.rule_id
                )
                
                req.set_status(status="PROVISIONED", session=session)
                logging.info(f"Successfully modified request {req.rule_id}")
                
            except Exception as e:
                logging.error(f"Failed to modify link for {req.rule_id}: {e}", exc_info=True)
            