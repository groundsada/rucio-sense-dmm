# Data Movement Manager (DMM)

Data Movement Manager (DMM) provides the interface layer between Rucio/FTS and SENSE for the Rucio-SENSE interoperation framework. 
DMM enables Software-Defined Networking (SDN) operated high-energy physics (HEP) data flows by orchestrating network-aware data transfers.

## Prerequisites

- **Rucio Server**: Must be running and accessible (with DMM patch applied)
- **PostgreSQL**: Must be running and reachable by DMM.
- **Certificates**: Valid X.509 host certificates for authentication with FTS.
- **SENSE OAuth**: Configured `.sense-o-auth.yaml` credentials
- **Kubernetes**: (for Kubernetes deployment, NRP is recommended)
- **Docker**: (for Docker deployment)

## Configuration

DMM requires the following configuration files:

1. **`dmm.cfg`**: DMM-specific configuration, refer to the sample config for an example.
2. **`rucio.cfg`**: Rucio client configuration
3. **X.509 Certificates**: Host certificate and key (`hostcert.pem`, `hostcert.key.pem`)
4. **`.sense-o-auth.yaml`**: SENSE OAuth credentials

## Deployment

### Option 1: Kubernetes (Recommended for Production)

#### 1. Create Configuration Secrets

```bash
cd etc/
./mksecrets.sh
```

#### 3. Initialize RSEs in Rucio

Ensure all required Rucio Storage Elements (RSEs) are configured before deploying DMM. If RSEs are added later, use the "Refresh Sites" button in the DMM web interface (Sites tab).

#### 4. Deploy to Kubernetes using Helm

```bash
helm install dmm etc/helm/
```

The deployment includes an embedded PostgreSQL instance. For production environments, consider using an external managed database service.

Metrics, traces and probes are all off or inert by default. To turn them on:

```bash
helm install dmm etc/helm/ \
  --set serviceMonitor.enabled=true \
  --set serviceMonitor.labels.release=<your prometheus release> \
  --set otel.endpoint=http://<collector>:4317
```

`dmm.metricsPort` must match `[dmm] metrics_port` in the `dmm.cfg` secret; the
chart does not template that file and cannot check it. Use
`metricsAnnotations.enabled=true` instead of the `ServiceMonitor` if your
collector discovers by `prometheus.io/scrape` annotation.

### Option 2: Docker

#### 1. Start PostgreSQL

```bash
docker run -d \
  --name dmm-postgres \
  -e POSTGRES_PASSWORD=your_secure_password \
  -e POSTGRES_DB=dmm \
  -p 5432:5432 \
  postgres:14
```

#### 2. Run DMM Container

```bash
docker run -d \
  --name dmm \
  -v $HOME/private/dmm.cfg:/opt/dmm/dmm.cfg \
  -v $HOME/private/rucio.cfg:/opt/rucio/etc/rucio.cfg \
  -v $HOME/private/certs/rucio-sense/hostcert.pem:/opt/certs/cert.pem \
  -v $HOME/private/certs/rucio-sense/hostcert.key.pem:/opt/certs/key.pem \
  -v $HOME/.sense-o-auth.yaml:/root/.sense-o-auth.yaml \
  aaarora/dmm:latest
```

## Monitoring and Management

Access the DMM web frontend to monitor data flows and manage site configurations. The interface provides:

- Real-time transfer status monitoring
- Site/RSE management (with refresh capability)
- Network provisioning status

### Prometheus requirements

`[prometheus] host` must point at a Prometheus that scrapes each site's DTN node
exporters **with a `sitename` label matching the SENSE site name**. `MonitDaemon`
measures per-circuit throughput with
`node_network_transmit_bytes_total{device=...,instance=...,job=...,sitename=...}`,
so exporters scraped without `sitename` match no series at all. DMM records that
as an unmeasurable cycle and leaves the previous reading in place, and
`dmm_monit_scrape_failures_total{reason="no_interfaces"}` will climb — check it
first if circuit health looks wrong.

DMM exports its own metrics on `[dmm] metrics_port` (default 9100) and, for
backwards compatibility, at `/metrics` on the frontend port.

### Tracing

Off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set — a deployment with no
collector stays silent rather than failing to connect every batch interval.
Everything else is the standard `OTEL_*` set: `OTEL_SERVICE_NAME` (default
`dmm`), `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_HEADERS` for a
multi-tenant collector, and `OTEL_TRACES_SAMPLER_ARG` (default `0.1`, because
fourteen daemons open a span per cycle). `OTEL_SDK_DISABLED=true` turns it off
outright.

Spans: one per daemon cycle, one per SENSE-O call carrying the SENSE UUID, one
around the LP solve with its `linprog` call count, and one per `/query` — whose
parent is the `traceparent` Rucio sends, so a rule can be followed from the
transfertool through DMM's allocation into SENSE-O. `etc/rucio.patch` injects
that header; both halves tag the span with the rule id, and Rucio degrades to an
unheadered request if OpenTelemetry is not installed there.

`etc/rucio.patch` also adds two counters on the Rucio side, through the
`MetricManager` already present in `transfertool/fts3.py`:
`sense_routing_applied` and `sense_routing_skipped{reason}`, where reason is one
of `no_rule_id`, `not_allocated`, `dmm_unreachable`, `invalid_response` or
`no_endpoints_in_transfer`. Only Rucio can count these — DMM sees roughly one
query per rule, never one per transfer — and without them a DMM outage looks
like a healthy stack over which zero bytes rode a circuit.

The daemons and the frontend report the same `service.name` under different
`service.instance.id`. Tracing is configured **after** the frontend is forked,
in each process separately: a `TracerProvider` built before the fork loses its
exporter thread in the child, and the global provider cannot be replaced once
set. `dmm.py` does this deliberately; `core/tracing.py` explains it and detects
the mistake if the ordering is ever changed.

Trace and span ids are injected into the existing log lines, which is what makes
a Loki line clickable through to the Tempo trace. Logs still reach Loki from
stdout only — no second OTLP log pipeline is installed.

### Health endpoints

- `GET /health` — deep check. 200 when every daemon is completing cycles, the
  database answers and the site table is populated; **503** otherwise, with
  per-daemon detail in the body. Use this for a readiness probe.
- `GET /health/live` — the frontend process is serving. Use this for a liveness
  probe: `/health` going red for one slow daemon should not restart the pod and
  take its in-flight circuits with it.

The frontend runs in a separate process from the daemons, so it cannot see
their state directly. Each daemon writes a small file to
`$DMM_HEARTBEAT_DIR` (default `/tmp/dmm-heartbeats`) and `/health` reads them.
Both processes must see the same directory — in Kubernetes that means an
`emptyDir`, not two separate paths. A daemon is reported stale after five
missed intervals, measured against its own configured frequency; a daemon whose
frequency is negative is reported `disabled` and does not fail the check.