import logging
from contextlib import contextmanager
from functools import wraps
from time import monotonic

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from dmm.db.session import get_session
from dmm.models.endpoint import Endpoint as DBEndpoint
from dmm.models.request import Request as DBRequest

registry = REGISTRY

# Cycles are bounded by how long SENSE-O takes, not by the daemon frequency, so
# the buckets have to reach well past the default 10s ceiling.
DAEMON_DURATION_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, float("inf"))

DAEMON_CYCLE_DURATION = Histogram(
    "dmm_daemon_cycle_duration_seconds",
    "Time spent running one daemon cycle, measured after the lock is held",
    ["daemon"], buckets=DAEMON_DURATION_BUCKETS)

DAEMON_LOCK_WAIT = Histogram(
    "dmm_daemon_lock_wait_seconds",
    "Time a daemon waited for the process-wide lock before its cycle could start",
    ["daemon"], buckets=DAEMON_DURATION_BUCKETS)

DAEMON_LAST_SUCCESS = Gauge(
    "dmm_daemon_last_success_timestamp_seconds",
    "Unix time of the last cycle that returned without raising",
    ["daemon"])

DAEMON_ERRORS = Counter(
    "dmm_daemon_cycle_errors_total", "Daemon cycles that raised, by exception type",
    ["daemon", "exc_type"])

DAEMON_RUNNING = Gauge(
    "dmm_daemon_running", "1 while the daemon loop is alive, 0 before start and after exit",
    ["daemon"])

SENSE_API_BUCKETS = (0.05, 0.25, 1, 2.5, 5, 10, 30, 60, 120, 300, float("inf"))

SENSE_API_DURATION = Histogram(
    "dmm_sense_api_duration_seconds", "SENSE-O and address-pool API call latency",
    ["op", "outcome"], buckets=SENSE_API_BUCKETS)

SENSE_SYNC_TIMEOUTS = Counter(
    "dmm_sense_sync_timeouts_total",
    "sync=true calls that returned 504, leaving DMM and SENSE-O possibly divergent",
    ["op"])

ADDRESS_FREE_FAILURES = Counter(
    "dmm_address_free_failures_total", "free_address calls that failed, each leaking one subnet",
    ["pool_site"])

SITES_LAST_REFRESH = Gauge(
    "dmm_sites_last_refresh_timestamp_seconds", "Unix time of the last successful site database refresh")


# Duplicated from core/utils rather than imported: core.utils imports core.allocation,
# which imports this module, and the cycle would not resolve.
def _is_sync_timeout(exc) -> bool:
    msg = str(exc).lower()
    return "504" in msg and ("gateway time-out" in msg or "gateway timeout" in msg or "time-out" in msg)


@contextmanager
def sense_api_call(op):
    """Time one SENSE-O API call and classify how it ended. Never swallows."""
    start = monotonic()
    outcome = "ok"
    try:
        yield
    except Exception as e:
        if _is_sync_timeout(e):
            outcome = "timeout"
            SENSE_SYNC_TIMEOUTS.labels(op).inc()
        else:
            outcome = "error"
        raise
    finally:
        SENSE_API_DURATION.labels(op, outcome).observe(monotonic() - start)


