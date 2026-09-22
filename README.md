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

Bamboo's own hello/discovery is a separate mechanism and is not a way to find
peers today:

- `Protocol` never instantiates a `PeerTable`, so `Libby.peers_alive()` returns
  `{}` and `Libby.wait_for_peer()` always fails, on either transport.
- `Libby.rabbitmq()` doesn't start discovery at all. The broker routes messages;
  it does not tell a peer who else is connected.
- On ZMQ a hello only reaches peers already in the sender's address book, so a
  client learns nothing about a daemon that hasn't been told about the client.
  `Libby.knows_key()` stays False in the usual one-sided setup.

`LibbyDaemon`'s `discovery_enabled` / `on_hello` control that mechanism, not
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
