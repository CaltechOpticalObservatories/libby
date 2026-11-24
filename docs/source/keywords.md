# Keywords

A **keyword** is a typed named value served over libby, with a uniform
payload convention:

- `{}` → show (return current value)
- `{"value": V}` → modify (apply, then return it)

Types: `BoolKeyword`, `IntKeyword`, `FloatKeyword`, `StringKeyword`,
`DictKeyword`, `TriggerKeyword`. Access mode is inferred — pass a `getter`
for read-only, a `setter` for write-only, both for read-write. Optional
extras: `units`, `description`, `nullable`, `validator`, `timeout_s`
(advertised in `keys.describe`; the CLI uses it to extend the modify timeout
for slow operations like motion).

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

## Dict keywords

A `DictKeyword` carries a JSON object (a `dict` with string keys), for a
request that needs more than one value at once, such as a move with a target
and a speed. Give it a `fields` schema to have each modify checked before the
setter sees it:

```python
from libby import Field

libby.keyword_registry.dict(
    "move",
    setter=stage.move_to,
    fields={
        "x": Field("float", units="mm"),
        "y": Field("float", units="mm"),
        "speed": Field("float", units="mm/s", required=False,
                       description="Defaults to the stage maximum"),
    },
    timeout_s=30.0,
    description="Move to (x, y).",
)
```

A field is a type name (`"bool"`, `"int"`, `"float"`, `"string"`, or `"any"`
for an unchecked JSON value) or a `Field`, which adds `units`, `description`,
`required` (default true) and `nullable` (default false). With a schema, a
modify:

- may name only the schema's fields, and must include every required one;
- must give each field a value of its type, by the same rules as the scalar
  keywords: an `int` field rejects `true`, and a `float` field accepts `2` and
  casts it to `2.0`;
- passes an optional field it leaves out to the setter as absent, not `None`.

```python
client.rpc("my-peer", "move", {"value": {"x": 1.5, "y": 2.0, "speed": 5}})  # ok
client.rpc("my-peer", "move", {"value": {"x": 1.5}})
# {"ok": False, "error": "missing required field 'y'"}
client.rpc("my-peer", "move", {"value": {"x": 1.5, "y": 2, "z": 0}})
# {"ok": False, "error": "unknown field 'z'; expected 'x', 'y', 'speed'"}
client.rpc("my-peer", "move", {"value": {"x": 1.5, "y": "2"}})
# {"ok": False, "error": "field 'y': value must be a number"}
```

A `validator` runs after the schema checks, for rules that span fields, such
as "the target must be inside the travel range". Without `fields`, any dict is
accepted and the validator is the only check. Fields are flat: use `"any"` for
a nested value. The setter always receives a new dict, never the sent object.

`keys.describe` advertises the schema under `fields`, and `libby describe`
prints it:

```
$ libby describe hsfei.stage.move
hsfei.stage.move:
  type         dict
  readonly     False
  writeonly    True
  nullable     False
  timeout_s    30.0
  description  Move to (x, y).
  fields:
    x      float  required  mm
    y      float  required  mm
    speed  float  optional  mm/s  Defaults to the stage maximum
```

The keygrabber skips dict values: neither sink has a single field to store
one in.

## Calling keywords

Clients call the keyword by name:

```python
client = Libby.rabbitmq(self_id="client", rabbitmq_url="amqp://localhost")

client.rpc("my-peer", "position", {})               # show
client.rpc("my-peer", "position", {"value": 12.5})  # modify
client.rpc("my-peer", "halt", {"value": 1})         # fire
```

Three meta-services are auto-registered on every peer that uses the keyword
registry:

- `keys.list` — payload `{"pattern": "..."}` (default `"%"`) → `matches`,
  names sorted, plus `services` (below). `%` wildcards within a single name.
- `keys.describe` — payload `{"name": "..."}` → flat metadata dict. Exact
  lookup; no wildcards.
- `keys.read` — payload `{"names": [...]}` or `{"pattern": "..."}` → `values`,
  a map of name to that keyword's own show response. Reads a whole peer in one
  request.

`keys.read` exists because a daemon answers requests one at a time, inline on
its receive thread. Reading twenty keywords individually does not overlap
anything on that daemon; it serializes exactly as a batch would, while paying
twenty dispatch cycles instead of one. Batching matters most for a poller that
must not crowd out an operator or a control command.

A failing getter is reported inside `values` as
`{"ok": false, "error": "..."}`, so one broken keyword costs only itself.
Pattern selection skips write-only keywords, which have nothing to show;
naming one explicitly still answers with its error.

`keys.list` also reports `services`: the non-keyword keys this peer answers,
including the `keys.*` meta-services and any RPC service a daemon registered
itself. It is how a caller tells "this peer has no `keys.read`" from "this
peer did not answer", because an unknown key is dropped without an ACK and so
probing for one is indistinguishable from a timeout. A peer running an older
libby omits the field, which is the negative signal.

`LibbyDaemon` subclasses also get a `lasterror` keyword for free (not just
any keyword-registry user, since it needs the daemon's own logger): a
nullable string holding the most recent `self.logger.error(...)` message, so
a failure that only got logged locally is still visible to a remote
`libby show <peer>.lasterror`. Write `null` to clear it.

See {mod}`libby.keyword` and {mod}`libby.keyword_registry` in the
{doc}`API reference </api/index>` for the full type and method signatures.
