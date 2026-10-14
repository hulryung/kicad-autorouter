#!/usr/bin/env python3
"""Route -> evaluate -> improve the router -> route again, with Claude Code.

Each round:
  1. benchmark: a copy of the reference board is stripped of its tracks and
     routed with the router code under test (scripts/improve_bench.py, KiCad's
     Python): metrics.json plus images of the result next to the reference;
  2. evaluate: `claude -p` reads the metrics and the images and scores the
     artwork (structured JSON: score, criteria, issues with positions);
  3. improve: `claude -p` edits the router code in a separate git worktree,
     guided by the evaluation and by what earlier rounds tried;
  4. verify: the unit tests and the benchmark run again on the edited code;
     the change is kept (committed on the loop's branch) only when the
     deterministic figures get better, otherwise the worktree is reset.

Nothing outside .improve/<run>/ is written: the reference board is only read,
the user's checkout is untouched, and the kept changes sit on the branch
improve/<run> for review (git diff main...improve/<run>, then merge or drop).

  python3 scripts/improve-loop.py [--reference board.kicad_pcb] [--rounds 3] [--skip GND,GND1,GND2]
                                  [--model opus] [--budget-usd 5] [--shove] [--goal "text"]
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = Path.home() / "dev/CAN-debugger-hw/hardware/can-debugger/CAN-debugger.kicad_pcb"
KICAD_PYTHONS = ["/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
                 r"C:\Program Files\KiCad\bin\python.exe", "/usr/bin/python3"]

CRITERIA = ["connectivity", "drc", "similarity_to_reference", "escape_and_fanout", "directness", "via_use", "neatness",
            # the design intent (only meaningful with --intent; otherwise the reviewer gives 10)
            "impedance_widths", "diff_pairs", "length_matching", "routing_priority"]

EVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "criteria": {"type": "object", "properties": {
            k: {"type": "integer", "minimum": 0, "maximum": 10}
            for k in CRITERIA},
            "required": CRITERIA, "additionalProperties": False},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "x_mm": {"type": ["number", "null"]}, "y_mm": {"type": ["number", "null"]},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "likely_cause_in_router": {"type": "string"}},
            "required": ["title", "x_mm", "y_mm", "severity", "likely_cause_in_router"], "additionalProperties": False}},
        "focus": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["score", "criteria", "issues", "focus", "summary"],
    "additionalProperties": False,
}

EVAL_PROMPT = """You review PCB artwork made by an autorouter, the way a senior layout engineer would.

The board {board} was finished by a human designer (the *reference*). Its tracks and vias were stripped
and the autorouter routed it again (the *routed* board). Nets {skip} are left to copper pours on purpose
(they get pad vias, not tracks), so missing tracks on those nets are fine.

Read metrics.json in {bench} (connectivity, DRC by type, artwork figures, per-net comparison with the
reference, escape-side agreement for the pins of the larger parts, the positions of problems) and LOOK at
every image listed in its "images" (use the Read tool on each path): the whole board and close-ups,
routed and reference side by side.

{intent}
Judge: open connections and DRC first; then how close the routing is to what the designer did
(pin escape directions, layer use, via placement, route topology); detours, zig-zags, odd angles, tracks
squeezed between pads, stubs, vias where none are needed, long parallel runs that block other pins.
{goal}
Give a score 0-100 (the overall quality a layout engineer would sign off on), each criterion 0-10
(10 = nothing to fix), and concrete issues with board positions in mm and what in an
autorouter's logic most likely causes each (pad escape planning, route ordering, rip-up, via cost,
fanout, cost function, pair coupling, impedance sizing, meander placement...). "focus" is the single change to the router you would make next.
Answer only with the JSON object."""

IMPROVE_PROMPT = """You improve the built-in PCB autorouter in this repository (a KiCad plugin, Python).

