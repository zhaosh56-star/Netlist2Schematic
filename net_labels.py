"""Compatibility command entrypoint; implementation: netlist2schematic.plugins.net_labels.net_labels."""
import importlib
import sys

_implementation = importlib.import_module('netlist2schematic.plugins.net_labels.net_labels')
if __name__ == "__main__":
    sys.exit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
    def __getattr__(name):
        return getattr(_implementation, name)
