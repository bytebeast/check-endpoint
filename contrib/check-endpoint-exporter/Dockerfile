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

# ---- stage 1: build the pycurl wheel ----------------------------------------
FROM python:3.12-slim AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libcurl4-openssl-dev \
      libssl-dev \
 && rm -rf /var/lib/apt/lists/*

RUN pip wheel --no-cache-dir --wheel-dir /wheels pycurl

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim

# libcurl4 is required by pycurl at runtime; ca-certificates is required for
# TLS verification (the probe verifies certificates by default).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libcurl4 \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels pycurl \
 && rm -rf /wheels

WORKDIR /app
COPY check-endpoint.py /app/check-endpoint.py

# Run as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home exporter
USER 10001

# Default exporter port (override with --prometheus-port).
EXPOSE 9109

# The metrics endpoint answers 200 on any path while the server is alive.
# NOTE: if you change --prometheus-port, override this HEALTHCHECK too.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:9109/').read()"]

# The script is the entrypoint; pass the daemon flags and target URL as args.
# Example:
#   docker run -p 9109:9109 check-endpoint-exporter \
#     --prometheus --prometheus-bind 0.0.0.0 https://example.com
ENTRYPOINT ["python", "/app/check-endpoint.py"]
CMD ["--help"]