Code: plugins/autorouter/engine/native/ (extract.py reads the board, plans pad escapes, assigns routing
groups; grid.py, astar.py the search, router.py ordering/rip-up/pairs/fanout/stitching, pipeline.py the
passes, tuning.py length matching with meanders, apply.py writes tracks, problem.py the data model and
NativeOptions); plugins/autorouter/engine/si.py (design intent: impedance classes, length groups, routing
order, KiCad net classes and DRC rules) and engine/stackup.py (stackups, impedance formulas).
README.md "How the built-in router works" explains the design.

A benchmark strips the tracks of a finished reference board ({board}) and routes it again. The latest
result, before your change: {bench}/metrics.json and the images it lists (Read them).
Reviewer's evaluation of that result:
{evaluation}

Earlier rounds of this loop (what was tried, and whether it helped; do not repeat a rejected idea as is):
{history}

Task: make ONE focused change to the router that addresses the reviewer's "focus" (or the most important
issue you see), so the next benchmark run is better: fewer open connections and DRC problems first, then
closer to the reference artwork (escape sides, fewer detours and vias, shorter tracks), and meeting the
design intent when there is one (impedance widths, coupled pairs, matched lengths, routing groups in
order), without making routing much slower.
Rules:
- edit only files under plugins/autorouter/engine/ (and tests/ if a test needs updating);
- keep it deterministic (no randomness, no wall-clock decisions);
- run the tests before you finish: {kicad_py} scripts/run-tests.py
- you may run the benchmark yourself to check (takes 1-3 minutes):
  {kicad_py} {bench_script} --reference {reference} --plugins plugins --out {scratch} --skip {skip} --no-images{intent_arg}
  (it prints a one-line JSON summary at the end); never write to the reference board;
