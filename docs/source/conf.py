# Configuration file for the Sphinx documentation builder.
#
# https://www.sphinx-doc.org/en/master/usage/configuration.html

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version as _pkg_version

# Make the `libby` package importable for autodoc without an editable install
# (the docs workflow does `pip install -e .[docs]`, but this keeps `make html`
# working from a plain checkout too).
sys.path.insert(0, os.path.abspath("../.."))

# -- Project information -----------------------------------------------------

project = "Libby"
copyright = "2026, Prakriti Gupta, Jeb Bailey, Michael Langmayr"
author = "Prakriti Gupta, Jeb Bailey, Michael Langmayr"

try:
    release = _pkg_version("libby")
except PackageNotFoundError:
    release = "0.1.0"
version = release

# -- General configuration ----------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "fieldlist",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"

napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- Options for HTML output --------------------------------------------------

html_theme = "shibuya"
html_static_path = ["_static"]
html_title = "Libby"

html_theme_options = {
    "accent_color": "indigo",
    "github_url": "https://github.com/CaltechOpticalObservatories/libby",
    "nav_links": [
        {"title": "Guide", "url": "installation"},
        {"title": "API Reference", "url": "api/index"},
    ],
}
