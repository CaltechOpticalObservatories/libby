# Keywords

A **keyword** is a typed named value served over libby, with a uniform
payload convention:

- `{}` → show (return current value)
- `{"value": V}` → modify (apply, then return it)

Types: `BoolKeyword`, `IntKeyword`, `FloatKeyword`, `StringKeyword`,
`TriggerKeyword`. Access mode is inferred — pass a `getter` for read-only, a
`setter` for write-only, both for read-write. Optional extras: `units`,
`description`, `nullable`, `validator`, `timeout_s` (advertised in
`keys.describe`; the CLI uses it to extend the modify timeout for slow
operations like motion).

Each `Libby` peer carries a `keyword_registry` with typed builder methods.
Build keywords by calling `lib.keyword_registry.<type>(...)`, then flush them
to the peer with `register_keywords`:

```python
from libby import Libby

libby = Libby.rabbitmq(self_id="my-peer", rabbitmq_url="amqp://localhost")

state = {"position": 0.0}
libby.keyword_registry.bool("online", getter=lambda: True)
libby.keyword_registry.float("position",
                             getter=lambda: state["position"],
                             setter=lambda v: state.update(position=v),
                             units="mm")
libby.keyword_registry.trigger("halt", action=lambda: print("halted"))

libby.register_keywords(libby.keyword_registry.drain())
```

You can also build keywords directly via `BoolKeyword(...)` /
`FloatKeyword(...)` etc. and pass a list to `register_keywords`. The registry
is a convenience layer over the same type classes.

Clients call the keyword by name:

```python
client = Libby.rabbitmq(self_id="client", rabbitmq_url="amqp://localhost")

client.rpc("my-peer", "position", {})               # show
client.rpc("my-peer", "position", {"value": 12.5})  # modify
client.rpc("my-peer", "halt", {"value": 1})         # fire
```

Two meta-services are auto-registered on every peer that uses the keyword
registry:

- `keys.list` — payload `{"pattern": "..."}` (default `"%"`) → names, sorted.
  `%` wildcards within a single name.
- `keys.describe` — payload `{"name": "..."}` → flat metadata dict. Exact
  lookup; no wildcards.

`LibbyDaemon` subclasses also get a `lasterror` keyword for free (not just
any keyword-registry user, since it needs the daemon's own logger): a
nullable string holding the most recent `self.logger.error(...)` message, so
a failure that only got logged locally is still visible to a remote
`libby show <peer>.lasterror`. Write `null` to clear it.

See {mod}`libby.keyword` and {mod}`libby.keyword_registry` in the
{doc}`API reference </api/index>` for the full type and method signatures.
