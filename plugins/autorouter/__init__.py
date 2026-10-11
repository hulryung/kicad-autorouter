"""
KiCad Autorouter plugin.

Routes the open board with the Freerouting engine without leaving the PCB
editor: the board is exported to Specctra DSN, Freerouting runs headless in
the background with live progress, and the resulting session is imported
back as one undoable step.
"""
import os

__version__ = "0.2.0"
__author__ = "hulryung"
__license__ = "MIT"


def _in_ipc_plugin_process() -> bool:
    """KiCad launches IPC plugins as separate processes with a per-launch
    token; registering a SWIG action plugin there fails."""
    return bool(os.environ.get("KICAD_API_TOKEN"))


# Register the SWIG action plugin with KiCad. Guarded so the engine modules
# can be imported and unit-tested outside KiCad's Python (no pcbnew/wx).
if not _in_ipc_plugin_process():
    try:
        from .plugin import AutorouterPlugin
    except ImportError:          # plain Python without pcbnew/wx: engine-only use
        AutorouterPlugin = None
    if AutorouterPlugin is not None and __name__ != "__main__":
        try:
            AutorouterPlugin().register()
        except Exception:        # not inside the PCB editor; KiCad would show it
            pass