def timed_sense_call(op):
    def decorate(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            with sense_api_call(op):
                return function(*args, **kwargs)
        return wrapper
    return decorate

REQUEST_LABELS = ["rule_id", "transfer_status", "sense_circuit_status", "src_site", "dst_site"]

TERMINAL_WINDOW_HOURS = 6
MAX_EXPORTED_REQUESTS = 5000

# name, help, accessor, value used when the column is NULL (None skips the sample)
REQUEST_GAUGES = (
    ("dmm_request_sense_retries", "Number of SENSE retries for request",
     lambda r: r.sense_retries, 0),
    ("dmm_request_allocated_bandwidth_mbps", "Allocated bandwidth in Mbps",
     lambda r: r.allocated_bandwidth_mbps, None),
    ("dmm_request_available_bandwidth_mbps", "Available bandwidth in Mbps",
     lambda r: r.available_bandwidth_mbps, None),
    ("dmm_request_previous_bandwidth_mbps", "Previous bandwidth in Mbps",
     lambda r: r.previous_bandwidth_mbps, None),
    ("dmm_request_fts_streams_current", "Current FTS streams",
     lambda r: r.fts_streams_current, 0),
    ("dmm_request_fts_streams_desired", "Desired FTS streams",
     lambda r: r.fts_streams_desired, None),
    ("dmm_request_prometheus_throughput_mbps", "Measured throughput in Mbps",
     lambda r: r.prometheus_throughput, 0),
    ("dmm_request_prometheus_bytes", "Measured bytes from prometheus polling",
     lambda r: r.prometheus_bytes, 0),
    ("dmm_request_rule_size_bytes", "Size of the Rucio rule in bytes",
     lambda r: r.rule_size, None),
)


def _epoch(dt):
    # created_at is written tz-aware UTC while sense_provisioned_at and
    # rucio_finished_at are written naive by datetime.now(). Both columns drop the
    # offset on the way into the database, so any span crossing the two is only
    # correct while the container runs UTC, which it does.
    return dt.timestamp() if dt is not None else None


def _elapsed(start, end):
    start, end = _epoch(start), _epoch(end)
    if start is None or end is None:
        return None
    seconds = end - start
    return seconds if seconds >= 0 else None


def _is_reused(req) -> bool:
    return bool(req.sense_alloc_rule_id) and req.sense_alloc_rule_id != req.rule_id

# First match wins. Keeps failure cardinality bounded at one series per class,
# where the raw reason string would be unbounded.
FAILURE_REASON_CLASSES = (
    ("max_retries", ("reached max sense retries",)),
    ("no_vlan_range", ("no vlan range",)),
    ("no_subnet", ("allocation failed", "subnet")),
    ("missing_site", ("missing source or destination site",)),
    ("circuit_failed", ("circuit reached", "sense circuit")),
    ("staging_failed", ("staging failed",)),
    ("provisioning_failed", ("provisioning failed",)),
)


def classify_failure_reason(reason) -> str:
    if not reason:
        return "unknown"
    text = str(reason).lower()
    for name, needles in FAILURE_REASON_CLASSES:
        if any(needle in text for needle in needles):
            return name
    return "other"


class RequestCollector:
    """
    Derives the per-request series from the database on each scrape. Registered
    in the daemon process; a failure here yields no samples rather than failing
    the whole scrape.
    """

    def collect(self):
        try:
            with get_session() as session:
                reqs = DBRequest.get_for_metrics(
                    session=session,
                    terminal_window_hours=TERMINAL_WINDOW_HOURS,
                    limit=MAX_EXPORTED_REQUESTS,
                )
                return self._families(reqs)
        except Exception as e:
            logging.error(f"Failed to collect request metrics: {e}", exc_info=True)
            return []

    def _families(self, reqs):
        total = GaugeMetricFamily(
            "dmm_requests_total",
            "Number of requests exported: all non-terminal, plus terminal ones "
            f"updated within {TERMINAL_WINDOW_HOURS}h")
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
            labels=REQUEST_LABELS + ["src_rse", "dst_rse", "reused"])
        gauges = {
            name: GaugeMetricFamily(name, doc, labels=REQUEST_LABELS)
            for name, doc, _, _ in REQUEST_GAUGES
        }
        ratio = GaugeMetricFamily(
            "dmm_request_throughput_ratio",
            "Measured throughput as a fraction of allocated bandwidth",
            labels=REQUEST_LABELS)
        health = GaugeMetricFamily(
            "dmm_request_health", "Health status (1=healthy, 0=unhealthy, absent=unknown)",
            labels=REQUEST_LABELS)

        time_to_provision = GaugeMetricFamily(
            "dmm_request_time_to_provision_seconds",
            "Seconds from the request being created to its circuit reaching CREATE - READY",
            labels=REQUEST_LABELS)
        circuit_lifetime = GaugeMetricFamily(
            "dmm_request_circuit_lifetime_seconds",
            "Seconds the circuit was live before the Rucio rule finished",
            labels=REQUEST_LABELS)

        failed = CounterMetricFamily(
            "dmm_requests_failed_total", "Requests that failed permanently, by reason class",
            labels=["reason_class"])
        failures = {}

        # A reused request skips staging and the decider entirely, so a lifecycle
        # funnel that ignores this arm shows a false drop-off before PROVISIONED.
        reused = CounterMetricFamily(
            "dmm_requests_reused_total", "Requests that claimed an already-provisioned circuit")
        reused_count = 0

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

            is_reused = _is_reused(req)
            reused_count += int(is_reused)
            info.add_metric(
                labels + [req.src_logical_site or src_site, req.dst_logical_site or dst_site,
                          str(is_reused).lower()], 1)

            for name, _, value_of, default in REQUEST_GAUGES:
                value = value_of(req)
                if value is None:
                    value = default
                if value is not None:
                    gauges[name].add_metric(labels, value)

            allocated = req.allocated_bandwidth_mbps
            if allocated and allocated > 0 and req.prometheus_throughput is not None:
                ratio.add_metric(labels, req.prometheus_throughput / allocated)

            if str(req.health) in ("0", "1"):
                health.add_metric(labels, int(req.health))

            provisioned = _elapsed(req.created_at, req.sense_provisioned_at)
            if provisioned is not None:
                time_to_provision.add_metric(labels, provisioned)

            lifetime = _elapsed(req.sense_provisioned_at, req.rucio_finished_at)
            if lifetime is not None:
                circuit_lifetime.add_metric(labels, lifetime)

            if req.failed_at is not None or str(req.transfer_status) == "FAILED":
                reason_class = classify_failure_reason(req.failure_reason)
                failures[reason_class] = failures.get(reason_class, 0) + 1

        for reason_class in sorted(failures):
            failed.add_metric([reason_class], failures[reason_class])
        reused.add_metric([], reused_count)

        return [total, by_status, info, *gauges.values(), ratio, health,
                time_to_provision, circuit_lifetime, failed, reused]


