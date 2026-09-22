# Libby

Libby is a tiny messaging library built on [Bamboo](https://github.com/CaltechOpticalObservatories/bamboo)
with pluggable transports (ZMQ or RabbitMQ). It gives you:

- **Keywords** — typed, named values (`show` / `modify`) served over RPC, with
  a registry, auto-generated `keys.list` / `keys.describe` / `keys.read`
  services, and CLI coercion.
- **`LibbyDaemon`** — a base class for peers: lifecycle, RPC handlers, and
  pub/sub, in a few overrides.
- **`Client`** — a long-lived, in-process handle for reading and writing
  keywords from scripts, and for blocking on a keyword condition with
  `wait_for` (libby's `ktl.waitFor`).
- **Keygrabber**: a daemon that polls keywords from other peers and writes them
  to a time-series database for dashboarding.
- **`libby` CLI** — a command-line front end for keyword peers
  (`show` / `modify` / `list` / `describe` / `waitfor`), with TAB completion
  and wildcard listing to find which peers are up.

```{toctree}
:maxdepth: 2
:hidden:

installation
keywords
client
cli
daemon
keygrabber
api/index
```

## Where to start

- New to the package? Start with {doc}`installation`.
- Building a peer that serves keywords? Read {doc}`keywords` then {doc}`daemon`.
- Writing a script or tool that talks to peers? Read {doc}`client`.
- Poking at peers interactively? Read {doc}`cli`.
- Recording keywords for Grafana? Read {doc}`keygrabber`.
- Looking for a specific class or function? See {doc}`api/index`.
