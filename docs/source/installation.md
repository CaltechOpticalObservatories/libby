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

To also pull in the docs toolchain (Sphinx + the Shibuya theme):

```bash
pip install -e ".[docs]"
```

## Testing

```bash
python -m unittest discover -s tests
```

Most of `tests/` needs no transport at all. `tests/test_client_integration.py`
is the exception: it starts a real `LibbyDaemon` over RabbitMQ and exercises
`Client` against it, and skips itself automatically if no broker is reachable
at `amqp://localhost`.