- do not commit; the loop measures your change and keeps or discards it.
Finish with a short plain summary: what you changed, where, and why."""


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def find_kicad_python():
    for p in KICAD_PYTHONS:
        if os.path.isfile(p):
            r = subprocess.run([p, "-c", "import pcbnew"], capture_output=True)
            if r.returncode == 0:
                return p
    sys.exit("KiCad's Python (with pcbnew) was not found.")


def claude_env():
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)                      # allowed to start its own session from inside one
    return env


def run_claude(prompt, cwd, out_file, args, schema=None, tools=("Read",), edit=False, timeout=3600):
    cmd = [args.claude, "-p", prompt, "--output-format", "json", "--no-session-persistence",
           "--add-dir", str(args.run_dir)]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if edit:
        cmd += ["--permission-mode", "acceptEdits"]
    cmd += ["--allowedTools", *tools]
    if args.model:
        cmd += ["--model", args.model]
    if args.budget_usd:
        cmd += ["--max-budget-usd", str(args.budget_usd)]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=claude_env())
    Path(out_file).write_text(r.stdout + ("\n--- stderr ---\n" + r.stderr if r.stderr else ""), encoding="utf-8")
    try:
        data = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude returned no JSON (exit {r.returncode}): {r.stderr[-500:] or r.stdout[-500:]}")
    if data.get("is_error"):
        raise RuntimeError(f"claude failed: {str(data.get('result'))[:500]}")
    data["_seconds"] = round(time.time() - t0)
    return data


def structured(data):
    if isinstance(data.get("structured_output"), dict):
        return data["structured_output"]
    text = (data.get("result") or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


INTENT_TEXT = """This run also has a design intent ({intent_file}): controlled-impedance classes (track
widths per layer computed from the stackup), length-matching groups (meanders after routing), differential
pairs and a routing order of net groups. metrics.json "intent" reports how well it is met: the share of
controlled track length at the right width per layer, each pair's coupled share and skew, each length
group's spread before/after tuning, and open nets per routing group. The intent is also written into
KiCad's DRC rules, so "rule_errors" and the drc entries diff_pair_gap_out_of_range, skew_out_of_range,
length_out_of_range, track_width are intent violations (judged under the intent criteria, not "drc").
Pairs listed under "pair_exceptions" were connected directly on purpose (ends too close for coupling to
matter, crossed pins, no room): judge whether that exception was reasonable and the direct route clean,
not that they are uncoupled. Meanders are handed to KiCad as tuning patterns ("kicad_tuning_patterns").
Look at the meanders and pairs in the images too: even bumps, no bumps squeezed against other copper,
pairs running side by side at a constant gap."""


def bench(args, plugins, out, images=True):
    cmd = [args.kicad_py, str(args.bench_script), "--reference", str(args.reference), "--plugins", str(plugins),
           "--out", str(out), "--skip", args.skip]
    if args.intent:
        cmd += ["--intent", str(args.intent_frozen)]
    if args.shove:
        cmd.append("--shove")
    if not images:
        cmd.append("--no-images")
    env = dict(os.environ, PYTHONHASHSEED="0")
    t0 = time.time()
    with open(Path(out).with_suffix(".log"), "w", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=args.bench_timeout, env=env)
    metrics_file = Path(out) / "metrics.json"
    if r.returncode != 0 or not metrics_file.exists():
        raise RuntimeError(f"benchmark failed (exit {r.returncode}), see {Path(out).with_suffix('.log')}")
    m = json.loads(metrics_file.read_text(encoding="utf-8"))
    m["_wall_seconds"] = round(time.time() - t0)
    return m


def penalty(m, base_seconds):
    """Lower is better. Hard problems dominate; the artwork terms break ties."""
    agree, total = (int(x) for x in m["escape_side_agreement"].split(" of "))
    own_vias = m["vias"] - m.get("fanout_vias", 0) - m.get("stitching_vias", 0)
    p = (1000 * (m.get("unconnected_after") or 0) + 300 * m.get("drc_errors", 0) + 40 * m.get("dangling", 0)
         + 20 * m.get("off_45", 0) + 20 * m.get("doubled_back", 0) + 20 * m.get("acute_corners", 0)
         + 5 * (total - agree)
         + 0.05 * m["length_mm"] + 0.5 * max(0, own_vias))
    p += 60 * m.get("rule_errors", 0)
    it = m.get("intent") or {}
    if it:
        if it.get("impedance_width_ok_pct") is not None:
            p += 3 * (100 - it["impedance_width_ok_pct"])
        for pr in it.get("pairs", []):
            if not pr.get("exception"):           # a pair connected directly on purpose is not penalised
                p += 1.0 * (100 - pr["coupled_pct"])
        for g in it.get("length_groups", []):
            if not g["ok"]:
                p += 150 + 50 * min(10.0, sum(abs(v) for v in g["off_by_mm"].values()))
        for group, nets in (it.get("open_by_group") or {}).items():
            if group in ("pairs", "memory", "high_speed"):
                p += 500 * len(nets)                 # an important net left open is worse than any other
    slow = m["route_seconds"] - 1.5 * base_seconds
    if slow > 0:
        p += 0.5 * slow
    return round(p, 1)


def figures(m):
    f = {k: m.get(k) for k in ("routed", "unconnected_after", "drc_errors", "rule_errors", "dangling", "off_45",
                               "acute_corners",
                               "doubled_back", "length_mm", "vias", "route_seconds", "escape_side_agreement")}
    it = m.get("intent") or {}
    if it:
        f["impedance_ok_pct"] = it.get("impedance_width_ok_pct")
        f["pairs_coupled_pct"] = [p["coupled_pct"] for p in it.get("pairs", [])]
        f["length_groups_ok"] = it.get("length_groups_ok")
    return f


def git(*a, cwd=REPO, check=True):
    r = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
    return r.stdout


def make_worktree(run_dir, name):
    wt = run_dir / "worktree"
    branch = f"improve/{name}"
    git("worktree", "add", "-q", "-b", branch, str(wt), "HEAD")
    # start from the working state of the checkout, uncommitted edits and new files included
    diff = git("diff", "HEAD", "--binary")
    if diff.strip():
        p = subprocess.run(["git", "apply", "--whitespace=nowarn"], cwd=str(wt), input=diff, text=True,
                           capture_output=True)
        if p.returncode != 0:
            raise RuntimeError("could not carry the uncommitted changes into the worktree: " + p.stderr)
    for f in git("ls-files", "--others", "--exclude-standard").splitlines():
        if f.startswith(".improve/"):
            continue
        dst = wt / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, dst)
    if git("status", "--porcelain", cwd=wt).strip():
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", "Starting point: the working tree the improvement loop started from", cwd=wt)
    return wt, branch


def reset(wt):
    git("reset", "-q", "--hard", "HEAD", cwd=wt)
    git("clean", "-q", "-fd", cwd=wt)


def unit_tests(args, wt):
    """The whole suite with KiCad's Python (router core, AI loop, end to end on the fixture)."""
    r = subprocess.run([args.kicad_py, "scripts/run-tests.py"], cwd=str(wt), capture_output=True, text=True,
                       timeout=1800)
    if r.returncode != 0:
        return False, (r.stdout + r.stderr)[-2000:]
    return True, ""


