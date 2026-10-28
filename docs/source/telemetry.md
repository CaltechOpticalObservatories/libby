# Telemetry

libby can export traces and metrics over OTLP to an
[OpenTelemetry](https://opentelemetry.io/) Collector. It is optional and off
by default: without the `otel` extra libby never imports OpenTelemetry, and
with it nothing is exported until an OTLP endpoint is set.

## Turning it on

```bash
pip install "libby[otel]"
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

Every `LibbyDaemon` then exports from `start()` until `stop()`, and the `libby`
CLI exports each command's trace. The CLI exports no metrics, since every run
would start new series. Exports use OTLP over HTTP and honour the standard
`OTEL_*` variables, e.g. `OTEL_TRACES_SAMPLER` or `OTEL_SDK_DISABLED=true`.

| Process | `service.name` | `service.namespace` |
|---|---|---|
| `LibbyDaemon` | wire id, e.g. `hsfei.adc` | `group_id` |
| `libby` CLI | `libby-cli` | |

An application that installs its own OpenTelemetry SDK can call
`libby.telemetry.use_current_providers()` to have libby record into it.

## What is recorded

**Traces.** Each request is a client span on the sender and a server span on
the handling peer, joined into one trace through the W3C `traceparent` that
bamboo carries in the envelope. A publish is a producer span, and each
subscriber's handling is a consumer span under it. A CLI command is one span
over all the requests it makes. Span names are the key or keyword, e.g.
`positionvalue`.

**Metrics.** Two histograms in seconds, which give request rate, errors and
duration per key:

| Metric | Attributes |
|---|---|
| `rpc.client.call.duration` | `rpc.method`, `server.address`, `error.type` |
| `rpc.server.call.duration` | `rpc.method`, `error.type` |

`error.type` is set only on failures: the bamboo status (`timeout`,
`no_key`, ...) for a request that got no answer, `error_response` for an
answer with `ok: false`, or the exception type.

**Logs.** libby does not export logs. A line logged while a span is active
gets ` trace_id=... span_id=...` appended, in the daemon's configured log
handler and the CLI's log file, so a Collector reading journald or the files
can link it to its trace.

## Cost

Off, each hook costs about 0.1 µs. On, a traced request adds roughly 15 µs on
the sender and 12 µs on the handler.
