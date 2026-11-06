"""Reviewer images must cover all copper layers at the correct physical scale."""
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai import render, review


def main():
    class Layers:
        def CuStack(self):
            return range(4)

    class Board:
        def GetEnabledLayers(self):
            return Layers()

        def GetLayerName(self, layer):
            return ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'][layer]

    reviewer = review.Reviewer(Board(), {}, None, None, Path('/unused'), lambda s: None, binary='fake')
    with patch.object(render, 'render_board', side_effect=lambda b, w, **kw: w / kw['name']) as draw:
        images = reviewer._images(1, [])
    assert len(images) == 4
    assert [call.kwargs['layers'].split(',')[0] for call in draw.call_args_list] == ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
    try:
        import wx.svg
    except ImportError:
        print('test_ai_render: layer coverage ok; physical-scale render skipped (needs wx.svg)')
        return
    with tempfile.TemporaryDirectory() as d:
        svg, png = Path(d) / 'input.svg', Path(d) / 'output.png'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" height="5mm" viewBox="0 0 10 5">'
                       '<rect x="2" y="1" width="6" height="3" fill="#ff0000"/></svg>')
        assert render._svg_to_png(svg, png, 200)
        data = png.read_bytes()
        width, height = struct.unpack('>II', data[16:24])
        assert (width, height) == (200, 100), (width, height)
        chunks, offset = [], 8
        while offset < len(data):
            size = struct.unpack('>I', data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            if kind == b'IDAT':
                chunks.append(data[offset + 8:offset + 8 + size])
            offset += size + 12
        raw = zlib.decompress(b''.join(chunks))
        def pixel(x, y):
            offset = y * (width * 4 + 1) + 1 + x * 4
            return tuple(raw[offset:offset + 4])
        assert pixel(100, 50) == (255, 0, 0, 255), pixel(100, 50)
        assert pixel(5, 5) == (32, 32, 36, 255), pixel(5, 5)
        assert pixel(150, 50) == (255, 0, 0, 255), 'physical units left blank thumbnail padding'
    print('test_ai_render: all layers, aspect ratio, physical scale and opaque contrast verified')


if __name__ == '__main__':
    main()
