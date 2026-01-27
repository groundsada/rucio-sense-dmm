import re
import logging
import json
import requests
import urllib

from dmm.models.request import Request
from dmm.db.session import databased

from dmm.core.config import config_get
from dmm.daemons.base import DaemonBase

class FTSModifierDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        self.fts_host = config_get("fts", "fts_host")
        self.cert = (config_get("fts", "cert"), config_get("fts", "key"))
        self.capath = "/etc/grid-security/certificates/"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        self._process_requests(session, ["ALLOCATED", "DECIDED", "PROVISIONED"], self._modify_request)
        self._process_requests(session, ["DELETED"], self._delete_request)

    def _process_requests(self, session, statuses, action):
        reqs = Request.get_by_status(statuses=statuses, session=session)
        if reqs:
            for req in reqs:
                action(req, session)

    def _modify_request(self, req, session):
        if req.fts_streams_current != req.fts_streams_desired:
            logging.debug(f"Modifying FTS limits for request {req.rule_id}, from {req.fts_streams_current} to {req.fts_streams_desired}")
            link_modified = self._modify_link_config(req, max_active=req.fts_streams_desired, min_active=req.fts_streams_desired)
            se_modified = self._modify_se_config(req, max_inbound=req.fts_streams_desired, max_outbound=req.fts_streams_desired)
            if link_modified and se_modified:
                req.set_fts_streams(current=req.fts_streams_desired, session=session)

    def _delete_request(self, req, session):
        if req.fts_streams_current != 0:
            logging.debug(f"Deleting FTS limits for request {req.rule_id}")
            self._delete_link_config(req)
            self._delete_se_config(req)
            req.set_fts_streams(current=0, session=session)

    def _modify_link_config(self, req, max_active, min_active):
        data = self._prepare_link_data(req, max_active, min_active)
        return self._send_request("/config/links", data)

    def _modify_se_config(self, req, max_inbound, max_outbound):
        data = self._prepare_se_data(req, max_inbound, max_outbound)
        return self._send_request("/config/se", data)

    def _delete_link_config(self, req):
        src_url_no_port, dst_url_no_port = self._get_endpoints(req)
        try:
            response_link = requests.delete(
                self.fts_host + "/config/links/" + urllib.parse.quote("-".join([src_url_no_port, dst_url_no_port]), safe=""),
                headers=self.headers, cert=self.cert, verify=self.capath
            )
            success = response_link.status_code in [200, 201, 204]
            if not success:
                logging.warning(f"FTS link deletion returned status {response_link.status_code}: {response_link.text}")
            return success
        except Exception as e:
            logging.error(f"Error while deleting FTS link configs: {e}", exc_info=True)
            return False
        
    def _delete_se_config(self, req):
        src_url_no_port, dst_url_no_port = self._get_endpoints(req)
        try:
            response_src = requests.delete(
                self.fts_host + "/config/se/" + urllib.parse.quote(src_url_no_port, safe=""),
                headers=self.headers, cert=self.cert, verify=self.capath
            )
            response_dst = requests.delete(
                self.fts_host + "/config/se/" + urllib.parse.quote(dst_url_no_port, safe=""),
                headers=self.headers, cert=self.cert, verify=self.capath
            )
            success_src = response_src.status_code in [200, 201, 204]
            success_dst = response_dst.status_code in [200, 201, 204]
            
            if not success_src:
                logging.warning(f"FTS SE deletion (src) returned status {response_src.status_code}: {response_src.text}")
            if not success_dst:
                logging.warning(f"FTS SE deletion (dst) returned status {response_dst.status_code}: {response_dst.text}")
                
            return success_src and success_dst
        except Exception as e:
            logging.error(f"Error while deleting FTS SE configs: {e}", exc_info=True)
            return False

    def _prepare_link_data(self, req, max_active, min_active):
        src_url_no_port, dst_url_no_port = self._get_endpoints(req)
        return json.dumps({
            "symbolicname": "-".join([src_url_no_port, dst_url_no_port]),
            "source": src_url_no_port,
            "destination": dst_url_no_port,
            "max_active": max_active,
            "min_active": min_active,
            "nostreams": 0,
            "optimizer_mode": 0,
            "no_delegation": False,
            "tcp_buffer_size": 0
        })

    def _prepare_se_data(self, req, max_inbound, max_outbound):
        src_url_no_port, dst_url_no_port = self._get_endpoints(req)
        return json.dumps({
            src_url_no_port: {
                "se_info": {
                    "inbound_max_active": None,
                    "inbound_max_throughput": None,
                    "outbound_max_active": max_outbound,
                    "outbound_max_throughput": None,
                    "udt": None,
                    "ipv6": None,
                    "se_metadata": None,
                    "site": None,
                    "debug_level": None,
                    "eviction": None
                }
            },
            dst_url_no_port: {
                "se_info": {
                    "inbound_max_active": max_inbound,
                    "inbound_max_throughput": None,
                    "outbound_max_active": None,
                    "outbound_max_throughput": None,
                    "udt": None,
                    "ipv6": None,
                    "se_metadata": None,
                    "site": None,
                    "debug_level": None,
                    "eviction": None
                }
            }
        })

    def _send_request(self, endpoint, data):
        try:
            response = requests.post(
                self.fts_host + endpoint, headers=self.headers, cert=self.cert, verify=self.capath, data=data
            )
            success = response.status_code in [200, 201]
            if success:
                logging.info(f"FTS config modified successfully for {endpoint}")
            else:
                logging.warning(f"FTS config modification returned status {response.status_code}: {response.text}")
            return success
        except Exception as e:
            logging.error(f"Error while modifying FTS config for {endpoint}: {e}", exc_info=True)
            return False

    def _get_endpoints(self, req):
        src_url_no_port = req.src_endpoint.protocol + "://" + req.src_endpoint.hostname.split(":")[0]
        dst_url_no_port = req.dst_endpoint.protocol + "://" + req.dst_endpoint.hostname.split(":")[0]
        return src_url_no_port, dst_url_no_port
