"""The telemetry backend libby exports to: pinned binaries, configs and units.

Nothing here is instrument-specific. An installer such as instrumentctl
downloads each of :data:`RELEASES`, checks it against its SHA-256, and renders
each of :data:`SERVICES` with :func:`render`, supplying every name in
:data:`PLACEHOLDERS`. The backend is an OpenTelemetry Collector receiving OTLP
and fanning out to Prometheus (metrics), Tempo (traces) and Loki (logs), each a
plain systemd service.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Mapping, Tuple

_GITHUB = "https://github.com"


@dataclass(frozen=True)
class Release:
    """One upstream binary, pinned to a version and the archive's SHA-256."""

    binary: str
    version: str
    url: str
    sha256: str
    # Path of the binary inside the archive
    member: str


# linux/amd64 builds, checked against each project's published checksums
RELEASES: Tuple[Release, ...] = (
    Release(
        binary="otelcol-contrib",
        version="0.164.0",
        url=f"{_GITHUB}/open-telemetry/opentelemetry-collector-releases/releases/download/"
            "v0.164.0/otelcol-contrib_0.164.0_linux_amd64.tar.gz",
        sha256="f0c1ce9f661399beb4a58b711924716e08ea70c3802db0715fcfde3cbbada2d4",
        member="otelcol-contrib",
    ),
    Release(
        binary="prometheus",
        version="3.13.5",
        url=f"{_GITHUB}/prometheus/prometheus/releases/download/"
            "v3.13.5/prometheus-3.13.5.linux-amd64.tar.gz",
        sha256="0388822ace519c5a3a8ef556a541ce1b536e21d872f82deded465bd27a5a0699",
        member="prometheus-3.13.5.linux-amd64/prometheus",
    ),
    Release(
        binary="tempo",
        version="3.0.0",
        url=f"{_GITHUB}/grafana/tempo/releases/download/v3.0.0/tempo_3.0.0_linux_amd64.tar.gz",
        sha256="f4150470a5f6bf57e94dac5392c552b488932ea9e88a6ee2a75d7b969b7cb6a4",
        member="tempo",
    ),
    Release(
        binary="loki",
        version="3.7.9",
        url=f"{_GITHUB}/grafana/loki/releases/download/v3.7.9/loki-linux-amd64.zip",
        sha256="17a5fae28ce733b3560c27c76620920b169bd2aea9473947b733b511f92e81dd",
        member="loki-linux-amd64",
    ),
)

# Each has a <service>.yaml config and a <service>.service unit template
SERVICES: Tuple[str, ...] = ("otelcol", "prometheus", "tempo", "loki")

PLACEHOLDERS = frozenset({
    "name",               # prefix of each unit, e.g. hispec for hispec-prometheus
    "display_name",       # as the units read in systemctl status
    "user",               # account the services run as
    "bin",                # directory holding the RELEASES binaries
    "etc",                # directory holding the rendered configs
    "data",               # directory each service keeps its data under
    "otlp_bind",          # address the Collector receives OTLP on
    "query_bind",         # address Prometheus, Tempo and Loki answer Grafana on
    "metrics_retention",  # Prometheus retention, e.g. 5y
    "trace_retention",    # Tempo retention, e.g. 336h
})

_PLACEHOLDER = re.compile(r"@([a-z_]+)@")


def render(template: str, values: Mapping[str, str]) -> str:
    """Fill ``template``'s ``@placeholder@`` values.

    Raise ``KeyError`` naming any placeholder ``values`` lacks, so a missing
    one fails the install rather than reaching a host as a literal ``@data@``.
    """
    text = resources.files(__package__).joinpath("templates", template).read_text(
        encoding="utf-8")
    missing = sorted(set(_PLACEHOLDER.findall(text)) - set(values))
    if missing:
        raise KeyError(f"{template} needs {', '.join(missing)}")
    return _PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), text)
