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

    def invoke(fn, bridge=True):
        result = {}
        def worker():
            try:
                result['value'] = AutorouterDialog._on_main(dialog, fn) if bridge else fn()
            except Exception as exc:
                result['error'] = exc
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
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
    # Stop on the first extraction stage after a real final-pour refill.
    # The worker must report the interrupted pass without undoing that fill.
    from test_fanout_stub_clearance import make_board
    from autorouter.engine.native import extract, pipeline
    from autorouter.engine.native.problem import NativeOptions
    board, _ = make_board()
    zone = next(iter(board.Zones()))
    zone.UnFill()
    assert zone.GetFilledPolysList(pcbnew.B_Cu).OutlineCount() == 0
    refilled = []
    def read(b, opts, status):
        def operation():
            filling = [False]
            def stage(text):
                if filling[0]:
                    assert zone.GetFilledPolysList(pcbnew.B_Cu).OutlineCount() > 0
                    refilled.append(zone.GetFilledPolysList(pcbnew.B_Cu).Format())
                    raise Overrun('after completed refill')
                filling[0] = text == 'Refilling the pours...'
            return extract.extract(b, opts, status=stage)
        return AutorouterDialog._on_main(dialog, operation)
    outcome = invoke(lambda: pipeline.run_job(
        board, NativeOptions(fanout_first=False, stitch_vias=False, tune_lengths=False),
        extract_fn=read, apply_paths=False, escalate=False), bridge=False)
    assert 'error' not in outcome, outcome
    job = outcome['value']
    assert job.wall_capped and job.timed_out
    assert 'the pour pass' in job.stats.get('overrun_passes', []), job.stats
    assert refilled and zone.GetFilledPolysList(pcbnew.B_Cu).Format() == refilled[-1]
    app.Destroy()
    print('test_ui_exception_bridge: ok')


if __name__ == '__main__':
    main()
