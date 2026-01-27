import logging
import re
import json

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request
from dmm.models.site import Site
from dmm.models.mesh import Mesh

from dmm.core.config import config_get

from sense.client.workflow_combined_api import WorkflowCombinedApi

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
            if req_.sense_circuit_status and re.match(r"(MODIFY) - (COMMITTING|COMMITTED)", req_.sense_circuit_status):
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
                if not status or not re.match(r"(CREATE|MODIFY|REINSTATE) - READY$", status):
                    logging.debug(f"Cannot modify request {req.rule_id} in status '{status}', will try again later")
                    continue
                    
                vlan_range = Mesh.get_vlan_range(site_1=req.src_site, site_2=req.dst_site, session=session)
                if not vlan_range:
                    logging.error(f"No VLAN range found for {req.rule_id}")
                    continue
                    
                response = self._modify_request(req, vlan_range, session=session)
                
                req.set_status(status="PROVISIONED", session=session)
                logging.info(f"Successfully modified request {req.rule_id}")
                
            except Exception as e:
                logging.error(f"Failed to modify link for {req.rule_id}: {e}", exc_info=True)

    def _modify_request(self, req, vlan_range, session=None):
        try:
            workflow_api = WorkflowCombinedApi()
            workflow_api.si_uuid = req.sense_uuid
            intent = {
                "service_profile_uuid": self.profile_uuid,
                "queries": [
                    {
                        "ask": "edit",
                        "options": [
                            {"data.connections[0].bandwidth.capacity": str(int(req.allocated_bandwidth_mbps))},
                            {"data.connections[0].terminals[0].uri": Site.get_by_name(name=req.src_site.name, session=session, use_lock=False).sense_uri},
                            {"data.connections[0].terminals[0].ipv6_prefix_list": req.src_endpoint.ip_range},
                            {"data.connections[0].terminals[1].uri": Site.get_by_name(name=req.dst_site.name, session=session, use_lock=False).sense_uri},
                            {"data.connections[0].terminals[1].ipv6_prefix_list": req.dst_endpoint.ip_range},
                            {"data.connections[0].terminals[0].vlan_tag": vlan_range},
                            {"data.connections[0].terminals[1].vlan_tag": vlan_range}
                        ]
                    }
                ],
                "alias": req.rule_id
            }
            response = workflow_api.instance_modify(json.dumps(intent), sync="true")
            return response
        except Exception as e:
            logging.error(f"Failed to modify request {req.rule_id}: {e}")
            raise e
            