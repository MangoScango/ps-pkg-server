"""CLI: scan directories for PS4 PKGs and print metadata.

Usage:
  python -m pkgtool <dir> [<dir> ...] [--json] [--icons <dir>]
"""

from __future__ import annotations

import argparse
import json
import sys

from .scan import scan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan directories for PS4 PKG metadata.")
    ap.add_argument("paths", nargs="+", help="directories or .pkg files to scan")
    ap.add_argument("--json", action="store_true", help="output JSON instead of a table")
    ap.add_argument("--icons", metavar="DIR", help="extract icons to this directory")
    ap.add_argument("--workers", type=int, default=8, help="parallel parse workers")
    args = ap.parse_args(argv)

    result = scan(args.paths, icon_dir=args.icons, workers=args.workers)

    if args.json:
        print(
            json.dumps(
                {
                    "total": result.total,
                    "records": [r.to_dict() for r in result.records],
                    "errors": [r.to_dict() for r in result.errors],
                },
                indent=2,
            )
        )
        return 0

    for r in result.records:
        mtag = f" [{r.marriage[:8]}]" if r.marriage else ""
        print(f"{r.platform or '?':<4} {r.edition or '?':<7} {r.title_id or '?':<12} {r.version or '?':<8} {r.kind:<10} {r.region:<5} {r.title or r.filename}{mtag}")
    print(f"\n{len(result.records)} package(s), {len(result.errors)} error(s)")
    for e in result.errors:
        print(f"  ERROR {e.filename}: {e.error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
