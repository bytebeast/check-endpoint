# check-endpoint interactive CLI image.
#
# The sibling image in contrib/check-endpoint-exporter/ is a long-lived daemon
# with a port and a healthcheck. This one is the opposite: a tool you invoke,
# either from a laptop like any other CLI, or from inside a cluster as a debug
# pod so you can probe an endpoint from the network that actually matters.
#
# Build from the REPOSITORY ROOT (so check-endpoint.py is in the context):
#   docker build -f contrib/check-endpoint-cli/Dockerfile -t check-endpoint .
#
# Multi-arch, since laptops are usually arm64 and clusters usually amd64:
#   docker buildx build --platform linux/amd64,linux/arm64 \
#       -f contrib/check-endpoint-cli/Dockerfile \
#       -t ghcr.io/bytebeast/check-endpoint:latest --push .

# Pinned so the image is reproducible: without this, a new pycurl release
# silently changes what gets built. Bump deliberately.
ARG PYCURL_VERSION=7.47.0

# ---- stage 1: build the pycurl wheel ----------------------------------------
FROM python:3.12-slim AS builder
ARG PYCURL_VERSION

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libcurl4-openssl-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Pin the TLS backend explicitly. Debian's libcurl4 is OpenSSL-linked, but if
# pycurl guesses a different backend the wheel builds cleanly and then dies at
# import with "link-time ssl backend is different from compile-time ssl
# backend" - a failure that only shows up in stage 2, long after the build
# looked like it worked.
ENV PYCURL_SSL_LIBRARY=openssl
RUN pip wheel --no-cache-dir --wheel-dir /wheels "pycurl==${PYCURL_VERSION}"

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim
ARG PYCURL_VERSION

# Extra diagnostic tools, off by default to keep the image small. Once you are
# exec'd into a pod, having dig and curl on hand to cross-check the probe is
# often worth the megabytes:
#   --build-arg EXTRA_PACKAGES="dnsutils curl iputils-ping"
ARG EXTRA_PACKAGES=""

# libcurl4        - required by pycurl at runtime
# ca-certificates - required for TLS verification, which is on by default.
#                   Without it EVERY https target returns <TLS-FAIL>, which
#                   looks identical to a genuinely broken certificate.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libcurl4 \
        ca-certificates \
        ${EXTRA_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels \
        "pycurl==${PYCURL_VERSION}" \
    && rm -rf /wheels \
    # pip exists only to place that wheel. Nothing at runtime uses it, it
    # carries CVEs of its own (CVE-2025-8869, CVE-2026-6357, CVE-2026-3219,
    # CVE-2026-8643), and removing it also removes the only package-install
    # path inside a container that is otherwise read-only. `python -m ensurepip`
    # brings it back if you ever need it interactively.
    && pip uninstall --yes pip setuptools wheel

# Installed on PATH under its own name so an interactive session can type
# `check-endpoint https://...` exactly as you would on a laptop, rather than
# `python /app/check-endpoint.py`.
WORKDIR /usr/local/bin
COPY check-endpoint.py /usr/local/bin/check-endpoint.py
RUN chmod 0755 /usr/local/bin/check-endpoint.py \
    && ln -s check-endpoint.py /usr/local/bin/check-endpoint

# Colour is auto-disabled when stdout is not a TTY, so this only has an effect
# under `docker run -it` or `kubectl exec -it`. Piping still gives clean text.
ENV TERM=xterm-256color

# Unprivileged, and /tmp as the working directory so --cookie-jar and other
# writes land somewhere that stays writable under readOnlyRootFilesystem
# (mount an emptyDir there - see debug-pod.yaml).
RUN useradd --system --uid 10001 --no-create-home probe
USER 10001
WORKDIR /tmp

# No EXPOSE and no HEALTHCHECK: this image is not a server. --prometheus still
# works if you want it, but the exporter image is the better fit for that.
#
# To get a shell instead of the probe:
#   docker run --rm -it --entrypoint bash check-endpoint
ENTRYPOINT ["check-endpoint"]
CMD ["--help"]
