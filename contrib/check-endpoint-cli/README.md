# check-endpoint CLI image

A general-purpose image for running `check-endpoint` interactively — from a
laptop, or from inside a Kubernetes cluster where the network you care about
actually is.

For scraping metrics continuously, use
[`contrib/check-endpoint-exporter/`](../check-endpoint-exporter/) instead. That
image is a daemon; this one is a command.

## Build

From the repository root, so `check-endpoint.py` is in the build context:

```bash
docker build -f contrib/check-endpoint-cli/Dockerfile -t check-endpoint .
```

Multi-arch, since laptops are usually arm64 and clusters usually amd64:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
    -f contrib/check-endpoint-cli/Dockerfile \
    -t ghcr.io/bytebeast/check-endpoint:latest --push .
```

With extra diagnostic tools baked in, which is often worth it for a debug image:

```bash
docker build --build-arg EXTRA_PACKAGES="dnsutils curl iputils-ping" \
    -f contrib/check-endpoint-cli/Dockerfile -t check-endpoint .
```

`EXTRA_PACKAGES` is a space-separated list of Debian package names, passed
straight to `apt-get install` in the runtime stage. Names have to be exactly
what the archive calls them: `netcat` is a virtual package and fails, use
`netcat-openbsd`; there is no `iputils` or `ping` package, the binaries ship
separately as `iputils-ping`, `iputils-tracepath` and `iputils-arping`. To
check a name before spending a build on it:

```bash
docker run --rm python:3.12-slim \
    bash -c "apt-get update -qq && apt-cache policy netcat-openbsd"
```

### Laptop debug image

The tagged builds above stay lean because they get pulled onto every node that
runs them. A laptop image has no such constraint, so it is worth building one
fat image once and keeping it around for the sessions where you are chasing a
problem rather than taking a measurement:

```bash
docker build \
    -f contrib/check-endpoint-cli/Dockerfile \
    --build-arg EXTRA_PACKAGES="bash coreutils findutils grep sed gawk less \
                                tar gzip zip unzip file which hostname curl \
                                wget ca-certificates openssl iproute2 dnsutils \
                                traceroute procps psmisc util-linux jq yq \
                                awscli vim-tiny netcat-openbsd \
                                iputils-ping iputils-tracepath \
                                iputils-arping" \
    -t check-endpoint:debug .
```

`dnsutils` and `iproute2` for the resolver and routing questions that the `DNS`
and `TCP` columns raise, `openssl` for the certificate that `--tls-info`
reports, `traceroute` and `netcat-openbsd` for the hop that is dropping the
connection, `jq` and `yq` for reading the API responses and manifests you are
probing against.

Two things are deliberately not in that list. `kubectl` is not in the Debian
archive — it comes from `pkgs.k8s.io`, so adding it means a second `RUN` with
that apt repository rather than a `--build-arg`. And this image is for a
laptop: keep the cluster-side one lean, because a debug pod carrying `awscli`
and a shell is a credential path for anyone who can `exec` into it, and IRSA
will happily hand it a role.

Tag it for what is inside rather than reusing `:debug` on every rebuild —
`check-endpoint:debug-2026-08` beats wondering which packages the local
`:debug` actually has.

## Run locally

```bash
# Colour output needs -t, since colour is auto-disabled off a TTY
docker run --rm -it check-endpoint -c 10 --stats https://example.com

# Pipe it somewhere - drop -t and the output is clean text
docker run --rm check-endpoint -c 10 --stats https://example.com > result.txt

# Get a shell instead of the probe
docker run --rm -it --entrypoint bash check-endpoint
```

### A container that stays up

`ENTRYPOINT` is the probe, so the container exits as soon as the run finishes.
For a session where you want the container to outlive any one shell — several
terminals against it, or a scratch file in `/tmp` that survives you closing the
window — override the entrypoint with something that does nothing and exec into
it:

```bash
docker run -d --name probe --entrypoint sleep check-endpoint:debug infinity
docker exec -it probe bash
```

Inside, the CLI is on `PATH` under its own name, uid is 10001 and the working
directory is `/tmp`:

```bash
check-endpoint -c 10 --stats https://example.com
check-endpoint --tls-info https://example.com
```

`docker rm -f probe` when you are done. Note that `infinity` sits after the
image name because it is an argument to `sleep`, not to `docker run` — the same
split as `--command --` in `kubectl run`.

## Run in Kubernetes

### Interactive debug pod

```bash
kubectl apply -f debug-pod.yaml
kubectl exec -it check-endpoint -- bash

# then, inside:
check-endpoint -c 10 --stats http://my-svc.my-ns.svc.cluster.local/health
check-endpoint --tls-info https://external-api.example.com
```

`kubectl delete -f debug-pod.yaml` when you're done.

### One-shot, no manifest

```bash
kubectl run check-endpoint --rm -it --restart=Never \
    --image=ghcr.io/bytebeast/check-endpoint:latest \
    -- -c 10 --stats https://example.com
```

Everything after `--` goes to the entrypoint, so it reads exactly like the local
CLI.

### From a specific node

Timing varies by where you land. To probe from a particular node:

```bash
kubectl run check-endpoint --rm -it --restart=Never \
    --image=ghcr.io/bytebeast/check-endpoint:latest \
    --overrides='{"spec":{"nodeName":"ip-10-0-1-23.ec2.internal"}}' \
    -- -c 20 --stats https://example.com
```

## Things that will skew your measurements

These matter more here than in the exporter, because in-cluster numbers get
compared against laptop numbers and the difference gets blamed on the endpoint.

**`ndots:5` inflates the DNS column.** This is the big one. Kubernetes' default
resolver config appends cluster search domains to any name with fewer than five
dots, so resolving `example.com` fires three NXDOMAIN round trips to CoreDNS
before the real query. All of that lands in `DNS`. The supplied `debug-pod.yaml`
sets `ndots:1`; a fully-qualified name with a trailing dot (`example.com.`) also
bypasses it. Leave `ndots:5` in place only when you are deliberately measuring
what your own workloads experience — because they have it too.

**CPU limits throttle the prober.** A container that hits its CFS quota gets
descheduled, and that stall is indistinguishable from network latency in the
output. `debug-pod.yaml` sets a CPU _request_ and no CPU limit for this reason.
If your namespace has a `LimitRange` that injects a default CPU limit, you will
need an explicit override.

**Service mesh sidecars are in the path.** With Istio or Linkerd injected you
are measuring the mesh, not the endpoint — TLS is terminated and re-originated
by the sidecar, so `TLS_HANDSHAKE` describes the proxy's connection and the
certificate `--tls-info` reports is the mesh's. Add
`sidecar.istio.io/inject: "false"` to the pod annotations to probe past it, or
keep it and know what you are looking at.

**`-6` fails on single-stack clusters.** Most CNI setups are IPv4-only, so
forcing IPv6 gives `<CONN-FAIL>` on every row. That's the cluster, not the
target.

**Read percentiles, not a single run.** The first request in a fresh pod pays
setup costs that later ones don't, so use `-c N` with `--stats` rather than
trusting one row.

## Security notes

The image runs as UID 10001, and the manifest sets `readOnlyRootFilesystem`,
drops all capabilities, and applies `RuntimeDefault` seccomp — so it satisfies
the `restricted` Pod Security Standard without exemptions.

`/tmp` is an `emptyDir` because `readOnlyRootFilesystem` would otherwise break
`--cookie-jar` and `--export`.

Remember this is a general-purpose HTTP client with a shell inside your cluster.
Delete the debug pod when you're finished rather than leaving it running.
