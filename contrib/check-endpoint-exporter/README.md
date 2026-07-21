# check-endpoint-exporter

Run [`check-endpoint.py`](../../check-endpoint.py) as a Prometheus exporter.

The core script is a CLI tool. Its `--prometheus` flag turns it into a small
pull-based exporter: it serves metrics over HTTP and **re-probes the target on
every scrape**, so Prometheus always pulls fresh per-phase timing (DNS, TCP,
TLS, time-to-first-byte, download, total), the HTTP status, response size, and
the TLS certificate expiry.

This directory holds everything needed to build and run that exporter mode in a
container and on Kubernetes. It is optional and self-contained, which is why it
lives under `contrib/` rather than next to the script.

## Layout

```
contrib/check-endpoint-exporter/
├── README.md                 # this file
├── Dockerfile                # multi-stage image (pycurl + libcurl, non-root)
├── .dockerignore
├── docker-compose.yml        # local test
├── check-endpoint.py         # copy of the script, so this dir builds standalone
├── kubernetes/               # plain manifests (no Helm)
│   ├── deployment.yaml
│   ├── service.yaml
│   └── servicemonitor.yaml
└── helm/
    └── check-endpoint-exporter/   # Helm chart
        ├── Chart.yaml
        ├── values.yaml
        └── templates/
```

## Metrics

Every scrape runs `-c` probes (default 1) and exposes:

| Metric                                                            | Type  | Meaning                                             |
| ----------------------------------------------------------------- | ----- | --------------------------------------------------- |
| `check_endpoint_up`                                               | gauge | `1` if the most recent probe succeeded, else `0`    |
| `check_endpoint_http_response_code`                               | gauge | HTTP status of the last successful probe            |
| `check_endpoint_dns_seconds`                                      | gauge | DNS lookup time                                     |
| `check_endpoint_tcp_connect_seconds`                              | gauge | TCP connect time                                    |
| `check_endpoint_tls_handshake_seconds`                            | gauge | TLS handshake time (absent on plain HTTP)           |
| `check_endpoint_pretransfer_seconds`                              | gauge | Connect-ready to request-sent                       |
| `check_endpoint_first_byte_seconds`                               | gauge | Time to first byte                                  |
| `check_endpoint_body_download_seconds`                            | gauge | Body download time                                  |
| `check_endpoint_total_seconds`                                    | gauge | Total request time                                  |
| `check_endpoint_response_bytes`                                   | gauge | Response body size                                  |
| `check_endpoint_total_seconds_p50/p90/p95/p99`                    | gauge | Per-scrape percentiles (only when `-c` > 1)         |
| `check_endpoint_tls_expiry_days`                                  | gauge | Days until the TLS certificate expires (HTTPS only) |
| `check_endpoint_requests_total` / `check_endpoint_failures_total` | gauge | Probes run / failed this scrape                     |

All series are labeled `url` and `host`. The server answers metrics on **any**
path; `/metrics` is used by convention.

## Build the image

Build from the **repository root** so the Dockerfile can copy the script:

```bash
docker build -f contrib/check-endpoint-exporter/Dockerfile -t check-endpoint-exporter:latest .
```

Or build from this directory (it contains a copy of the script):

```bash
cd contrib/check-endpoint-exporter
docker build -t check-endpoint-exporter:latest .
```

Push it to your registry and note the reference (e.g.
`ghcr.io/OWNER/check-endpoint-exporter:1.0.0`); you will use it below.

## Run with Docker

```bash
docker run --rm -p 9109:9109 check-endpoint-exporter:latest \
  --prometheus --prometheus-bind 0.0.0.0 https://example.com

# in another terminal
curl localhost:9109/metrics
```

Or `docker compose up --build` (edit the target URL in `docker-compose.yml`
first). Extra probe flags go after the URL-less options, e.g. add `--http2` or
`-H "Authorization: Bearer TOKEN"` or `-c 10` (per-scrape percentiles).

## Deploy to Kubernetes with Helm

