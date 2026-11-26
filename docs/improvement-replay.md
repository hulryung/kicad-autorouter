# Compare and replay improvements

After **AI assistant → Score and improve**, click **Compare / replay**. A local
browser page compares the initial board with the selected retained checkpoint.
Drag the comparison slider, use the checkpoint slider, or play the improvements
at one, two or four seconds per step. Native sliders also support arrow keys.
Score, open connections, DRC report count, track length and via count are shown
beside the images. The GUI includes every enabled copper layer in its view.

Only accepted checkpoints appear in playback. Rejected experiments and their
reasons are listed separately. These are saved board states, not a recording
of individual tracks being drawn. With one accepted improvement there are two
frames; with no accepted improvement there is only the initial frame. A retained
candidate is not necessarily applied to the editor if the search was cancelled.

The viewer works offline, without uploading a board or using an external
service. It opens from the run's `search/replay/index.html`; keep the `frames`
folder alongside it. Job retention also applies to this directory, so copy the
whole replay directory elsewhere to keep or share it. Exported board images and
rejection reasons may contain project details: review them before publishing.

## Export an existing run and a video

Use KiCad's Python (the same environment as `scripts/route-with-codex.py`):

```sh
python3 scripts/export-replay.py /path/to/search --out /path/to/new-replay \
  --layers F.Cu,In1.Cu,In2.Cu,B.Cu,Edge.Cuts --size 1920 --seconds 2 --video
```

The input directory must contain `optimization.json` and its initial/attempt
board directories. The output directory must be new. Source boards, run data,
and the open editor are not modified. Older runs are supported using their
saved directory layout and initial review measurements when available; missing
historical measurements display **not recorded**.

Outputs:

- `index.html`: before/after comparison and checkpoint playback.
- `frames/frame-0000.png`, …: consistently framed PNGs for a video editor.
- `timelapse.mp4`: optional silent H.264 video at 30 fps, holding each checkpoint
  for `--seconds`; this requires **ffmpeg** on PATH. Without ffmpeg, omit
  `--video` and use the PNG sequence in your video editor.
- `replay.json` and `description.txt`: summary measurements and a text description
  of the sequence. The MP4 contains clean board images; the browser view adds
  metrics and playback controls.

The CLI defaults to F.Cu/B.Cu/Edge.Cuts. Select inner layers explicitly for
multi-layer work. `--size` controls the longest image dimension (256–4096 px);
board aspect ratio is retained, with at most one padding pixel for H.264.
The exporter rejects changed outline bounds or mismatched image sizes instead
of producing a misleading aligned comparison. It requires a working 2-D SVG
renderer and never substitutes a 3-D camera view for a missing frame.

New optimizer runs retain initial measurements and relative checkpoint paths.
This recording metadata does not change the routing acceptance gates, cancellation
behavior or existing Undo implementation. Manual editor Undo verification was
removed from acceptance requirements at the user's request.
