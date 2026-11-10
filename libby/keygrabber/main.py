"""Console entry point for the keygrabber daemon.

Exits live here rather than in the daemon, so the daemon stays importable and
raises catchable exceptions.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Mapping
from typing import List, Optional

from .. import telemetry
from ..daemon import DEFAULT_LOG_FORMAT
from ..errors import LibbyError
from .daemon import KeygrabberDaemon


def main(argv: Optional[List[str]] = None) -> int:
    """Run the keygrabber until it is stopped. Return an exit code."""
    parser = argparse.ArgumentParser(
        prog="keygrabber",
        description="Poll libby keywords and write them to a time-series database",
    )
    parser.add_argument("-c", "--config", required=True,
                        help="path to the keygrabber config (YAML or JSON)")
    parser.add_argument("-d", "--daemon-id", default=None,
                        help="daemon id, for a subsystem config with several")
    namespace = parser.parse_args(argv)

    try:
        daemon = KeygrabberDaemon.from_config_file(namespace.config,
                                                   namespace.daemon_id)
        if not isinstance(daemon.get_config("logging"), Mapping):
            log_to_stderr()
        daemon.serve()
    except KeyboardInterrupt:
        return 0
    except (LibbyError, OSError, ValueError) as exc:
        print(f"keygrabber: {exc}", file=sys.stderr)
        return 1
    return 0


def log_to_stderr() -> None:
    """Send INFO and above to stderr, which systemd puts in the journal.

    Only for a config with no ``logging`` section. One with a section has
    already given the daemon its own handler, and a second one on the root
    logger would print every line twice.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(telemetry.TraceContextFormatter(DEFAULT_LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # pika logs each connection step at INFO, which the transport's
    # once-a-second reconnect loop would turn into a flood during an outage
    logging.getLogger("pika").setLevel(logging.WARNING)


if __name__ == "__main__":
    raise SystemExit(main())
