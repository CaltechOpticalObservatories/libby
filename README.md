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
extras: `units`, `description`, `nullable`, `validator`.

```python
from libby import Libby, BoolKeyword, FloatKeyword, TriggerKeyword

libby = Libby.rabbitmq(self_id="my-peer", rabbitmq_url="amqp://localhost")

state = {"position": 0.0}
libby.register_keywords([
    BoolKeyword("online", getter=lambda: True),
    FloatKeyword("position",
                 getter=lambda: state["position"],
                 setter=lambda v: state.update(position=v),
                 units="mm"),
    TriggerKeyword("halt", action=lambda: print("halted")),
])
```

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
