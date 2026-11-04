"""Tests for the telemetry backend's pinned releases and templates."""
from __future__ import annotations

import re
import unittest
from importlib import resources

import yaml

from libby import telemetry_backend as backend

VALUES = {
    "name": "demo",
    "display_name": "Demo",
    "user": "demo",
    "bin": "/opt/demo/telemetry/bin",
    "etc": "/etc/demo/telemetry",
    "data": "/var/lib/demo/telemetry",
    "otlp_bind": "0.0.0.0",
    "query_bind": "127.0.0.1",
    "metrics_retention": "5y",
    "trace_retention": "336h",
}
PLACEHOLDER = re.compile(r"@([a-z_]+)@")


def _templates():
    return [f"{service}{suffix}" for service in backend.SERVICES
            for suffix in (".yaml", ".service")]


class TemplateTests(unittest.TestCase):
    """Every template renders completely from PLACEHOLDERS."""

    def test_placeholders_lists_exactly_what_the_templates_use(self):
        """Keep the documented placeholder set honest."""
        folder = resources.files(backend.__name__).joinpath("templates")
        used = {name for template in _templates()
                for name in PLACEHOLDER.findall(folder.joinpath(template).read_text())}
        self.assertEqual(used, set(backend.PLACEHOLDERS))

    def test_every_template_renders_without_leftovers(self):
        """Leave no @placeholder@ behind for a host to read literally."""
        for template in _templates():
            with self.subTest(template):
                self.assertIsNone(PLACEHOLDER.search(backend.render(template, VALUES)))

    def test_configs_are_valid_yaml(self):
        """Parse each rendered config."""
        for service in backend.SERVICES:
            with self.subTest(service):
                self.assertIsInstance(yaml.safe_load(backend.render(f"{service}.yaml", VALUES)),
                                      dict)

    def test_a_missing_value_is_named(self):
        """Fail the render rather than write a half-filled file."""
        partial = {key: value for key, value in VALUES.items() if key != "data"}
        with self.assertRaisesRegex(KeyError, "data"):
            backend.render("tempo.yaml", partial)

    def test_units_run_the_pinned_binaries(self):
        """Point each unit at a binary RELEASES provides."""
        binaries = {release.binary for release in backend.RELEASES}
        for service in backend.SERVICES:
            with self.subTest(service):
                unit = backend.render(f"{service}.service", VALUES)
                started = re.search(r"^ExecStart=/opt/demo/telemetry/bin/(\S+)", unit, re.M)
                self.assertIn(started.group(1), binaries)


class ReleaseTests(unittest.TestCase):
    """Pinned releases are fetched over HTTPS and checked by SHA-256."""

    def test_each_release_is_https_with_a_sha256(self):
        """Refuse anything an installer could not verify."""
        for release in backend.RELEASES:
            with self.subTest(release.binary):
                self.assertTrue(release.url.startswith("https://github.com/"))
                self.assertRegex(release.sha256, r"^[0-9a-f]{64}$")
                self.assertIn(release.version, release.url)


if __name__ == "__main__":
    unittest.main()
