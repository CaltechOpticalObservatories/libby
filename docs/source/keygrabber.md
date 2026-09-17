# Keygrabber

The keygrabber is a libby daemon that polls keywords from other peers on a
configurable cadence and writes them to a time-series database, so Grafana can
dashboard an instrument without every daemon growing its own database code.

One process serves the whole fleet. Cadence lives in this daemon's config
rather than in each hardware daemon, adding a metric never restarts a daemon
that owns moving hardware, and the database credential lives in one place.

InfluxDB 2.x is the only backend today. It needs the optional extra:

```bash
pip install libby[influxdb]
```

## Running it

```bash
keygrabber -c /etc/hispec/keygrabber.yaml
```

It is an ordinary `LibbyDaemon`, so `SIGTERM` stops it cleanly and so does
writing its `shutdown` keyword. On the way out it gives its retry queue a
bounded chance to drain, so a graceful stop does not lose the last tick.

## Config

A keygrabber config is an ordinary daemon config (`peer_id`, `group_id`,
`transport`, and the transport's own settings) plus three sections of its own.

```yaml
peer_id: keygrabber
group_id: hispec
transport: rabbitmq
rabbitmq_url: amqp://localhost

sink:
  type: influxdb
  url: http://influx.hispec:8086
  org: hispec
  bucket: telemetry
  token_env: HISPEC_INFLUX_TOKEN

workers: 4

defaults:
  interval_s: 10.0
  timeout_s: 2.0
  refresh_s: 300.0

collections:
  adc:
    peer: hsfei.adc
    interval_s: 5.0
    keywords: ["positionvalue%", "ismoving", "isconnected"]
  pressure:
    peer: hsfei.atcpress
    interval_s: 60.0
    keywords: ["%"]
    exclude: ["units_code"]
```

### sink

`type` selects the backend; `influxdb` is the only one implemented.

The token is **never** written in the config. `token_env` names an environment
variable to read it from, and a `token` key in the file is rejected outright.
`timeout_ms` is optional.

### collections

One entry per peer and cadence; several entries may target the same peer at
different cadences. A collection name must match `[a-z0-9_]+`, because it
becomes the prefix of that collection's control keywords.

- `peer` is `<group>.<daemon>`, the address of one peer
- `keywords` is a list of names or `%` patterns to record
- `exclude` removes names the includes matched
- `interval_s`, `timeout_s` and `refresh_s` fall back to `defaults`

`uptime` and `lasterror` are excluded by default: `uptime` changes every second
and says nothing a timestamp does not, and `lasterror` is null most of the
time. Naming either one in `keywords` explicitly opts it back in.

Patterns are resolved against the live peer at startup and again every
`refresh_s`, so keywords added by a restarted daemon get picked up without
restarting the keygrabber.

### Cadence and timeouts

Config load rejects an `interval_s` at or below `1.5 x timeout_s`. bamboo waits
`timeout_s` for the acknowledgement and a further `timeout_s / 2` for the
reply, so one unanswered read can occupy a worker for one and a half timeouts,
and a tighter interval would be overrun by a single slow peer.

A tick whose predecessor is still running is skipped rather than queued behind
it, so a wedged peer cannot accumulate overlapping reads.

## Control keywords

The keygrabber is itself a peer, so its cadence and health are reachable with
the ordinary `libby` verbs, and it can feed the same dashboards it fills.

| Keyword | Type | Access | Meaning |
|---|---|---|---|
| `enabled` | bool | R/W | Collect on the configured cadences; false pauses without exiting |
| `isconnected` | bool | R/W | Last sink write succeeded; write true to request a reconnect |
| `pointswritten` | int | R | Samples the sink has stored since start |
| `readerrors` | int | R | Keyword reads that failed |
| `writeerrors` | int | R | Sink writes that failed |
| `queuedepth` | int | R | Batches waiting in the retry queue |
| `skippedticks` | int | R | Ticks skipped because the previous read was still in flight |
| `droppedbatches` | int | R | Batches discarded because a queue was full |
| `reload` | trigger | W | Re-read the config file and apply it |
| `shutdown` | trigger | W | Gracefully stop the daemon |
| `<collection>.enabled` | bool | R/W | Collect this one collection |
| `<collection>.interval` | float | R/W | Cadence in seconds |
| `<collection>.lastsample` | string | R | UTC time of the last successful tick |
| `<collection>.lag` | float | R | Seconds the last tick ran past its due time |

```bash
libby show   hispec.keygrabber.%
libby modify hispec.keygrabber.adc.interval=30
libby modify hispec.keygrabber.enabled=false
```

Per-collection keywords contain a dot, and `%` matches within a single segment
only, so `libby list hispec.keygrabber.%` will not show `adc.enabled`. Use
`hispec.keygrabber.%.%` for those.

`isconnected` reports whether the last write to the sink succeeded; it does not
ping the database, because these getters are answered on the transport's
receive thread and a blocking one would time out every read in flight. Writing
`true` asks the writer thread to reconnect and returns immediately, so poll the
keyword for the outcome. There is no manual disconnect.

### reload

`reload` re-reads the config file and applies it to the running collections,
so keyword selections and cadences can change without a restart. A file that
fails to parse leaves the running collections untouched and reports why, both
to the caller and on `lasterror`.

It will not add or remove collections, and says so rather than half-applying:
libby has no way to withdraw a keyword, so a new collection's control keywords
could not appear without a restart. Changing `sink` or `workers` also needs a
restart.

## How it reads

A tick is one `keys.read` request per peer, not one per keyword. This matters
more than the smaller number suggests: a daemon dispatches requests inline on
its receive thread, so reading twenty keywords individually does not overlap
anything on that daemon. It serializes exactly as a batch would, while paying
twenty dispatch cycles instead of one. The saving is contention on a control
daemon's only dispatch thread.

A peer running a libby without `keys.read` is read one keyword at a time
instead. That is detected from the `services` field of `keys.list`, not by
trying `keys.read` and seeing what happens: an unknown key is dropped without
an acknowledgement, so a probe cannot tell an old peer from a dead one.

Every value in a tick carries one timestamp, taken by the keygrabber rather
than by each daemon, which keeps clock skew between daemon hosts out of the
data.

## Storage

Samples reach the backend through a `Sink`, and nothing in a `Sample` is
Influx-shaped, so a second backend is a new sink rather than a change to the
collector.

The InfluxDB schema is one measurement per keyword name, tagged with `group`,
`peer` and `units`, with a single `value` field. A keyword name carries one
type across peers, so field types stay consistent, and a Grafana query is a
measurement plus a `peer` tag filter.

Three details follow from what Influx can store:

- Integers are written as floats, so one peer reporting `0` and another `0.5`
  for the same keyword cannot collide as int against float and be rejected.
- A null value is skipped, since Influx has no null field. It is not an error.
- A keyword with no units gets a `units=none` tag, because Influx drops an
  empty tag value and the keyword would otherwise split into two series.

A failed write goes to a bounded retry queue with exponential backoff, which
drops its oldest batch when full, so a database that stays down cannot grow the
daemon's memory without limit.

See {mod}`libby.keygrabber.sink` in the {doc}`API reference </api/index>` for
the sink contract.
