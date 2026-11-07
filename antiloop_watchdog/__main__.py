"""python -m antiloop_watchdog [--config PATH] [--check-config] [--print-token-path] [--version]"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .schema import ConfigError
from .service import Service


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="antiloop_watchdog", description="Anti-Loop Watchdog service")
    ap.add_argument("--config", help="path to watchdog.config.json (default: <project>/config/watchdog.config.json)")
    ap.add_argument("--base-dir", help="directory that relative paths in the config resolve against")
    ap.add_argument("--check-config", action="store_true", help="validate the configuration and exit")
    ap.add_argument("--print-token-path", action="store_true", help="print the API token file path and exit")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    try:
        svc = Service(config_path=args.config, base_dir=args.base_dir)
    except ConfigError as exc:
        print(f"configuration error: {exc.message}", file=sys.stderr)
        return 2
    if args.print_token_path:
        print(svc.token_path)
        return 0
    if args.check_config:
        print("configuration OK")
        for w in svc.cm.warnings:
            print("warning:", w)
        return 0
    api = svc.cm.data["api"]
    print(f"antiloop-watchdog {__version__} listening on http://{api['host']}:{api['port']} "
          f"(token file: {svc.token_path})", flush=True)
    svc.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
