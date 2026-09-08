## Finding the one bad host

You know the situation. A service is behaving badly, but only _sometimes_. It's
behind a load balancer with a pile of backends, and you're fairly sure one of
them is sick. You just don't know which one.

Here's the lazy way to find out: hit every backend directly, ten times each, and
see which one stands out.

```bash
#!/bin/bash
#
# requires:
#       brew install parallel
#
url="http://www.google.com"
parallel_jobs=10
ips_file='ips.txt'

parallel -j${parallel_jobs} --joblog probe.log --results 'output/{}.txt' \
    ${HOME}/bin/check-endpoint.py -c 10 --pin-ip {} \
        --capture-on failed --capture-dir captures/ "${url}" \
    :::: <(awk 1 ${ips_file})
```

Update the url, drop your backend IPs in `ips.txt`, one per line, and run it.
That's the whole thing.

### What's going on here

`--pin-ip` is doing the heavy lifting. It tells `check-endpoint.py` to talk to
one specific IP while still presenting the real hostname, so SNI, the `Host`
header, and cert verification all behave normally. You're testing one backend,
but the request looks completely ordinary from the server's side.

`parallel` runs ten of those at a time. The nice part is that this scales
without you thinking about it. Ten IPs or ten thousand, same script: it keeps
ten in flight and works through the list.

Each backend's output lands in its own file at `output/<ip>.txt`, so nothing
interleaves and you can go read them at your leisure. Anything the probe
considered a failure also gets recorded in full by `--capture-on failed`, which
means you get to _read_ what went wrong instead of trying to reproduce it. Those
captures go in `captures/`, one timestamped folder per failing run. Without
`--capture-dir` they'd land in whatever directory you ran this from, which gets
messy fast once a few dozen hosts start misbehaving.

The `awk 1` wrapper around the IPs file is a small bit of paranoia. It makes
sure every line ends properly, so a file that's missing its final newline
doesn't quietly glue two addresses together into one nonsense argument.

### Reading the results

Start with the slow ones:

```bash
sort -t$'\t' -k4 -rn probe.log | head
```

Then just grep across the output files for whatever you're chasing: timeouts,
5xx, a suspiciously slow handshake:

```bash
grep -l '<TO>\|<CONN-FAIL>' output/*.txt
```

If one IP shows up over and over and its neighbours don't, congratulations, you
found your bad host.

### One tip

Out of the box the probe exits 0 even when a request times out, so `probe.log`
will happily report success for a host that's plainly broken. If you'd rather
have failures show up as real non-zero exits in the job log, give it something
to actually assert on:

```bash
--assert-status 200 --max-ttfb 500ms
```

Now a bad backend fails its job, and `probe.log` becomes your list of suspects
instead of just a timing record.

### Ten hosts or ten thousand

`ips.txt` can hold ten backends or ten thousand. The script doesn't care, it
just keeps ten probes in flight and works down the list. The bookkeeping is
genuinely free: dispatching a thousand jobs costs about six seconds in total,
which is nothing next to the actual probing.

On a big list it's worth adding `-t 3`, though. The default per-request timeout
is 10 seconds and we're doing 10 requests per host, so one properly dead backend
can sit on a job slot for a minute and a half. Which is a shame, given a dead
backend is exactly what you came here for. Three seconds is generous for a
healthy host and writes off a black-holed one in thirty seconds instead.

One thing to know before you point this at a thousand IPs: `--results` writes
three files per host (stdout, stderr, and a sequence marker), so you'll end up
with a few thousand files in `output/`. Perfectly fine, just maybe don't run it
straight from your home directory.

### Go easy on the job count

Worth remembering that this is a _timing_ tool. Crank `parallel_jobs` too high
and your probes start competing with each other for CPU and network on your own
machine, which shows up as inflated numbers that have nothing to do with the
servers you're measuring. Ten is a comfortable default. If you're tempted to go
much past that, it's usually better to let it take a bit longer.
