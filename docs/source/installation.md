# Installation

## Requirements

- Python 3.9 or higher
- `pip` (ideally the latest version)
- `setuptools` 68 or higher (for building the package)

## 1. Clone the repository

```bash
git clone https://github.com/CaltechOpticalObservatories/libby
cd libby
```

## 2. Set up a Python environment

```bash
python -m venv venv
source venv/bin/activate
```

## 3. Install build dependencies

```bash
pip install --upgrade pip setuptools wheel
```

## 4. Install the package

Editable installs are recommended for development — changes take effect
immediately without reinstalling:

```bash
pip install -e .
```

For the Cyclone DDS transport, install the `cyclonedds` extra. On Python 3.10
to 3.13 its wheel bundles the Cyclone C library:

```bash
pip install -e ".[cyclonedds]"
```

cyclonedds 11.0.1 has no Python 3.14 wheel, and its source build fails to
import on 3.14; the fix is on cyclonedds-python's master branch but not yet in
a release. Until one ships, the transport is not installable on 3.14.

To also pull in the docs toolchain (Sphinx + the Shibuya theme):

```bash
pip install -e ".[docs]"
```

For OpenTelemetry export (see {doc}`telemetry`):

```bash
pip install -e ".[otel]"
```

## Testing

```bash
python -m unittest discover -s tests
```

Most of `tests/` needs no transport at all. `tests/test_client_integration.py`
is the exception: it starts a real `LibbyDaemon` and exercises `Client`
against it, once per transport. The RabbitMQ cases skip themselves if no
broker is reachable at `amqp://localhost`, and the Cyclone DDS cases unless
the `cyclonedds` extra is installed.
