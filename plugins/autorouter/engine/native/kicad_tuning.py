"""KiCad's own length tuning patterns for the router's meanders.

KiCad keeps a tuned meander as a tuning pattern (a "generated" board item):
a base line along the track, a target length window, spacing, amplitudes,
and the tracks it made. The length tuner (Route > Tune Length) creates them
interactively, and Edit > Update All Tuning Patterns rebuilds every one with
KiCad's meander engine against the board's rules.

The scripting API cannot construct one, but it can load one: the patterns
are written to a small temporary board file, loaded, and moved onto the real
board (they are groups underneath, so the meander tracks become their
members). In the PCB editor the plugin then asks KiCad to update all tuning
patterns, so the final meanders are KiCad's own; in a saved board (headless
runs) they stay the router's, owned by a pattern KiCad can retune.
"""
import os
import tempfile
import uuid
from typing import List

import pcbnew

MENU_LABEL = "Update All Tuning Patterns"


def _mm(nm: float) -> str:
    return f"{nm / 1e6:.6f}"


def placeholder_sexpr(board, spec: dict, uid: str) -> str:
    """A track for the pattern to own while it sits in the temporary file
    (KiCad drops tuning patterns without members when it loads a file)."""
    layer, x1, y1, x2, y2, width = spec["base"]
    return (f'\t(segment\n\t\t(start {_mm(x1)} {_mm(y1)})\n\t\t(end {_mm(x2)} {_mm(y2)})\n'
            f'\t\t(width {_mm(width)})\n\t\t(layer "{board.GetLayerName(layer)}")\n\t\t(net 0)\n'
            f'\t\t(uuid "{uid}")\n\t)\n')


def pattern_sexpr(board, spec: dict, uid: str, member: str = "") -> str:
    """One tuning pattern (single track mode) as KiCad writes it."""
    layer, x1, y1, x2, y2, width = spec["base"]
    lname = board.GetLayerName(layer)
    target, tol = spec["target"], spec["tolerance"]
    status = "tuned" if abs(spec.get("length", target) - target) <= tol else (
        "too_short" if spec.get("length", target) < target else "too_long")
    return f'''	(generated
		(uuid "{uid}")
		(type "tuning_pattern")
		(name "Tuning Pattern")
		(layer "{lname}")
		(base_line
			(pts
				(xy {_mm(x1)} {_mm(y1)}) (xy {_mm(x2)} {_mm(y2)})
			)
		)
		(corner_radius_percent {spec.get("corner", 80)})
		(end
			(xy {_mm(x2)} {_mm(y2)})
		)
		(initial_side "default")
		(last_netname "{spec["net"]}")
		(last_status "{status}")
		(last_track_width {_mm(width)})
		(last_tuning_length {_mm(spec.get("length", target))})
		(max_amplitude {_mm(spec["amax"])})
		(min_amplitude {_mm(spec["amin"])})
		(min_spacing {_mm(spec["spacing"])})
		(origin
			(xy {_mm(x1)} {_mm(y1)})
		)
		(override_custom_rules yes)
		(rounded no)
		(single_sided no)
		(target_length {_mm(target)})
		(target_length_max {_mm(target + tol)})
		(target_length_min {_mm(target - tol)})
		(tuning_mode "single")
		(members "{member}")
	)
'''


def attach(board, specs: List[dict]) -> int:
    """Put a tuning pattern on the board for each spec ({net, base: (layer,
    x1, y1, x2, y2, width), members: [track UUIDs], target, tolerance,
    spacing, amin, amax, length}); returns how many were attached."""
    if not specs:
        return 0
    uids = [str(uuid.uuid4()) for _ in specs]
    dummies = [str(uuid.uuid4()) for _ in specs]
    version = getattr(pcbnew, "SEXPR_BOARD_FILE_VERSION", 20260206)          # this KiCad's own file format
    text = (f"(kicad_pcb\n\t(version {version})\n\t(generator \"autorouter\")\n"
            + "".join(placeholder_sexpr(board, s, d) for s, d in zip(specs, dummies))
            + "".join(pattern_sexpr(board, s, u, d) for s, u, d in zip(specs, uids, dummies)) + ")\n")
    fd, path = tempfile.mkstemp(suffix=".kicad_pcb", prefix="autorouter-tuning-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        holder = pcbnew.LoadBoard(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    done = 0
    for spec, uid in zip(specs, uids):
        item = holder.ResolveItem(pcbnew.KIID(uid))
        if item is None or item.Type() != pcbnew.PCB_GENERATOR_T:
            continue
        group = pcbnew.Cast_to_PCB_GROUP(item)           # a tuning pattern is a group underneath
        for m in list(group.GetItems()):
            group.RemoveItem(m)                          # the placeholder stays behind in the holder
        holder.Remove(item)
        item.thisown = 0
        board.Add(item)
        for m in spec["members"]:
            track = board.ResolveItem(pcbnew.KIID(m))
            if track is not None:
                group.AddItem(track)
        done += 1
    _HOLDERS.append(holder)                              # its items moved out; keep it alive with them
    return done


_HOLDERS: list = []


def request_update() -> bool:
    """In the PCB editor: run Edit > Update All Tuning Patterns once the
    plugin returns, so KiCad's length tuner rebuilds the meanders itself.
    False when there is no editor window (headless) or no such menu item."""
    try:
        import wx
    except ImportError:
        return False
    frame = None
    for w in wx.GetTopLevelWindows():
        if w.GetName() == "PcbFrame" or "PCB Editor" in w.GetTitle():
            frame = w
            break
    if frame is None or frame.GetMenuBar() is None:
        return False
    labels = {MENU_LABEL, wx.GetTranslation(MENU_LABEL)}
    owner = [None]
    item_id = wx.NOT_FOUND
    bar = frame.GetMenuBar()
    for i in range(bar.GetMenuCount()):
        for item in _items(bar.GetMenu(i)):
            if item.GetItemLabelText() in labels:
                item_id = item.GetId()
                owner[0] = _owner(bar.GetMenu(i), item_id)
                break
        if item_id != wx.NOT_FOUND:
            break
    if item_id == wx.NOT_FOUND or owner[0] is None:
        return False
    # KiCad's menus run their own actions (the frame does not): the event goes
    # to the menu, queued so it runs after the plugin has returned the board
    event = wx.CommandEvent(wx.wxEVT_MENU, item_id)
    event.SetEventObject(owner[0])
    wx.PostEvent(owner[0], event)
    return True


def _items(menu):
    for item in menu.GetMenuItems():
        yield item
        if item.GetSubMenu() is not None:
            yield from _items(item.GetSubMenu())


def _owner(menu, item_id):
    for item in menu.GetMenuItems():
        if item.GetId() == item_id:
            return menu
        if item.GetSubMenu() is not None:
            found = _owner(item.GetSubMenu(), item_id)
            if found is not None:
                return found
    return None
