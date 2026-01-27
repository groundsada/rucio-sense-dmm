import logging
import json

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request

from dmm.core.config import config_get

from dmm.models.site import Site
from dmm.models.mesh import Mesh

from sense.client.workflow_combined_api import WorkflowCombinedApi

class SENSEStagerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        self.profile_uuid = config_get("sense", "profile_uuid")
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_allocated = Request.get_by_status(statuses=["ALLOCATED"], session=session)
        if reqs_allocated == []:
            return
        for req in reqs_allocated:
            sense_uuid = None
            try:
                vlan_range = Mesh.get_vlan_range(site_1=req.src_site, site_2=req.dst_site, session=session)
                if vlan_range is None:
                    logging.error(f"No VLAN range found for {req.rule_id}, marking as FAILED")
                    req.set_status(status="FAILED", session=session)
                    continue
                    
                response = self._stage_request(req, vlan_range, session=session)
                if not response:
                    logging.error(f"Staging returned empty response for {req.rule_id}")
                    continue
                    
                logging.debug(f"Staging returned response {response}")
                sense_uuid = response.get("service_uuid")
                
                if not sense_uuid:
                    raise ValueError("No service_uuid in response")
                
                req.set_sense_uuid(sense_uuid, session=session)
                
                # Extract available bandwidth from SENSE response
                # SENSE returns bandwidth in Mbps, store it as-is
                # The system uses Mbps throughout (decider rounds to 1000 Mbps increments)
                bandwidth_mbps = int(response.get("queries", [{}])[1].get("results", [{}])[0].get("bandwidth", 0))
                available_bandwidth_mbps = bandwidth_mbps  # Store in Mbps for consistency
                
                src_uri = response.get("queries")[2].get("results")[0].get("ipv6_subnet_uri")
                dst_uri = response.get("queries")[2].get("results")[1].get("ipv6_subnet_uri")
                req.set_sense_uris(src_uri, dst_uri, session=session)
                req.set_available_bandwidth(available_bandwidth_mbps, session=session)
                req.set_status(status="STAGED", session=session)
                
            except Exception as e:
                logging.error(f"Failed to stage link for {req.rule_id}, {e}, will try again")
    
    @databased
    def _stage_request(self, req, vlan_range, session=None):
        try:
            workflow_api = WorkflowCombinedApi()
            workflow_api.instance_new()
            intent = {
                "service_profile_uuid": self.profile_uuid,
                "queries": [
                    {
                        "ask": "edit",
                        "options": [
                            {"data.connections[0].terminals[0].uri": Site.get_by_name(name=req.src_site.name, session=session, use_lock=False).sense_uri},
                            {"data.connections[0].terminals[0].ipv6_prefix_list": req.src_endpoint.ip_range},
                            {"data.connections[0].terminals[1].uri": Site.get_by_name(name=req.dst_site.name, session=session, use_lock=False).sense_uri},
                            {"data.connections[0].terminals[1].ipv6_prefix_list": req.dst_endpoint.ip_range},
                            {"data.connections[0].terminals[0].vlan_tag": vlan_range},
                            {"data.connections[0].terminals[1].vlan_tag": vlan_range}
                        ]
                    }, {
                    "ask": "total-block-maximum-bandwidth",
                    "options": [
                        {
                            "name": "Connection 1",
                            "start": "now",
                            "end-before": "+24h"
                        }
                    ]
                    },
                    {
                    "ask": "extract-result-values",
                    "options": [
                        {
                            "sparql": "SELECT DISTINCT ?ipv6_subnet_uri ?ipv6_subnet WHERE {?route mrs:routeFrom ?ipv6_subnet_uri. ?ipv6_subnet_uri mrs:type 'ipv6-prefix-list'. ?ipv6_subnet_uri mrs:value ?ipv6_subnet}"
                        }
                    ]
                    }
                ],
                "alias": req.rule_id
            }
            response = workflow_api.instance_create(json.dumps(intent))
            if not self._good_response(response):
                workflow_api.instance_delete(si_uuid=response["service_uuid"])
                raise ValueError(f"SENSE req staging failed for {req.rule_id}")
            return response
        except Exception as e:
            logging.error(f"Failed to stage link for {req.rule_id}, {e}, will try again")
            return None

    @staticmethod
    def _good_response(response):
        return bool(response and not any("ERROR" in r for r in response))