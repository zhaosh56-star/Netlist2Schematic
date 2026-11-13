"""Compatibility command entrypoint; see netlist2schematic/source_map.json."""
from netlist2schematic.common.sa_common import source_path as _source_path
__file__ = str(_source_path('place-layered.py'))
exec(compile(_source_path('place-layered.py').read_bytes(), __file__, "exec"), globals())
