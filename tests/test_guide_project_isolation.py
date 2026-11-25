"""Guide learning must read the reference rules without loading it in the editor."""
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))


def main():
    try:
        import pcbnew
    except ImportError:
        print('test_guide_project_isolation: skipped (requires KiCad Python)')
        return
    from autorouter.engine import guides
    source = ROOT / 'tests/fixtures/obd2-adapter.kicad_pcb'
    active = pcbnew.LoadBoard(str(source))
    project = int(active.GetProject())
    filename = active.GetFileName()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'reference.kicad_pcb'
        # Thirty millimetres exceeds the style learner's class-length minimum.
        track = pcbnew.PCB_TRACK(active)
        track.SetStart(pcbnew.VECTOR2I(100000000, 100000000))
        track.SetEnd(pcbnew.VECTOR2I(130000000, 100000000))
        track.SetWidth(500000)
        track.SetLayer(pcbnew.F_Cu)
        track.SetNetCode(active.FindNet('/CANH').GetNetCode())
        active.Add(track)
        pcbnew.SaveBoard(str(path), active, True)
        pro = path.with_suffix('.kicad_pro')
        def rules(width):
            pro.write_text(json.dumps({'meta': {'version': 1}, 'net_settings': {
                'meta': {'version': 3}, 'classes': [{'name': 'Default', 'track_width': width,
                    'clearance': 0.2, 'via_diameter': 0.6, 'via_drill': 0.3}]}}))
        with patch.object(pcbnew, 'LoadBoard', side_effect=AssertionError('editor project replaced')):
            rules(0.88)
            wide = guides.learn(str(path))
            rules(0.2)
            narrow = guides.learn(str(path))
        assert wide['style']['class_width_mm'] == {}, wide
        assert narrow['style']['class_width_mm']['Default'] == 0.5, narrow
        assert int(active.GetProject()) == project and active.GetFileName() == filename
    print('test_guide_project_isolation: reference rules respected; editor loader never called; project preserved')


if __name__ == '__main__':
    main()
