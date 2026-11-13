#!/usr/bin/env python3
"""Parse a flat SPICE circuit or --cell into the SA components/nets document."""
import argparse
import json
import sys

from netlist2schematic.netlist.netlist_core import NetlistError, parse_netlist, to_document


def parse(path, cell=None):
    return to_document(parse_netlist(path, cell=cell))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('netlist')
    parser.add_argument('--cell', help='select one .subckt without recursive expansion')
    args = parser.parse_args(argv)
    try:
        doc = parse(args.netlist, cell=args.cell)
    except (NetlistError, OSError) as exc:
        parser.exit(2, f'error: {exc}\n')
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    print(f"{len(doc['components'])} components, {len(doc['nets'])} nets", file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
