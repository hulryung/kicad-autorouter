"""UI-thread failures must reach the routing worker, including hard stops."""
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
    import wx
except ImportError:
    print('test_ui_exception_bridge: skipped (needs KiCad Python)')
    sys.exit(0)


def main():
    if os.environ.get('KICAD_AUTOROUTER_GUI_TESTS') != '1':
        print('test_ui_exception_bridge: skipped (needs desktop session)')
        return
    app = wx.GetApp() or wx.App(False)
    from autorouter.dialog import AutorouterDialog
    from autorouter.engine.native.watchdog import Overrun
    ui_thread = threading.get_ident()
    messages = []
    dialog = SimpleNamespace(_append_log=messages.append)

    def invoke(fn):
        result = {}
        def worker():
            try:
                result['value'] = AutorouterDialog._on_main(dialog, fn)
            except Exception as exc:
                result['error'] = exc
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while thread.is_alive() and time.monotonic() < deadline:
            app.Yield()
            thread.join(.005)
        assert not thread.is_alive(), 'UI callback left routing worker waiting'
        return result

    assert invoke(lambda: threading.get_ident()) == {'value': ui_thread}
    assert invoke(lambda: None) == {'value': None}
    for error in (ValueError('board read failed'), Overrun('after pour refill')):
        def fail():
            assert threading.get_ident() == ui_thread
            raise error
        assert invoke(fail).get('error') is error, 'UI exception was swallowed'
    assert len(messages) == 2
    app.Destroy()
    print('test_ui_exception_bridge: ok')


if __name__ == '__main__':
    main()
