import logging

from prometheus_client import REGISTRY, CollectorRegistry, generate_latest, start_http_server
from prometheus_client.core import GaugeMetricFamily

from dmm.db.session import get_session
from dmm.models.request import Request as DBRequest

registry = REGISTRY

REQUEST_LABELS = ["rule_id", "transfer_status", "sense_circuit_status", "src_site", "dst_site"]

FAILURE_REASON_MAX_LEN = 256

REQUEST_GAUGES = (
    ("dmm_request_sense_retries", "Number of SENSE retries for request",
     lambda r: r.sense_retries if r.sense_retries is not None else 0),
    ("dmm_request_allocated_bandwidth_mbps", "Allocated bandwidth in Mbps",
     lambda r: r.allocated_bandwidth_mbps),
    ("dmm_request_available_bandwidth_mbps", "Available bandwidth in Mbps",
     lambda r: r.available_bandwidth_mbps),
    ("dmm_request_previous_bandwidth_mbps", "Previous bandwidth in Mbps",
     lambda r: r.previous_bandwidth_mbps),
    ("dmm_request_fts_streams_current", "Current FTS streams",
     lambda r: r.fts_streams_current),
    ("dmm_request_fts_streams_desired", "Desired FTS streams",
     lambda r: r.fts_streams_desired),
    ("dmm_request_prometheus_throughput_gbps", "Measured throughput in Gbps",
     lambda r: r.prometheus_throughput),
    ("dmm_request_prometheus_bytes", "Measured bytes from prometheus polling",
     lambda r: r.prometheus_bytes),
)


class RequestCollector:
    """
    Derives the per-request series from the database on each scrape. Registered
    in the daemon process; a failure here yields no samples rather than failing
    the whole scrape.
    """

    def collect(self):
        try:
            with get_session() as session:
                return self._families(DBRequest.get_all(session=session))
        except Exception as e:
            logging.error(f"Failed to collect request metrics: {e}", exc_info=True)
            return []

    def _families(self, reqs):
        total = GaugeMetricFamily(
            "dmm_requests_total", "Total number of requests tracked by DMM")
        total.add_metric([], len(reqs))

        counts = {}
        for req in reqs:
            status = str(req.transfer_status or "UNKNOWN")
            counts[status] = counts.get(status, 0) + 1

        by_status = GaugeMetricFamily(
            "dmm_requests_by_status", "Number of requests by transfer status", labels=["status"])
        for status in sorted(counts):
            by_status.add_metric([status], counts[status])

        info = GaugeMetricFamily(
            "dmm_request_info", "Request state marker (always 1) with core identifying labels",
            labels=REQUEST_LABELS + ["src_rse", "dst_rse", "failure_reason"])
        gauges = {
            name: GaugeMetricFamily(name, doc, labels=REQUEST_LABELS)
            for name, doc, _ in REQUEST_GAUGES
        }
        health = GaugeMetricFamily(
            "dmm_request_health", "Health status (1=healthy, 0=unhealthy, absent=unknown)",
            labels=REQUEST_LABELS)

        for req in reqs:
            src_site = req.src_site.name if req.src_site else "UNKNOWN"
            dst_site = req.dst_site.name if req.dst_site else "UNKNOWN"
            labels = [
                req.rule_id,
                str(req.transfer_status or "UNKNOWN"),
                str(req.sense_circuit_status or "UNKNOWN"),
                src_site,
                dst_site,
            ]

            reason = req.failure_reason or ""
            if len(reason) > FAILURE_REASON_MAX_LEN:
                reason = reason[:FAILURE_REASON_MAX_LEN - 3] + "..."
            info.add_metric(
                labels + [req.src_logical_site or src_site, req.dst_logical_site or dst_site, reason], 1)

            for name, _, value_of in REQUEST_GAUGES:
                value = value_of(req)
                if value is not None:
                    gauges[name].add_metric(labels, value)

            if str(req.health) in ("0", "1"):
                health.add_metric(labels, int(req.health))

        return [total, by_status, info, *gauges.values(), health]


def render_requests() -> bytes:
    """
    Render only the per-request series, for the frontend's /metrics. The daemon
    process serves these too, alongside everything the daemons record.
    """
    isolated = CollectorRegistry()
    isolated.register(RequestCollector())
    return generate_latest(isolated)


def start_metrics_server(port: int) -> bool:
    """
    Serve DMM's metrics on the given port. Must be called from the parent
    process, before the frontend is spawned and before any daemon starts.
    Failures are logged, not raised.
    """
    if port is None or port <= 0:
        logging.info("metrics port is not set, not starting the metrics exporter")
        return False
    try:
        registry.register(RequestCollector())
        start_http_server(port, registry=registry)
        logging.info(f"Metrics exporter listening on :{port}")
        return True
    except Exception as e:
        logging.error(f"Failed to start metrics exporter on :{port}: {e}", exc_info=True)
        return False
