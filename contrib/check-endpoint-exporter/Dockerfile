# check-endpoint Prometheus exporter image.
#
# check-endpoint.py needs pycurl, which is a compiled binding to libcurl.
# Stage 1 builds a pycurl wheel with the toolchain; stage 2 keeps only the
# libcurl runtime and CA certificates, so the final image has no compilers.
#
# Build from the REPOSITORY ROOT (so check-endpoint.py is in the context):
#   docker build -f contrib/check-endpoint-exporter/Dockerfile -t check-endpoint-exporter .
#
# Or build from this directory (a copy of the script lives here too):
#   docker build -t check-endpoint-exporter .

# Pinned so the image is reproducible; matches contrib/check-endpoint-cli.
ARG PYCURL_VERSION=7.47.0

# ---- stage 1: build the pycurl wheel ----------------------------------------
FROM python:3.12-slim AS builder
ARG PYCURL_VERSION

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libcurl4-openssl-dev \
      libssl-dev \
 && rm -rf /var/lib/apt/lists/*

# Without this, a backend mismatch only shows up at import time in stage 2
# ("link-time ssl backend is different from compile-time ssl backend").
ENV PYCURL_SSL_LIBRARY=openssl
RUN pip wheel --no-cache-dir --wheel-dir /wheels "pycurl==${PYCURL_VERSION}"

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim
ARG PYCURL_VERSION

# libcurl4 is required by pycurl at runtime; ca-certificates is required for
# TLS verification (the probe verifies certificates by default).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libcurl4 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
# pip is only needed to place the wheel; it carries its own CVEs and nothing
# uses it at runtime.
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
      "pycurl==${PYCURL_VERSION}" \
 && rm -rf /wheels \
 && pip uninstall --yes pip setuptools wheel

WORKDIR /app
COPY check-endpoint.py /app/check-endpoint.py
RUN chmod 0755 /app/check-endpoint.py \
    && ln -s check-endpoint.py /app/check-endpoint

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home exporter
USER 10001

# Default exporter port (override with --prometheus-port).
EXPOSE 9109

# /healthz answers without probing the target; any other path runs a probe.
# NOTE: if you change --prometheus-port, override this HEALTHCHECK too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:9109/healthz').read()"]

# The script is the entrypoint; pass the daemon flags and target URL as args.
# Example:
#   docker run -p 9109:9109 check-endpoint-exporter \
#     --prometheus --prometheus-bind 0.0.0.0 https://example.com
ENTRYPOINT ["python", "/app/check-endpoint.py"]
CMD ["--help"]
