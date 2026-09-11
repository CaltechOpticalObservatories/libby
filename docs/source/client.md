# Client library

`Client` is the programmatic front for reading and writing keywords — the
import-and-use counterpart to the CLI. Where the CLI opens a connection per
command, a `Client` holds one for its lifetime, so a script can touch many
keywords cheaply. It addresses keywords by the same qualified
`<group>.<scope>.<name>` and reuses the CLI's `cli_config.yaml`.

```python
from libby import Client

with Client.from_config() as client:          # transport/url from cli_config.yaml
    pos = client.get("hsfei.focpupsel.positionvalue")   # -> 7.15
    full = client.show("hsfei.focpupsel.positionvalue") # -> {"ok": True, "value": 7.15, "units": "mm", ...}
    client.set("hsfei.pickoff.softmax", 120)            # returns the applied value
```

Construct explicitly when you don't want config-file resolution:

```python
client = Client.rabbitmq(rabbitmq_url="amqp://user:pass@host")
client = Client.zmq(address_book={"hsfei_pickoff": "tcp://host:5555"})
```

- `get(name)` → the value; `show(name)` → the full response dict (value,
  units, flags); `set(name, value)` → the value the daemon applied.
- `wait_for(expression, timeout)` → blocks until a keyword satisfies a
  comparison; see below.
- Failures raise rather than return sentinels: `KeywordError` when the daemon
  rejects a get/set (its message is on `.error`), `LibbyTimeout` when a
  request isn't answered, both subclasses of `LibbyError`. `set` accepts
  `timeout_s=`; otherwise it honors the keyword's `timeout_s` metadata, like
  the CLI.

```python
from libby import KeywordError

try:
    client.get("hsfei.yjpiaagim.positionvaluex")
except KeywordError as ex:
    print(ex.error)        # "Control loops are not closed"
```

## Waiting on a keyword

`wait_for` blocks until a keyword satisfies a comparison, and returns whether it did.

```python
if client.wait_for("$hsfei.pickoff.positionvalue > 15", timeout=5):
    print("pickoff exceeded 15 within 5 seconds")

client.wait_for("$ismoving == false", 30, service="hsfei.pickoff")
```

`timeout` is seconds, and is the total time to wait; `None` (the default)
waits indefinitely and `0` evaluates once. `service` supplies a default
`<group>.<scope>` so the expression can name a keyword bare. `case=True`
compares strings exactly — by default they compare case-insensitively. `poll_s` sets the interval between reads (libby daemons don't broadcast
keyword changes, so `wait_for` polls) and `rpc_timeout_s` bounds each
individual read.

A malformed expression, or one whose two sides can't be compared at all,
raises `ExpressionError`; a read the daemon rejects (unknown or write-only
keyword) raises `KeywordError`, since waiting can't resolve it. A keyword with
no value yet counts as "not true yet" and the wait continues.

`wait_for_result(...)` takes the same arguments and returns a `WaitResult`
(`satisfied`, `keyword`, `value`, `elapsed_s`, `polls`) for callers that want
to report the value the wait settled on. See {mod}`libby.expression` for the
accepted expression syntax — currently one comparison between a
`$`-prefixed keyword and a literal.

Exact names only for now; `%` wildcard reads, `list`, and `describe` are
planned follow-ons — use the {doc}`CLI <cli>` for those today.

See {mod}`libby.client` in the {doc}`API reference </api/index>` for the
full method signatures.
