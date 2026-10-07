"""An example mod: send what a decoder prints to the agent (docs/MODS.md).

Pipe a decoder that prints one JSON object per line into it, and name
the fields to use::

    my-decoder --json | python mod_stdin.py --mod lightning --id-field strike_id \
        --keep distance_km,energy --heard-here

Each line becomes one point, sent to the agent's `custom` input over UDP.
A line that is not JSON, or has no id, is counted and skipped. It needs
only the Python standard library; with `rf-agent` installed it checks
each point with the same code the agent uses before it sends it.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from typing import Any

try:
    from rf_agent.messages import custom_point
except ImportError:  # the agent is not installed here: the agent checks again
    custom_point = None


def point_from(line: str, args: argparse.Namespace) -> dict[str, Any] | None:
    """Build the point to send from one decoder line, or None."""
    try:
        item = json.loads(line)
    except ValueError:
        return None
    if not isinstance(item, dict) or item.get(args.id_field) is None:
        return None
    out: dict[str, Any] = {"mod": args.mod, "id": str(item[args.id_field]), "t": time.time()}
    if args.heard_here:
        out["heard_here"] = True
    elif args.lat_field in item and args.lon_field in item:
        out["lat"], out["lon"] = item[args.lat_field], item[args.lon_field]
    keep = [name for name in args.keep.split(",") if name]
    out["meta"] = {name: item[name] for name in keep if name in item}
    if custom_point is not None:
        checked = custom_point(
            out["id"],
            out["t"],
            lat=out.get("lat"),
            lon=out.get("lon"),
            heard_here=args.heard_here,
            meta=out["meta"],
        )
        if checked is None:
            return None
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a decoder's JSON lines to rf-agent.")
    parser.add_argument("--mod", required=True, help="this mod's name, such as lightning")
    parser.add_argument("--id-field", default="id", help="the field that names each thing")
    parser.add_argument("--lat-field", default="lat")
    parser.add_argument("--lon-field", default="lon")
    parser.add_argument("--heard-here", action="store_true", help="draw at the station")
    parser.add_argument("--keep", default="", help="fields to send in meta, comma separated")
    parser.add_argument("--agent", default="127.0.0.1:30960", help="the agent's custom input")
    args = parser.parse_args(argv)
    host, _, port = args.agent.rpartition(":")
    target = (host or "127.0.0.1", int(port))
    skipped = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for line in sys.stdin:
            point = point_from(line, args)
            if point is None:
                skipped += 1
                continue
            sock.sendto((json.dumps(point) + "\n").encode(), target)
    if skipped:
        print(f"{skipped} lines skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
