# Libby
Libby: a tiny messaging library which uses Bamboo with pluggable transports (ZMQ or RabbitMQ)

## Installation

To install the package in editable mode (ideal for development), follow these steps:

### Requirements

- Python 3.7 or higher
- `pip` (ensure it's the latest version)
- `setuptools` 42 or higher (for building the package)

### 1. Clone the repository

First, clone the repository to your local machine:

```bash
git https://github.com/CaltechOpticalObservatories/libby
cd libby
```

### 2. Set Up Your Python Environment

Create a virtual environment for your package:

```bash
python -m venv venv
source venv/bin/activate
```

### 3. Install Build Dependencies

Make sure setuptools and pip are up to date:

```bash
pip install --upgrade pip setuptools wheel
```


## Installing Dependencies

To install your package in editable mode for development, use the following command:

```bash
pip install -e .
```

This will install the package, allowing you to edit it directly and have changes take effect immediately without reinstalling.

To install any optional dependencies, such as development dependencies, use:

```bash
pip install -e .[dev]
```

## Keywords

A **keyword** is a typed named value served over libby, with a uniform payload convention:

- `{}` → show (return current value)
- `{"value": V}` → modify (apply, then return it)

Types: `BoolKeyword`, `IntKeyword`, `FloatKeyword`, `StringKeyword`,
`TriggerKeyword`. Access mode is inferred — pass a `getter` for
read-only, a `setter` for write-only, both for read-write. Optional
extras: `units`, `description`, `nullable`, `validator`, `timeout_s`
(advertised in `keys.describe`; the CLI uses it to extend the modify
timeout for slow operations like motion).

Each `Libby` peer carries a `keyword_registry` with typed builder
methods. Build keywords by calling `lib.keyword_registry.<type>(...)`,
then flush them to the peer with `register_keywords`:

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
`FloatKeyword(...)` etc. and pass a list to `register_keywords`. The
registry is a convenience layer over the same type classes.

Clients call the keyword by name:

```python
client = Libby.rabbitmq(self_id="client", rabbitmq_url="amqp://localhost")

client.rpc("my-peer", "position", {})               # show
client.rpc("my-peer", "position", {"value": 12.5})  # modify
client.rpc("my-peer", "halt", {"value": 1})         # fire
```

Two meta-services are auto-registered on every peer that uses the
keyword registry:

- `keys.list` — payload `{"pattern": "..."}` (default `"%"`) → names,
  sorted. `%` wildcards within a single name.
- `keys.describe` — payload `{"name": "..."}` → flat metadata dict.
  Exact lookup; no wildcards.

## CLI

`libby` is the command-line front for keyword peers. Verbs:

```
libby show     <group>.<scope>.<name>     # read a keyword (% wildcard in name)
libby modify   <group>.<scope>.<name>=V   # write a keyword (exact name)
libby list     <group>.<scope>.<pattern>  # list keyword names (% wildcard in name)
libby describe <group>.<scope>.<name>     # metadata for one keyword (exact name)
```

`<group>.<scope>` is the address of one peer (`peer_id` =
`<group>_<scope>`). Cross-peer fanout is not supported. `req` and `sub`
are kept for raw RPC / topic debugging.

### Examples

```
$ libby show hsfei.pickoff.positionvalue
hsfei.pickoff.positionvalue = 79.0 mm

$ libby show hsfei.pickoff.is%
hsfei.pickoff.isconnected   = True
hsfei.pickoff.isloopclosed  = True
hsfei.pickoff.ismoving      = False
hsfei.pickoff.isreferenced  = True

$ libby modify hsfei.pickoff.softmax=120
hsfei.pickoff.softmax = 120.0 mm

$ libby modify hsfei.pickoff.softmax=null   # or hsfei.pickoff.softmax=
hsfei.pickoff.softmax = None mm

$ libby describe hsfei.pickoff.positionvalue
hsfei.pickoff.positionvalue:
  type         float
  readonly     False
  writeonly    False
  nullable     False
  units        mm
  description  Stage position in engineering units.

$ libby list hsfei.pickoff.%min
hsfei.pickoff.hardmin
hsfei.pickoff.softmin
```

Add `--json` to any verb for machine-readable output (objects for
`show` / `modify` / `describe`, list of objects for `show <pattern>`,
list of strings for `list`).

### Modify syntax

- `key=value` or `key value` (positional) both work.
- Empty (`key=`) and `null` clear nullable values.
- Coercion is heuristic: `true` / `false` → bool, integer-looking →
  int, decimal-looking → float, else string.
- The CLI consults `keys.describe` for the keyword's `timeout_s`
  metadata before sending the modify, so slow operations (e.g. stage
  motion) get a longer wait automatically. `--timeout <s>` overrides.

### Config

The CLI looks for `~/.libby/cli_config.yaml` by default; override the
path per call with `--config <path>`. An example template ships with
the package at `libby/cli/cli_config.example.yaml` — copy it and
edit:

```bash
mkdir -p ~/.libby
cp $(python -c "import libby.cli, os; print(os.path.dirname(libby.cli.__file__))")/cli_config.example.yaml ~/.libby/cli_config.yaml
```

Schema:

```yaml
transport: rabbitmq          # zmq | rabbitmq
rabbitmq_url: amqp://localhost

# Used only when transport=zmq:
peers:
  hsfei_pickoff: tcp://hispec.caltech.edu:5555
```

All keys are optional. Missing file is fine — defaults are
`transport: rabbitmq` / `rabbitmq_url: amqp://localhost`.

Precedence: `--transport` / `--rabbitmq-url` flags override yaml; yaml
overrides built-in defaults. Flags must appear *after* the subcommand
(`libby show --transport zmq foo`, not the other way).

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | argument / parse error |
| 2 | RPC or response error (e.g. read-only, unknown keyword, transport failure) |
| 3 | wildcard `list` / `show` matched no keywords |
