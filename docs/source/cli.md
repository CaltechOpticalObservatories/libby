# CLI

`libby` is the command-line front for keyword peers. Verbs:

```
libby show     <group>.<scope>.<name>     # read a keyword (% wildcard in name)
libby modify   <group>.<scope>.<name>=V   # write a keyword (exact name)
libby list     <group>.<scope>.<pattern>  # list keyword names (% wildcard in name)
libby describe <group>.<scope>.<name>     # metadata for one keyword (exact name)
```

`<group>.<scope>` is the address of one peer: `group` is that peer's
`group_id`, `scope` is its `peer_id` (e.g. `peer_id: adc, group_id: hsfei` in
the daemon's config is addressed as `hsfei.adc`). `Libby.rabbitmq()` /
`Libby.zmq()` build the actual wire identity from those two fields via
`libby.naming.qualified_peer_id`, so a daemon config never needs to
concatenate them by hand. `group`/`scope` are case-insensitive (`HSFEI.ADC`
and `hsfei.adc` reach the same peer); the keyword name isn't. Cross-peer
fanout is not supported. `req` and `sub` are kept for raw RPC / topic
debugging.

## Examples

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

Add `--json` to any verb for machine-readable output (objects for `show` /
`modify` / `describe`, list of objects for `show <pattern>`, list of strings
for `list`).

## Modify syntax

- `key=value` or `key value` (positional) both work.
- Empty (`key=`) and `null` clear nullable values.
- Coercion is heuristic: `true` / `false` → bool, integer-looking → int,
  decimal-looking → float, else string.
- The CLI consults `keys.describe` for the keyword's `timeout_s` metadata
  before sending the modify, so slow operations (e.g. stage motion) get a
  longer wait automatically. `--timeout <s>` overrides.

## Config

The CLI looks for `~/.libby/cli_config.yaml` by default; override the path
per call with `--config <path>`. An example template ships with the package
at `libby/cli/cli_config.example.yaml` — copy it and edit:

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

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success |
| 1 | argument / parse error |
| 2 | RPC or response error (e.g. read-only, unknown keyword, transport failure) |
| 3 | wildcard `list` / `show` matched no keywords |
