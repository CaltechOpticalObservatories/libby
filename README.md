# Libby
Libby: a tiny messaging library which uses bamboo with a ZMQ transport

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
