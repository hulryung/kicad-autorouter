"""The SWIG action plugin (KiCad 9 and 10): the toolbar button and its Run()."""
from pathlib import Path

import pcbnew
import wx

from . import __version__
from .log import get_logger

logger = get_logger()


def _pcb_frame():
    """The PCB editor window, to parent our dialog on (None if not found)."""
    for name in ("PcbFrame", "PcbNew"):
        frame = wx.FindWindowByName(name)
        if frame:
            return frame
    return None


def _dev_reload():
    """With a .devreload file next to this module, drop the plugin's submodules
    so the next import picks up edited sources without restarting KiCad
    (Tools > External Plugins > Refresh Plugins only reloads the package)."""
    if not (Path(__file__).parent / ".devreload").exists():
        return
    import sys
    for name in [m for m in sys.modules if m.startswith("autorouter.") and m != "autorouter.plugin"]:
        del sys.modules[name]


class AutorouterPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Autorouter"
        self.category = "Routing"
        self.description = "Autoroute the board inside KiCad (built-in router, AI assistant)"
        self.show_toolbar_button = True
        icon = Path(__file__).parent / "plugin_resources" / "icon-24.png"
        self.icon_file_name = str(icon) if icon.exists() else ""
        self.dark_icon_file_name = self.icon_file_name

    def Run(self):
        try:
            _dev_reload()
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("Open a board first.", "Autorouter", wx.ICON_ERROR)
                return
            from .dialog import AutorouterDialog

            dialog = AutorouterDialog(_pcb_frame(), board)
            try:
                dialog.ShowModal()
            finally:
                dialog.Destroy()
            # KiCad records the board changes made during Run() as one undo
            # step and rebuilds connectivity once Run() returns.
            pcbnew.Refresh()
        except Exception as e:                     # never let KiCad see a traceback
            logger.exception("Autorouter failed")
            wx.MessageBox(f"Autorouter {__version__} failed:\n{e}", "Autorouter", wx.ICON_ERROR)
