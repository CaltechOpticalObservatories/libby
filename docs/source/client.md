# Client library

`Client` is the programmatic front for reading and writing keywords — the
import-and-use counterpart to the CLI. Where the CLI opens a connection per
command, a `Client` holds one for its lifetime, so a script can touch many
keywords cheaply. It addresses keywords by the same qualified
`<group>.<daemon>.<keyword>` and reuses the CLI's `cli_config.yaml`.

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
- `list(pattern)` → matching qualified names; `peers(pattern)` → the live
  daemons; `describe(name)` → one keyword's metadata; `read(names)` → many
  keywords in one request per peer; see below.
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

client.wait_for("$ismoving == false", 30, daemon="hsfei.pickoff")
```

`timeout` is seconds, and is the total time to wait; `None` (the default)
waits indefinitely and `0` evaluates once. `daemon` supplies a default
`<group>.<daemon>` so the expression can name a keyword bare. `case=True`
compares strings exactly — by default they compare case-insensitively. `poll_s` sets the interval between reads (libby daemons don't broadcast
keyword changes, so `wait_for` polls) and `rpc_timeout_s` bounds each
individual read.

A malformed expression, or one whose two sides can't be compared at all,
raises `ExpressionError`; a read the daemon rejects (unknown or write-only
keyword) raises `KeywordError`, since waiting can't resolve it. A keyword with
no value yet counts as "not true yet" and the wait continues.

`wait_for_result(...)` takes the same arguments and returns a `WaitResult`
(`satisfied`, `address`, `value`, `elapsed_s`, `polls`) for callers that want
to report the value the wait settled on. See {mod}`libby.expression` for the
accepted expression syntax — currently one comparison between a
`$`-prefixed keyword and a literal.

## Listing, describing and bulk reads

`list` returns fully qualified names, so its result feeds straight back into
`get`, `show` or `read`:

```python
names = client.list("hsfei.pickoff.is%")   # ["hsfei.pickoff.isconnected", ...]
meta = client.describe("hsfei.pickoff.positionvalue")
meta["type"], meta["units"]                # ("float", "mm")
```

`read` takes many names and issues one request per peer rather than one per
keyword, which is what a poller should use:

```python
values = client.read(names)
# {"hsfei.pickoff.isconnected": {"ok": True, "value": True}, ...}
```

Unlike `get` and `set`, `read` never raises for a failed read. Every requested
name maps to its own response, so one dead peer or one broken getter costs
only its own entries. Names may span peers; each peer is asked separately.
Long name lists are split into bounded requests (`chunk_size=`) and merged.

`listing` returns both the matching names and the peer's `services` from a
single `keys.list` response, for a caller that needs to know whether bulk
reads are available:

```python
listing = client.listing("hsfei.pickoff.%")
if "keys.read" in listing.services:
    values = client.read(list(listing.names))
else:                                  # peer on an older libby
    values = {n: client.show(n) for n in listing.names}
```

Checking `services` is the only reliable test: `Libby.knows_key` reads the
discovery registry, which stays empty without discovery, and calling
`keys.read` to see what happens cannot distinguish an old peer from a dead
one.

## Finding daemons

`peers` returns the live daemons matching a `<group>.<daemon>` pattern, and
`peer_listings` returns each one's keywords from the same round trip:

```python
client.peers("hsfei.%")              # ["hsfei.adc", "hsfei.atcpress", ...]
client.peers()                       # every daemon, any group
client.peer_listings("hsfei.%")      # {"hsfei.adc": ["isconnected", ...], ...}
```

`list` accepts the same wildcards in its group and daemon segments, so
`client.list("hsfei.%.isconnected")` reads across the group.

These broadcast a single `keys.list`, which every daemon answers. Nothing
says how many daemons exist, so they wait the full `timeout_s` (1s by
default) rather than returning on the first reply, and a daemon that is down
is simply absent. Over ZMQ the broadcast reaches only the daemons in the
address book; over RabbitMQ the broker reaches all of them.

Only daemons come back. Every `Libby` serves `keys.list`, so another
`Client` answers the broadcast as well, but `LibbyDaemon` is the only thing
that builds its `Libby` with `is_daemon=True` and the rest are filtered out.
A peer running a libby from before that flag omits it and is still listed,
so this does not hide daemons that have yet to be redeployed.

See {mod}`libby.client` in the {doc}`API reference </api/index>` for the
full method signatures.
