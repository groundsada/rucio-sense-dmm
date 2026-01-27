import logging
import json
import re

from dmm.daemons.base import DaemonBase
from dmm.db.session import databased

from dmm.models.request import Request
from dmm.models.site import Site
from dmm.models.mesh import Mesh

from dmm.core.config import config_get

from sense.client.workflow_combined_api import WorkflowCombinedApi

class SENSEProvisionerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)

        self.profile_uuid = config_get("sense", "profile_uuid")
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        # make sure there are no stale requests before provisioning any new ones
        reqs_stale = Request.get_by_status(statuses=["STALE"], session=session)
        if reqs_stale != []:
            logging.debug("Stale requests exist, skipping provisioning")
            return
            
        reqs_decided = Request.get_by_status(statuses=["DECIDED"], session=session)
        if reqs_decided == []:
            return
            
        # Check which specific requests have provisioning in progress
        # Build a set of rule_ids that are currently being provisioned
        all_reqs = Request.get_by_status(statuses=["DECIDED", "PROVISIONED"], session=session)
        provisioning_rule_ids = set()
        for req_ in all_reqs:
            if req_.sense_circuit_status and re.match(r"(CREATE|MODIFY) - (COMMITTING|COMMITTED)", req_.sense_circuit_status):
                provisioning_rule_ids.add(req_.rule_id)
        
        # Only skip requests that are already being provisioned, allow others to proceed        
        if provisioning_rule_ids:
            logging.debug(f"Requests currently being provisioned: {provisioning_rule_ids}")
                
        for req in reqs_decided:
            # Skip this specific request if it's already being provisioned
            if req.rule_id in provisioning_rule_ids:
                logging.debug(f"Request {req.rule_id} is already being provisioned, skipping")
                continue
                
            if req.sense_uuid is None:
                logging.warning(f"Request {req.rule_id} has no SENSE UUID, skipping")
                continue
                
            try:
                status = req.sense_circuit_status
                if status and re.match(r"(CREATE) - READY$", status):
                    logging.debug(f"Request {req.sense_uuid} already in ready status, marking as provisioned")
                    req.set_status(status="PROVISIONED", session=session)
                    continue
                    
                if not status or not re.match(r"(CREATE) - COMPILED$", status):
                    logging.debug(f"Request {req.sense_uuid} not in compiled status (current: {status}), will try to provision again")
                    continue
                    
                vlan_range = Mesh.get_vlan_range(site_1=req.src_site, site_2=req.dst_site, session=session)
                if not vlan_range:
                    logging.error(f"No VLAN range found for {req.rule_id}")
                    continue
                    
                response = self._provision_request(req, vlan_range, session=session)
                req.set_status(status="PROVISIONED", session=session)
                logging.info(f"Successfully provisioned request {req.rule_id}")
                
            except Exception as e:
                logging.error(f"Failed to provision link for {req.rule_id}: {e}", exc_info=True)
    
    @databased
    def _provision_request(self, req, vlan_range, session=None):
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
            response = workflow_api.instance_create(json.dumps(intent))
            if not self._good_response(response):
                raise ValueError(f"Failed to create instance for request {req.rule_id}, response: {response}")
            workflow_api.instance_operate("provision", sync="true")
            return response
        except Exception as e:
            logging.error(f"Failed to provision request {req.rule_id}: {e}")
            raise
    @staticmethod
    def _good_response(response):
        return bool(response and not any("error" in r for r in response))