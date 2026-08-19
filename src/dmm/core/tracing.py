"""
OpenTelemetry bootstrap.

Configured entirely through the standard `OTEL_*` environment variables so the
Helm chart can drive it without a code change, and disabled unless an exporter
endpoint is set — a deployment with no collector should be silent, not spend
every batch interval failing to connect.

## Call this after the fork, in each process

`DMM.start()` spawns the frontend with `multiprocessing.Process`. A
`TracerProvider` built in the parent before that call exports nothing from the
child: `BatchSpanProcessor` does its work on a background thread, and threads do
not survive a fork. The child inherits the object, queues spans into it, and
nothing ever drains the queue.

Setting the global provider is also once-per-process and cannot be undone, so
the child cannot repair an inherited one. `DMM.start()` therefore forks the
frontend *before* configuring tracing for the daemons, and `run_server`
configures its own. `setup_tracing` detects the mistake if that ordering is
ever changed and says so rather than losing spans quietly.
"""
import atexit
import logging
import os
import socket
from contextlib import contextmanager

_PROVIDER = None
_CONFIGURED_PID = None

DEFAULT_SERVICE_NAME = "dmm"
# Fourteen daemons each opening a span per cycle, plus a span per database
# query underneath, is a lot of trace for very little new information. The
# sampler is parent-based, so a trace that starts at Rucio is kept or dropped
# whole.
DEFAULT_SAMPLER = "parentbased_traceidratio"
DEFAULT_SAMPLER_ARG = "0.1"


def _endpoint_configured() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
                or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def _instance_id(role) -> str:
    return f"{role}-{socket.gethostname()}-{os.getpid()}"


def setup_tracing(role: str):
    """
    Configure tracing for this process. Returns the provider, or None if
    tracing is off or could not be started. Never raises: DMM must run whether
    or not a collector exists.
    """
    global _PROVIDER, _CONFIGURED_PID

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        logging.info("OTEL_SDK_DISABLED is set, not configuring tracing")
        return None

    if not _endpoint_configured():
        logging.info("no OTEL_EXPORTER_OTLP_ENDPOINT, not configuring tracing")
        return None

    if _CONFIGURED_PID is not None and _CONFIGURED_PID != os.getpid():
        logging.error(
            f"tracing was configured in pid {_CONFIGURED_PID} and inherited by pid "
            f"{os.getpid()} across a fork. The batch span processor's thread did not "
            f"come with it, so spans from this process would be queued and never "
            f"exported. Configure tracing after the fork, not before."
        )
        return None

    if _CONFIGURED_PID == os.getpid():
        return _PROVIDER

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logging.warning(f"opentelemetry is not installed, not configuring tracing: {e}")
        return None

    try:
        os.environ.setdefault("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
        os.environ.setdefault("OTEL_TRACES_SAMPLER", DEFAULT_SAMPLER)
        os.environ.setdefault("OTEL_TRACES_SAMPLER_ARG", DEFAULT_SAMPLER_ARG)

        # Both processes report the same service under different instance ids,
        # which is what lets a trace show the frontend handing off to a daemon
        # rather than looking like two unrelated services.
        resource = Resource.create({
            "service.instance.id": _instance_id(role),
            "dmm.role": role,
        })
        # Sampler and endpoint are read from the environment by the SDK itself.
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)

        _PROVIDER = provider
        _CONFIGURED_PID = os.getpid()
        atexit.register(provider.shutdown)

        _instrument_logging()
        _instrument_requests()
        logging.info(
            f"tracing configured for role={role} instance={_instance_id(role)} "
            f"sampler={os.environ['OTEL_TRACES_SAMPLER']}:"
            f"{os.environ['OTEL_TRACES_SAMPLER_ARG']}"
        )
        return provider
    except Exception as e:
        logging.error(f"failed to configure tracing: {e}", exc_info=True)
        return None


def _instrument_logging():
    """
    Put the trace and span id on every log record and into the log format.

    This is the join between Loki and Tempo: without it the logs and the traces
    describe the same work with nothing in common to click through on. The
    format is only widened once the record factory is installed, because until
    then the fields do not exist and every log call would raise.
    """
    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
    except ImportError:
        logging.warning("opentelemetry logging instrumentation not installed")
        return
    try:
        LoggingInstrumentor().instrument(
            # set_logging_format would replace DMM's format wholesale and lose
            # the thread name, which is the only per-daemon label the logs have.
            set_logging_format=False,
            inject_trace_context=True,
            # Defaults to true, and attaches a handler that ships every record
            # over OTLP. DMM's logs reach Loki from stdout; a second pipeline
            # would duplicate them and make tracing depend on a logs collector.
            enable_log_auto_instrumentation=False,
        )
    except Exception as e:
        logging.warning(f"could not instrument logging: {e}")
        return

    if not _record_carries_trace_ids():
        logging.warning(
            "the logging instrumentation did not add trace ids to log records; "
            "leaving the log format alone rather than breaking every log call")
        return

    from dmm import LOG_DATEFMT, LOG_FORMAT
    traced = LOG_FORMAT.replace(
        "%(levelname)s:", "%(levelname)s [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s]:")
    formatter = logging.Formatter(traced, datefmt=LOG_DATEFMT)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def _record_carries_trace_ids() -> bool:
    """
    The kwargs that make the instrumentation populate these fields have moved
    between releases, and widening the format without them makes every single
    log call raise. Ask the factory rather than trusting the version.
    """
    try:
        record = logging.getLogRecordFactory()(
            "dmm.tracing.probe", logging.DEBUG, __file__, 0, "probe", None, None)
        return hasattr(record, "otelTraceID") and hasattr(record, "otelSpanID")
    except Exception:
        return False


def _instrument_requests():
    """
    Outbound HTTP carries `traceparent` once this is on, which is what connects
    DMM's spans to SENSE-O's if SENSE-O ever reads the header.
    """
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
    except ImportError:
        logging.warning("opentelemetry requests instrumentation not installed")
        return
    try:
        RequestsInstrumentor().instrument()
    except Exception as e:
        logging.warning(f"could not instrument requests: {e}")


def instrument_database():
    """
    Span per query. Called from the daemon process only — the frontend opens its
    own sessions against the same engine and instrumenting per process is enough.
    """
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from dmm.db.session import get_engine
    except ImportError as e:
        logging.warning(f"opentelemetry sqlalchemy instrumentation not installed: {e}")
        return
    try:
        SQLAlchemyInstrumentor().instrument(engine=get_engine())
    except Exception as e:
        logging.warning(f"could not instrument sqlalchemy: {e}")


def instrument_app(app):
    """Server spans for the frontend, including extracting inbound trace context."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logging.warning("opentelemetry fastapi instrumentation not installed")
        return
    try:
        # The probes and the exporter would otherwise dominate the trace volume
        # with spans nobody will ever look at.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls="health,health/live,metrics")
    except Exception as e:
        logging.warning(f"could not instrument the frontend: {e}")


class _NoopSpan:
    def set_attribute(self, key, value):
        pass

    def record_exception(self, exception):
        pass

    def set_status(self, *args, **kwargs):
        pass


class _NoopTracer:
    """Stands in when opentelemetry is not installed at all."""

    @contextmanager
    def start_as_current_span(self, name, **kwargs):
        yield _NoopSpan()


_NOOP_TRACER = _NoopTracer()


def get_tracer(name: str):
    """
    A tracer that is safe to use whether or not tracing was configured, and
    whether or not opentelemetry is installed. With a provider but no exporter
    the SDK already returns non-recording spans, so instrumentation can be
    unconditional at the call site.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return _NOOP_TRACER
    return trace.get_tracer(name)
