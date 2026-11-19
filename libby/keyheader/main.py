"""Console entry point for the keyheader daemon.

Exits live here rather than in the daemon, so the daemon stays importable and
raises catchable exceptions.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from typing import List, Optional

from ..errors import LibbyError
from ..keygrabber.main import log_to_stderr
from .daemon import KeyheaderDaemon


def main(argv: Optional[List[str]] = None) -> int:
    """Run keyheader until it is stopped. Return an exit code."""
    parser = argparse.ArgumentParser(
        prog="keyheader",
        description="Answer FITS header segment requests from stored keyword values",
    )
    parser.add_argument("-c", "--config", required=True,
                        help="path to the keyheader config (YAML or JSON)")
    parser.add_argument("-d", "--daemon-id", default=None,
                        help="daemon id, for a subsystem config with several")
    namespace = parser.parse_args(argv)

    try:
        daemon = KeyheaderDaemon.from_config_file(namespace.config,
                                                  namespace.daemon_id)
        if not isinstance(daemon.get_config("logging"), Mapping):
            log_to_stderr()
        daemon.serve()
    except KeyboardInterrupt:
        return 0
    except (LibbyError, OSError, ValueError) as exc:
        print(f"keyheader: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
