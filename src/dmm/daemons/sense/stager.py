import logging

from dmm.daemons.base import DaemonBase

from dmm.db.session import databased
from dmm.models.request import Request, RequestStatus
from dmm.models.mesh import Mesh

from dmm.core.config import config_get
from dmm.core.sense import stage_link, parse_staging_response

class SENSEStagerDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        self.profile_uuid = config_get("sense", "profile_uuid")
        
    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs_allocated = Request.get_by_status(statuses=[RequestStatus.ALLOCATED], session=session)
        if reqs_allocated == []:
            return
        for req in reqs_allocated:
            try:
                vlan_range = Mesh.get_vlan_range(site_1=req.src_site, site_2=req.dst_site, session=session)
                if vlan_range is None:
                    logging.error(f"No VLAN range found for {req.rule_id}, marking as FAILED")
                    req.set_status(status=RequestStatus.FAILED, session=session)
                    continue
                    
                response = stage_link(
                    profile_uuid=self.profile_uuid,
                    src_site=req.src_site,
                    dst_site=req.dst_site,
                    src_ip_range=req.src_endpoint.ip_range,
                    dst_ip_range=req.dst_endpoint.ip_range,
                    vlan_range=vlan_range,
                    rule_id=req.rule_id
                )
                
                if not response:
                    logging.error(f"Staging returned empty response for {req.rule_id}")
                    continue
                    
                logging.debug(f"Staging returned response {response}")
                
                sense_uuid, bandwidth_mbps, src_uri, dst_uri = parse_staging_response(response)
                
                if not sense_uuid:
                    raise ValueError("No service_uuid in response")
                
                req.set_sense_uuid(sense_uuid, session=session)
                req.set_sense_uris(src_uri, dst_uri, session=session)
                req.set_available_bandwidth(bandwidth_mbps, session=session)
                req.set_status(status=RequestStatus.STAGED, session=session)
                
            except Exception as e:
                logging.error(f"Failed to stage link for {req.rule_id}, {e}, will try again")
                req.set_status(status=RequestStatus.RETRY, session=session)