```bash
helm upgrade --install check-endpoint \
  contrib/check-endpoint-exporter/helm/check-endpoint-exporter \
  --namespace monitoring --create-namespace \
  --set image.repository=ghcr.io/OWNER/check-endpoint-exporter \
  --set image.tag=1.0.0 \
  --set target.url=https://example.com \
  --set serviceMonitor.enabled=true      # if you run the Prometheus Operator
```

Common values (see `values.yaml` for the full list):

| Value                             | Default                          | Purpose                                     |
| --------------------------------- | -------------------------------- | ------------------------------------------- |
| `target.url`                      | `https://example.com`            | Endpoint to probe (**set this**)            |
| `probe.count`                     | `1`                              | Probes per scrape; `>1` adds percentiles    |
| `probe.timeout`                   | `10`                             | Per-request timeout (seconds)               |
| `probe.extraArgs`                 | `[]`                             | Any extra flags, e.g. `{--http2}`           |
| `image.repository` / `image.tag`  | `ghcr.io/OWNER/...` / appVersion | Image                                       |
| `exporter.port` / `exporter.path` | `9109` / `/metrics`              | Listen port / scrape path                   |
| `serviceMonitor.enabled`          | `false`                          | Create a Prometheus Operator ServiceMonitor |
| `service.prometheusAnnotations`   | `false`                          | Add `prometheus.io/*` scrape annotations    |

To probe several endpoints, install the chart multiple times with different
release names and `target.url` values:

```bash
helm upgrade --install check-endpoint-api  ./helm/check-endpoint-exporter --set target.url=https://api.example.com ...
helm upgrade --install check-endpoint-web  ./helm/check-endpoint-exporter --set target.url=https://www.example.com ...
```

## Deploy without Helm

Edit the image and URL in `kubernetes/deployment.yaml`, then:

```bash
kubectl apply -n monitoring -f contrib/check-endpoint-exporter/kubernetes/deployment.yaml
kubectl apply -n monitoring -f contrib/check-endpoint-exporter/kubernetes/service.yaml
# Prometheus Operator users:
kubectl apply -n monitoring -f contrib/check-endpoint-exporter/kubernetes/servicemonitor.yaml
```

## Wire up Prometheus

**Prometheus Operator (ServiceMonitor):** enable `serviceMonitor.enabled` (Helm)
or apply `kubernetes/servicemonitor.yaml`. Make sure its labels match your
Prometheus `serviceMonitorSelector`.

**Plain Prometheus (static or kubernetes_sd):** the Service carries the classic
annotations when `service.prometheusAnnotations=true`. For a static scrape:

```yaml
scrape_configs:
  - job_name: check-endpoint
    metrics_path: /metrics
    static_configs:
      - targets: ["check-endpoint-exporter.monitoring.svc:9109"]
```

## Example alerts

```yaml
groups:
  - name: check-endpoint
    rules:
      - alert: EndpointDown
        expr: check_endpoint_up == 0
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "{{ $labels.url }} is failing probes"

      - alert: EndpointSlowTTFB
        expr: check_endpoint_first_byte_seconds > 1
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: "{{ $labels.url }} TTFB above 1s"

      - alert: TLSCertExpiringSoon
        expr: check_endpoint_tls_expiry_days < 15
        labels: { severity: warning }
        annotations:
          summary: "{{ $labels.url }} certificate expires in {{ $value }} days"
```

## Notes

- **CA certificates**: the probe verifies TLS by default, so the image ships
  `ca-certificates`. To test an internal/self-signed endpoint, add `-k` via
  `probe.extraArgs`.
- **Security**: the container runs as uid 10001, non-root, with a read-only root
  filesystem, no added capabilities, and `RuntimeDefault` seccomp. It needs no
  persistent storage.
- **One target per instance**: like the blackbox exporter's simplest setup, each
  deployment probes one URL. Run multiple releases for multiple targets.
- **HEALTHCHECK / probes** assume port 9109. If you change `--prometheus-port`,
  update the container `HEALTHCHECK` and `exporter.port` accordingly.
