"""Console entry point for the keygrabber daemon.

Exits live here rather than in the daemon, so the daemon stays importable and
raises catchable exceptions.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

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
        daemon.serve()
    except KeyboardInterrupt:
        return 0
    except (LibbyError, OSError, ValueError) as exc:
        print(f"keygrabber: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
