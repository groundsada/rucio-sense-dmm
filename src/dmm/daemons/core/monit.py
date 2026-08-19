import logging
from datetime import datetime

from dmm.daemons.base import DaemonBase
from dmm.models.request import Request, RequestStatus
from dmm.db.session import databased
from dmm.core.monit import PrometheusUtils

# Mirrored by dmm_request_throughput_ratio, so alert rules can use their own
# threshold without redeploying DMM.
HEALTH_THROUGHPUT_RATIO = 0.8


class MonitDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        self.prometheus = PrometheusUtils()

    def process(self, **kwargs):
        self.run_once(**kwargs)

    @databased
    def run_once(self, session=None):
        reqs = Request.get_by_status(statuses=[RequestStatus.PROVISIONED], session=session)
        current_timestamp = round(datetime.timestamp(datetime.now()))

        for req in reqs:
            try:
                if req.sense_provisioned_at is None:
                    logging.debug(f"Request {req.rule_id} has no provisioned_at timestamp, skipping")
                    continue

                if not req.src_endpoint or not req.src_endpoint.ip_range:
                    logging.warning(f"Request {req.rule_id} has no source endpoint, skipping monitoring")
                    continue

                bytes_now = self._get_all_bytes_at_t(current_timestamp, req.src_endpoint.ip_range)

                if req.prometheus_bytes is None:
                    req.set_prometheus_metrics(bytes_transferred=bytes_now, session=session)
                    continue

                throughput_mbps = self._calculate_throughput_mbps(bytes_now, req.prometheus_bytes)
                req.set_prometheus_metrics(throughput=throughput_mbps, bytes_transferred=bytes_now, session=session)

                health_status = self._determine_health_status(throughput_mbps, req.allocated_bandwidth_mbps)
                req.set_health(health_status, session=session)

            except Exception as e:
                logging.error(f"Error monitoring request {req.rule_id}: {e}", exc_info=True)
                continue

    def _get_all_bytes_at_t(self, time, ipv6) -> float:
        transfers = self.prometheus.get_interfaces(ipv6)
        if not transfers:
            logging.warning(f"No interfaces found for IPv6 {ipv6}")
            return 0.0

        total_bytes = 0.0
        for transfer in transfers:
            try:
                device, instance, job, sitename = transfer[0], transfer[1], transfer[2], transfer[3]
                query_params = f'device="{device}",instance="{instance}",job="{job}",sitename="{sitename}"'
                metric = f"node_network_transmit_bytes_total{{{query_params}}}"
                response = self.prometheus.submit_query({"query": metric, "time": time})
                if response.get("status") == "success" and response.get("data", {}).get("result"):
                    bytes_at_t = PrometheusUtils.get_val_from_response(response)
                    total_bytes += float(bytes_at_t)
                else:
                    logging.warning(f"Query {metric} returned no data")
            except Exception as e:
                logging.error(f"Error querying bytes for interface {transfer[0]}: {e}", exc_info=True)
                continue

        return total_bytes

    def _calculate_throughput_mbps(self, bytes_now, prometheus_bytes) -> float:
        if self.frequency <= 0:
            logging.error("Monitoring frequency is 0 or negative, cannot calculate throughput")
            return 0.0
        byte_diff = bytes_now - prometheus_bytes
        if byte_diff < 0:
            logging.warning(f"Negative byte difference detected: {byte_diff}, resetting to 0")
            byte_diff = 0
        # Convert bytes/s → Mbps. SENSE bandwidth is decimal, so 1000 not 1024.
        return round(byte_diff / self.frequency / 1000 / 1000 * 8, 2)

    @staticmethod
    def _determine_health_status(throughput_mbps, bandwidth_mbps) -> str:
        if bandwidth_mbps is None or bandwidth_mbps <= 0:
            return "0"
        return "1" if throughput_mbps >= HEALTH_THROUGHPUT_RATIO * bandwidth_mbps else "0"
