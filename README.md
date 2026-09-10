# Libby

Libby: a tiny messaging library which uses Bamboo with pluggable transports (ZMQ or RabbitMQ)

## Documentation

Full docs (installation, keywords, the `Client` library, the `libby` CLI, and
how to build a `LibbyDaemon` peer, plus the generated API reference) are
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

## Testing

```bash
python -m unittest discover -s tests
```

Most of `tests/` needs no transport at all. `tests/test_client_integration.py`
is the exception: it starts a real `LibbyDaemon` over RabbitMQ and exercises
`Client` against it, and skips itself automatically if no broker is reachable
at `amqp://localhost`.
