"""Construct an old-format test artifact without the new-netlist entry point."""
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from netlist2schematic.common.sa_common import load_local, resource_root
from netlist2schematic.flow import run_flow
from netlist2schematic.recovery.fallback_bus import create_fallback


def saved_bus(source, out_dir, **kwargs):
    source=Path(source).resolve()
    placement,comps,nets,index,metadata,routed=create_fallback(source,deadline=perf_counter()+20)
    data=dict(metadata,format_version=1,mode='layered',source_netlist=str(source),
        source_sha256=sha256(source.read_bytes()).hexdigest(),source_cell=None,
        analog_canvas_root=str(resource_root()),placement=run_flow._saved_positions(placement,comps))
    return run_flow._finish(data,load_local('place_and_route','place-and-route.py'),
        placement,comps,nets,index,source.stem,Path(out_dir),routed_result=routed,simplify_routes='off')
