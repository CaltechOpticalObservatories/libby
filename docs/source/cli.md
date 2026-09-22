# CLI

`libby` is the command-line front for keyword peers. Verbs:

```
libby show     <group>.<daemon>.<keyword>         # read a keyword (% wildcard in keyword)
libby modify   <group>.<daemon>.<keyword>=V       # write a keyword (exact keyword)
libby list     <group>.<daemon>.<pattern>         # list keyword names (% wildcard in any segment)
libby list     <group>.<daemon>                   # list live daemons (% wildcard in any segment)
libby describe <group>.<daemon>.<keyword>         # metadata for one keyword (exact keyword)
libby waitfor  '$<group>.<daemon>.<keyword> > V'  # block until a comparison holds
libby completion bash|zsh                         # print shell code for TAB completion
```

`<group>.<daemon>` is the address of one daemon: `group` is its `group_id`
and `daemon` is its `peer_id` (e.g. `peer_id: adc, group_id: hsfei` in the
daemon's config is addressed as `hsfei.adc`). `Libby.rabbitmq()` /
`Libby.zmq()` build the actual wire identity from those two fields via
`libby.naming.qualified_peer_id`, so a daemon config never needs to
concatenate them by hand. `group`/`daemon` are case-insensitive (`HSFEI.ADC`
and `hsfei.adc` reach the same daemon); the keyword isn't. `list` is the one
verb that spans daemons; `req` and `sub` are kept for raw RPC / topic
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

$ libby list hsfei.%            # which daemons are up?
hsfei.adc
hsfei.atcpress
hsfei.pickoff

$ libby list %.%.isconnected    # one keyword across the whole fleet
hscal.hkettherm.isconnected
hsfei.adc.isconnected
hsfei.atcpress.isconnected

$ libby waitfor '$hsfei.pickoff.ismoving == false' --timeout 30
hsfei.pickoff.ismoving = False (satisfied after 4.2s)

$ libby waitfor '$hsfei.pickoff.positionvalue > 15' --timeout 5    # exit code 4
libby: $hsfei.pickoff.positionvalue > 15: still false after 5.0s; hsfei.pickoff.positionvalue = 11.0
```

Add `--json` to any verb for machine-readable output (objects for `show` /
`modify` / `describe`, list of objects for `show <pattern>`, list of strings
for `list`).

## Listing across daemons

A `%` in the `<group>` or `<daemon>` segment of `list` asks every reachable
daemon at once, rather than one named daemon. Drop the keyword segment
(`libby list hsfei.%`) to list the daemons themselves; keep it
(`libby list hsfei.%.is%`) to list matching keywords on each of them.

Nothing on the wire says how many daemons exist, so these always run for the
full timeout (default 1s, `--timeout` to change it) instead of returning on
the first answer. A daemon that is down simply doesn't appear. Exit code 3
means nothing answered.

Over ZMQ the broadcast only reaches daemons in the address book (`peers:` in
`cli_config.yaml`, or `--addr`); over RabbitMQ the broker reaches everyone.

## Completion

`libby completion bash` (or `zsh`) prints shell code that wires TAB
completion for verbs, flags and addresses. Add it to your shell rc:

```bash
eval "$(libby completion bash)"
```

Completing an address discovers live daemons the same way `list` does, so
TAB offers daemons after `<group>.` and that daemon's keywords after
`<group>.<daemon>.`. Results are cached for 10s in
`~/.libby/completion_cache.json` so a burst of TABs costs one broadcast, and
discovery is bounded at 0.5s so TAB never hangs. An unreachable broker
completes nothing rather than erroring.

## Modify syntax

- `key=value` or `key value` (positional) both work.
- Empty (`key=`) and `null` clear nullable values.
- Coercion is heuristic: `true` / `false` → bool, integer-looking → int,
  decimal-looking → float, else string.
- The CLI consults `keys.describe` for the keyword's `timeout_s` metadata
  before sending the modify, so slow operations (e.g. stage motion) get a
  longer wait automatically. `--timeout <s>` overrides.

## waitfor

`waitfor` blocks until a keyword
satisfies a comparison. Because libby daemons don't broadcast keyword changes,
it polls the keyword over RPC rather than waiting on a monitor — `--poll` sets
the interval (default 0.1s).

```
libby waitfor '$<group>.<daemon>.<keyword> <op> <value>'
```

- `<op>` is one of `==`, `!=`, `<`, `<=`, `>`, `>=`. Quote the whole
  expression so your shell doesn't eat the `$`, `<`, or `>`.
- Keyword references are `$`-prefixed, as in KTL. `-d/--daemon
  <group>.<daemon>` sets a default daemon so the expression can name a
  keyword bare: `libby waitfor '$ismoving == false' -d hsfei.pickoff`.
- KTL writes conditions parenthesized (`'($foo.BAR > 15)'`); that form works
  too, and so does putting the keyword on the right (`'15 < $foo.bar.baz'`).
- Values coerce like a `modify` value — `false` is a bool, `15` an int,
  `null` is `None`. Quote to keep a value a string: `'$status == "15"'`.
  A value only *needs* quoting if it contains whitespace, starts with `$`, or
  looks like an operator.
- String comparisons are case-insensitive, matching `ktl.waitFor`'s
  `case=False` default. `--case` compares exactly.
- `--timeout` here is the **total** time to wait, not the per-request RPC
  timeout, and it defaults to waiting indefinitely. Exit code is 0 if the
  comparison came true, 4 if the timeout expired with it still false.
- A keyword that has no value yet (a nullable one the daemon hasn't populated)
  counts as "not true yet" and the wait continues, as in KTL. A rejected read
  — unknown or write-only keyword — fails immediately, since no amount of
  waiting resolves it.

`and` / `or` / `not`, arithmetic, and multi-keyword expressions are not supported yet.

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
| 4 | `waitfor` timed out with the comparison still false |
