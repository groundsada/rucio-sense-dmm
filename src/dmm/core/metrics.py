import logging

from prometheus_client import REGISTRY, start_http_server

registry = REGISTRY


def start_metrics_server(port: int) -> bool:
    """
    Serve the daemon process's metrics on the given port. Must be called from
    the parent process, before the frontend is spawned and before any daemon
    starts. Failures are logged, not raised.
    """
    if port is None or port <= 0:
        logging.info("metrics port is not set, not starting the metrics exporter")
        return False
    try:
        start_http_server(port, registry=registry)
        logging.info(f"Metrics exporter listening on :{port}")
        return True
    except Exception as e:
        logging.error(f"Failed to start metrics exporter on :{port}: {e}", exc_info=True)
        return False