def write_report(args, rounds, best, base):
    lines = [f"# Router improvement run {args.name}", "",
             f"Reference board: `{args.reference}`  ", f"Branch: `{args.branch}` (worktree `{args.worktree}`)  ",
             f"Nets left to pours: {args.skip}  ", f"Design intent: {args.intent or 'none'}", "",
             "| Round | Reviewer score | Penalty | Routed | Open | DRC errors | Rule errors | Dangling | Off 45 | Length mm | Vias | Escape sides | Impedance ok % | Pairs coupled % | Length groups ok | Time s | Kept |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rounds:
        m = r.get("metrics") or {}
        lines.append(f"| {r['round']} | {r.get('score', '')} | {r.get('penalty', '')} | {m.get('routed', '')} | "
                     f"{m.get('unconnected_after', '')} | {m.get('drc_errors', '')} | {m.get('rule_errors', '')} | "
                     f"{m.get('dangling', '')} | "
                     f"{m.get('off_45', '')} | {m.get('length_mm', '')} | {m.get('vias', '')} | "
                     f"{m.get('escape_side_agreement', '')} | {m.get('impedance_ok_pct', '')} | "
                     f"{m.get('pairs_coupled_pct', '')} | {m.get('length_groups_ok', '')} | "
                     f"{m.get('route_seconds', '')} | {r.get('kept', '')} |")
    lines += ["", f"Baseline penalty {base}, best {best}.", ""]
    for r in rounds:
        lines.append(f"## Round {r['round']}")
        if r.get("evaluation"):
            e = r["evaluation"]
            crit = ", ".join(f"{k} {v}" for k, v in (e.get("criteria") or {}).items())
            lines += ["", f"**Reviewer ({e.get('score')}/100):** {e.get('summary', '')}", "",
                      f"**Criteria (0-10):** {crit}", "",
                      f"**Focus:** {e.get('focus', '')}", ""]
            for i in e.get("issues", [])[:8]:
                at = f" at ({i['x_mm']}, {i['y_mm']})" if i.get("x_mm") is not None else ""
                lines.append(f"- [{i['severity']}] {i['title']}{at}: {i['likely_cause_in_router']}")
        if r.get("change"):
            lines += ["", f"**Change:** {r['change']}", ""]
        if r.get("verdict"):
            lines += [f"**Result:** {r['verdict']}", ""]
    (args.run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", default=str(DEFAULT_REFERENCE))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--skip", default="GND,GND1,GND2")
    ap.add_argument("--shove", action="store_true")
    ap.add_argument("--model", default="")
    ap.add_argument("--budget-usd", type=float, default=0, help="per Claude call")
    ap.add_argument("--goal", default="", help="extra guidance for the reviewer and the improver")
    ap.add_argument("--bench-timeout", type=int, default=1800)
    ap.add_argument("--claude", default=shutil.which("claude") or "claude")
    ap.add_argument("--no-final-eval", action="store_true")
    ap.add_argument("--intent", default="", help="design intent JSON (impedance classes, length groups, routing "
                    "order); default: examples/<board>.intent.json when it exists, 'none' for no intent")
    args = ap.parse_args()

    args.reference = Path(args.reference).expanduser().resolve()
    if not args.reference.is_file():
        sys.exit(f"reference board not found: {args.reference}")
    if not shutil.which(args.claude) and not os.path.isfile(args.claude):
        sys.exit("the claude CLI was not found")
    args.kicad_py = find_kicad_python()
    args.name = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    args.run_dir = REPO / ".improve" / args.name
    args.run_dir.mkdir(parents=True)
    args.bench_script = args.run_dir / "improve_bench.py"           # frozen: every round measured the same way
    shutil.copy(REPO / "scripts" / "improve_bench.py", args.bench_script)
    args.worktree, args.branch = make_worktree(args.run_dir, args.name)
    if not args.intent:
        cand = REPO / "examples" / (args.reference.stem + ".intent.json")
        args.intent = str(cand) if cand.exists() else ""
    if args.intent == "none":
        args.intent = ""
    intent_text = ""
    if args.intent:
        args.intent_frozen = args.run_dir / "intent.json"
        shutil.copy(Path(args.intent).expanduser(), args.intent_frozen)
        intent_text = INTENT_TEXT.format(intent_file=args.intent_frozen)
    goal = f"\nThe user's goal for this run: {args.goal}\n" if args.goal else ""
    log(f"run {args.run_dir}  branch {args.branch}")

    log("baseline benchmark...")
    current = bench(args, args.worktree / "plugins", args.run_dir / "round0-bench")
    base_seconds = current["route_seconds"]
    best = base = penalty(current, base_seconds)
    current_dir = args.run_dir / "round0-bench"
    log(f"baseline: penalty {best}  {figures(current)}")
    rounds = [{"round": 0, "metrics": figures(current), "penalty": best, "kept": "baseline"}]
    history = []

    for k in range(1, args.rounds + 1):
        rd = {"round": k}
        rounds.append(rd)
        log(f"round {k}: evaluating the current result...")
        try:
            data = run_claude(EVAL_PROMPT.format(board=args.reference.name, skip=args.skip, bench=current_dir, goal=goal,
                                                intent=intent_text),
                              args.run_dir, args.run_dir / f"round{k}-evaluation.json", args, schema=EVAL_SCHEMA)
            evaluation = structured(data)
        except Exception as e:
            log(f"evaluation failed: {e}")
            rd["verdict"] = f"evaluation failed: {e}"
            break
        rounds[-2]["score"] = evaluation["score"]               # the score belongs to the state it reviewed
        rd["evaluation"] = evaluation
        log(f"  score {evaluation['score']}: {evaluation['focus'][:160]}")

        log(f"round {k}: asking Claude Code for a change...")
        scratch = args.run_dir / f"round{k}-scratch"
        prompt = IMPROVE_PROMPT.format(board=args.reference.name, bench=current_dir, reference=args.reference,
                                       evaluation=json.dumps(evaluation, indent=1) + goal,
                                       history=json.dumps(history, indent=1) if history else "(none yet)",
                                       kicad_py=args.kicad_py, bench_script=args.bench_script, scratch=scratch,
                                       skip=args.skip,
                                       intent_arg=f" --intent {args.intent_frozen}" if args.intent else "")
        tools = ("Read", "Edit", "Write", "Glob", "Grep", f"Bash({args.kicad_py} *)", "Bash(python3 *)",
                 "Bash(git diff*)", "Bash(git status*)", "Bash(ls *)")
        try:
            data = run_claude(prompt, args.worktree, args.run_dir / f"round{k}-improve.json", args, tools=tools,
                              edit=True, timeout=5400)
            change = (data.get("result") or "").strip()
        except Exception as e:
            log(f"improvement call failed: {e}")
            rd["verdict"] = f"improvement call failed: {e}"
            reset(args.worktree)
            continue
        rd["change"] = change
        diff = git("diff", cwd=args.worktree)
        (args.run_dir / f"round{k}.patch").write_text(diff, encoding="utf-8")
        log(f"  change: {change[:200]}")
        if not diff.strip():
            rd["verdict"] = "no code change"
            history.append({"round": k, "change": change[:600], "kept": False, "why": "no code change"})
            continue

        ok, out = unit_tests(args, args.worktree)
        if not ok:
            rd["verdict"] = "unit tests failed: change discarded"
            history.append({"round": k, "change": change[:600], "kept": False, "why": "unit tests failed: " + out[-300:]})
            log("  unit tests failed, discarded")
            reset(args.worktree)
            continue

        log(f"round {k}: benchmark of the changed router...")
        out_dir = args.run_dir / f"round{k}-bench"
        try:
            m = bench(args, args.worktree / "plugins", out_dir)
        except Exception as e:
            rd["verdict"] = f"benchmark failed ({e}): change discarded"
            history.append({"round": k, "change": change[:600], "kept": False, "why": str(e)})
            reset(args.worktree)
            continue
        p = penalty(m, base_seconds)
        rd["metrics"], rd["penalty"] = figures(m), p
        worse_hard = ((m.get("unconnected_after") or 0) > (current.get("unconnected_after") or 0)
                      or m.get("rule_errors", 0) > current.get("rule_errors", 0) + 2
                      or m.get("drc_errors", 0) > current.get("drc_errors", 0))
        if p < best - 0.5 and not worse_hard:
            git("add", "-A", cwd=args.worktree)
            git("commit", "-q", "-m", f"improve loop round {k}: " + (change.splitlines()[0][:70] if change else "router change"),
                "-m", change[:3000], cwd=args.worktree)
            rd["kept"] = "yes"
            rd["verdict"] = f"kept: penalty {best} -> {p}"
            history.append({"round": k, "change": change[:600], "kept": True, "penalty": [best, p], "figures": figures(m)})
            best, current, current_dir = p, m, out_dir
            log(f"  {rd['verdict']}")
        else:
            rd["kept"] = "no"
            rd["verdict"] = f"discarded: penalty {best} -> {p}" + (" (more open connections or DRC errors)" if worse_hard else "")
            history.append({"round": k, "change": change[:600], "kept": False, "penalty": [best, p], "figures": figures(m)})
            log(f"  {rd['verdict']}")
            reset(args.worktree)
        write_report(args, rounds, best, base)

    if not args.no_final_eval and rounds[-1].get("kept") == "yes":
        log("final evaluation of the best result...")
        try:
            data = run_claude(EVAL_PROMPT.format(board=args.reference.name, skip=args.skip, bench=current_dir, goal=goal,
                                                intent=intent_text),
                              args.run_dir, args.run_dir / "final-evaluation.json", args, schema=EVAL_SCHEMA)
            rounds[-1]["score"] = structured(data)["score"]
        except Exception as e:
            log(f"final evaluation failed: {e}")
    write_report(args, rounds, best, base)
    (args.run_dir / "rounds.json").write_text(json.dumps(rounds, indent=1), encoding="utf-8")
    kept = [r for r in rounds if r.get("kept") == "yes"]
    log(f"done: {len(kept)} change(s) kept, penalty {base} -> {best}. Report: {args.run_dir / 'report.md'}")
    log(f"review with: git diff HEAD...{args.branch}   (merge with: git merge {args.branch})")


if __name__ == "__main__":
    main()
