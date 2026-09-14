# Libby

Libby is a tiny messaging library built on [Bamboo](https://github.com/CaltechOpticalObservatories/bamboo)
with pluggable transports (ZMQ or RabbitMQ). It gives you:

- **Keywords** — typed, named values (`show` / `modify`) served over RPC, with
  a registry, auto-generated `keys.list` / `keys.describe` services, and CLI
  coercion.
- **`LibbyDaemon`** — a base class for peers: lifecycle, discovery, RPC
  handlers, and pub/sub, in a few overrides.
- **`Client`** — a long-lived, in-process handle for reading and writing
  keywords from scripts, and for blocking on a keyword condition with
  `wait_for` (libby's `ktl.waitFor`).
- **`libby` CLI** — a command-line front end for keyword peers
  (`show` / `modify` / `list` / `describe` / `waitfor`).

```{toctree}
:maxdepth: 2
:hidden:

installation
keywords
client
cli
daemon
api/index
```

## Where to start

- New to the package? Start with {doc}`installation`.
- Building a peer that serves keywords? Read {doc}`keywords` then {doc}`daemon`.
- Writing a script or tool that talks to peers? Read {doc}`client`.
- Poking at peers interactively? Read {doc}`cli`.
- Looking for a specific class or function? See {doc}`api/index`.
