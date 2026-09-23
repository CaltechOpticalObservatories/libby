# Libby

Libby: a tiny messaging library which uses Bamboo with pluggable transports (ZMQ or RabbitMQ)

## Documentation

Full docs (installation, keywords, the `Client` library, the `libby` CLI, how
to build a `LibbyDaemon` peer, the keygrabber, plus the generated API
reference) are
built with Sphinx + the Shibuya theme and published to GitHub Pages:

**[caltechopticalobservatories.github.io/libby](https://caltechopticalobservatories.github.io/libby/)**

To build them locally:

```bash
tox -e docs
open docs/build/html/index.html
```

## Quick start

```bash
git clone https://github.com/CaltechOpticalObservatories/libby
cd libby
python -m venv venv
source venv/bin/activate
pip install -e .
```

See the [installation guide](docs/source/installation.md) for full setup
details, and the docs site above for everything else (keywords, the client
library, the CLI, and writing a `LibbyDaemon` peer).

## Finding peers

`libby list <group>.<daemon>` (and `Client.peers`) is how you find out which
peers are up. It broadcasts a `keys.list` that every peer answers, so it works
the same on both transports and needs nothing configured beyond the transport
itself.

Discovery, in the sense of a peer table you can ask who is alive, is not
implemented. It may be one day; until then use the broadcast above rather than
these:

- `Libby.peers_alive()` returns `{}` and `Libby.wait_for_peer()` always fails,
  on either transport, because `Protocol` never instantiates a `PeerTable`.
- ZMQ runs bamboo's hello, but only toward peers already in the sender's
  address book, and nothing consumes it for liveness. A client learns nothing
  about a daemon that has not been told about the client, so
  `Libby.knows_key()` stays False in the usual one-sided setup.
- `Libby.rabbitmq()` does not start hello at all. The broker routes messages;
  it does not tell a peer who else is connected.

`LibbyDaemon`'s `discovery_enabled` / `on_hello` control bamboo's hello, not
the broadcast above.

## Testing

```bash
python -m unittest discover -s tests
```

Most of `tests/` needs no transport at all. `tests/test_client_integration.py`
is the exception: it starts a real `LibbyDaemon` and exercises `Client`
against it, once per transport. The ZMQ cases need no external service and
always run; the RabbitMQ cases skip themselves if no broker is reachable at
`amqp://localhost`.