class InfrastructureCollector:
    """
    Endpoint pool occupancy and leaks, derived from the database on each scrape.
    Pool exhaustion and address leaks both surface today as a failed transfer at
    some other layer, never as themselves.
    """

    def collect(self):
        try:
            with get_session() as session:
                return self._families(
                    DBEndpoint.get_all(session=session, use_lock=False),
                    DBRequest.get_live_endpoint_ids(session=session),
                )
        except Exception as e:
            logging.error(f"Failed to collect infrastructure metrics: {e}", exc_info=True)
            return []

    def _families(self, endpoints, live_endpoint_ids):
        total = GaugeMetricFamily(
            "dmm_endpoints_total", "Endpoints known to DMM", labels=["site"])
        allocated = GaugeMetricFamily(
            "dmm_endpoints_allocated", "Endpoints marked allocated", labels=["site"])
        leaked = GaugeMetricFamily(
            "dmm_endpoints_leaked",
            "Endpoints marked allocated whose owning request is terminal or gone",
            labels=["site"])

        counts = {}
        for endpoint in endpoints:
            site = endpoint.site_name or "UNKNOWN"
            bucket = counts.setdefault(site, [0, 0, 0])
            bucket[0] += 1
            if endpoint.is_allocated:
                bucket[1] += 1
                if endpoint.id not in live_endpoint_ids:
                    bucket[2] += 1

        for site in sorted(counts):
            n_total, n_allocated, n_leaked = counts[site]
            total.add_metric([site], n_total)
            allocated.add_metric([site], n_allocated)
            leaked.add_metric([site], n_leaked)

        return [total, allocated, leaked]


def render_requests() -> bytes:
    """
    Render the database-derived series for the frontend's /metrics. The daemon
    process serves these too, alongside everything the daemons record in memory.
    """
    isolated = CollectorRegistry()
    isolated.register(RequestCollector())
    isolated.register(InfrastructureCollector())
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
        registry.register(InfrastructureCollector())
        start_http_server(port, registry=registry)
        logging.info(f"Metrics exporter listening on :{port}")
        return True
    except Exception as e:
        logging.error(f"Failed to start metrics exporter on :{port}: {e}", exc_info=True)
        return False
