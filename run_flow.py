"""Compatibility command entrypoint; implementation: netlist2schematic.flow.run_flow."""
import importlib
import sys

_implementation = importlib.import_module('netlist2schematic.flow.run_flow')
if __name__ == "__main__":
    from netlist2schematic.flow.process_budget import command_main
    from pathlib import Path
    sys.exit(command_main(_implementation.main, Path(__file__)))
else:
    sys.modules[__name__] = _implementation
    def __getattr__(name):
        return getattr(_implementation, name)
