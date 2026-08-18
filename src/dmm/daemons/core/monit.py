import requests
import re
import logging

from dmm.daemons.base import DaemonBase
from datetime import datetime

from dmm.models.request import Request, RequestStatus
from dmm.db.session import databased

from dmm.core.config import config_get

class MonitDaemon(DaemonBase):
    def __init__(self, frequency, **kwargs):
        super().__init__(frequency, **kwargs)
        self.prometheus_user = config_get("prometheus", "user")
        self.prometheus_pass = config_get("prometheus", "password")
        self.prometheus_host = config_get("prometheus", "host")

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

                bytes_now = self.get_all_bytes_at_t(current_timestamp, req.src_endpoint.ip_range)

                # A scrape gap must not corrupt the byte counter or flip
                # health: when the lookup failed, leave the previous
                # prometheus_bytes and health untouched and skip this cycle.
                if bytes_now is None:
                    logging.warning(f"Request {req.rule_id}: prometheus scrape failed, skipping cycle")
                    continue

                if req.prometheus_bytes is None:
                    req.set_prometheus_metrics(bytes_transferred=bytes_now, session=session)
                    continue

                throughput = self.calculate_throughput(bytes_now, req.prometheus_bytes)
                req.set_prometheus_metrics(throughput=throughput, bytes_transferred=bytes_now, session=session)

                health_status = self.determine_health_status(throughput, req.allocated_bandwidth_mbps)
                req.set_health(health_status, session=session)
                
            except Exception as e:
                logging.error(f"Error monitoring request {req.rule_id}: {e}", exc_info=True)
                continue

    def calculate_throughput(self, bytes_now, prometheus_bytes):
        if self.frequency <= 0:
            logging.error("Monitoring frequency is 0 or negative, cannot calculate throughput")
            return 0.0
        # Calculate throughput in Gbps
        byte_diff = bytes_now - prometheus_bytes
        if byte_diff < 0:
            logging.warning(f"Negative byte difference detected: {byte_diff}, resetting to 0")
            byte_diff = 0
        return round(byte_diff / self.frequency / 1024 / 1024 / 1024 * 8, 2)

    def determine_health_status(self, throughput, bandwidth):
        if bandwidth is None or bandwidth <= 0:
            return "0"  # Unknown/unhealthy if no bandwidth allocated
        # Throughput should be at least 80% of allocated bandwidth
        return "1" if throughput >= 0.8 * bandwidth else "0"

    def submit_query(self, query_dict) -> dict:
        endpoint = "api/v1/query"
        query_addr = f"{self.prometheus_host}/{endpoint}"
        try:
            response = requests.get(
                query_addr,
                params=query_dict,
                auth=(self.prometheus_user, self.prometheus_pass),
                timeout=15,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logging.error(f"Prometheus query request failed for {query_addr}: {e}", exc_info=True)
            raise
        except ValueError as e:
            logging.error(f"Prometheus query returned invalid JSON for {query_addr}: {e}", exc_info=True)
            raise
    
    @staticmethod
    def get_val_from_response(response):
        try:
            return response["data"]["result"][0]["value"][1]
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(f"Invalid Prometheus response format: {response}") from e
                
    def get_interfaces(self, ipv6) -> str:
        ipv6_pattern = f"{ipv6[:-3]}[0-9a-fA-F]{{1,4}}"
        query = f"node_network_address_info{{address=~'{ipv6_pattern}'}}"
        response = self.submit_query({"query": query})

        interfaces = []
        if response.get("status") == "success":
            for metric in response.get("data", {}).get("result", []):
                metric_data = metric.get("metric", {}) if isinstance(metric, dict) else {}
                address = metric_data.get("address")
                if address and re.match(ipv6_pattern, address):
                    interfaces.append(
                        (
                            metric_data.get("device"),
                            metric_data.get("instance"),
                            metric_data.get("job"),
                            metric_data.get("sitename"),
                        )
                    )
        return interfaces

    def get_all_bytes_at_t(self, time, ipv6) -> float | None:
        """
        Returns the total number of bytes transmitted from a given Rucio RSE via
        a given ipv6 address at the given time.

        Returns None when the scrape failed (no interface found, or every
        per-interface query returned no data) so callers can distinguish a scrape
        gap from a genuine zero. A partial scrape (some interfaces succeed,
        others fail) still returns a number.
        """
        transfers = self.get_interfaces(ipv6)
        if not transfers:
            logging.warning(f"No interfaces found for IPv6 {ipv6}")
            return None

        total_bytes = 0.0
        scraped = 0
        for transfer in transfers:
            try:
                device, instance, job, sitename = transfer[0], transfer[1], transfer[2], transfer[3]
                query_params = f"device=\"{device}\",instance=\"{instance}\",job=\"{job}\",sitename=\"{sitename}\""
                metric = f"node_network_transmit_bytes_total{{{query_params}}}"

                response = self.submit_query({"query": metric, "time": time})
                if response.get("status") == "success" and response.get("data", {}).get("result"):
                    bytes_at_t = self.get_val_from_response(response)
                    total_bytes += float(bytes_at_t)
                    scraped += 1
                else:
                    logging.warning(f"Query {metric} returned no data")
            except Exception as e:
                logging.error(f"Error querying bytes for interface {device}: {e}", exc_info=True)
                continue

        if scraped == 0:
            logging.warning(f"All per-interface byte queries failed for IPv6 {ipv6}")
            return None
        return total_bytes