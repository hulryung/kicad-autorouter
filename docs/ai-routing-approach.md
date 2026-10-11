# AI-assisted routing: approach and plan

This note records how the AI assistant in this plugin is meant to work, what
was tried, and where it should go. It is the design behind the **AI assistant**
tab (`plugins/autorouter/engine/ai/`).

## Where an LLM helps, and where it does not

Routing is geometry under hard constraints. Language models are poor at
producing exact coordinates and at keeping dozens of clearances in their head,
but good at the parts a human engineer does with judgement:

| Task | Deterministic router | LLM |
|---|---|---|
| Finding a clearance-correct path for one connection | strong (A*, rip-up) | weak |
| Deciding what to route first, what to leave to pours, which nets are power | needs rules | strong, from names, classes and context |
| Picking widths, via costs, layer directions for a board | needs defaults | good, from the board's rules and intent |
| Reading a DRC report and deciding what to change | none | strong |
| Recognising "this looks wrong" on a picture | none | good with a rendered image |
| Drawing a specific track by hand | n/a | risky, must be verified |

So the division of labour is: **the router owns geometry, the model owns
decisions**, and every model decision is executed by the router and checked by
KiCad's DRC before the model sees the result.

## Levels of involvement

1. **Operator** (implemented). The model gets a JSON view of the board (rules,
   net classes, nets with pads/pours/pairs, footprints per sheet, options, the
   last run, the DRC) and answers with actions: change options, route a scope
   (board / nets / sheets / selection), remove or add tracks and vias, refill,
   ask the user. Rounds repeat with results until it says it is done. The
   output is schema-enforced (`codex exec --output-schema`), so a reply is
   always machine-readable.
2. **Local editor** (next). Richer, still verified primitives: route a net with
   waypoints or a preferred layer, keep a region clear (temporary keepout),
   assign a net to a class, set priorities for the order, move a via. Each
   edit is applied, DRC-checked, and rolled back if it makes things worse.
3. **Reviewer with eyes** (next, cheap). Attach a rendered image of the board
   (`kicad-cli pcb export svg` / `pcb render`) to the prompt with `codex exec
   -i`, so the model can criticise the artwork the way a human does from a
   screenshot: zig-zags, detours, tracks under connectors, missing return vias.
4. **Learned style** (later, needs reference data). Original artworks of the
   same designer or product line give statistics the router can be tuned to:
   widths per class, via density, layer usage, escape habits, pair handling.
   A model (or plain fitting) turns those into router defaults per project,
   and the `artwork:` metrics from `tests/test_native_e2e.py` measure the
   distance to the reference. This is the "similar to the original artwork"
   goal and needs the reference boards first.

Generating whole routes token by token (the model writing coordinates for
every segment) is not on the list: it is slow, unreliable, and the router
already does it better. Direct `add_track` exists for small, explicit fixes
and is always followed by a DRC.

## Why Codex CLI, and how it is called

- It is already installed and logged in on the developer's machines, needs no
  API key in the plugin, and runs any model Codex supports (`-m`).
- `codex exec` with `--output-schema` gives strict JSON; the sandbox is
  read-only and the working directory is the job folder, which holds the
  context files, the prompt and the reply for later inspection.
- OpenAI's strict schema mode requires every property to be required (so
  optional fields are nullable) and forbids free-form objects, which is why
  the option bag spells out every key.

## Loop in detail

```
request ──► board-context.json ──► codex exec (schema) ──► reply.actions
   ▲                                                            │
   │        results + fresh DRC + unconnected count ◄── apply on board (live)
   └──────────── until done / ask / round limit ────────────────┘
```

Everything the loop changes on the board is part of the plugin run, so one
Ctrl+Z reverts it.

## What to build next, in order

1. Image attachment per round (level 3) and a "review" preset that asks for a
   critique only.
2. Waypoint / layer-preference routing primitives and temporary keepouts
   (level 2), each verified by DRC with rollback.
3. A congestion / failure analysis the plugin computes itself (free cells
   around failed exits, which nets block a connection) and puts in the context,
   so the model's advice is grounded instead of guessed.
4. Reference-artwork profiles (level 4) once reference boards are available.
