"""
AI-assisted routing through the Codex CLI.

  context.py   what the model gets to see: the board, its rules, nets, the
               last run's outcome and the DRC, as JSON          - pcbnew
  codex.py     running `codex exec` non-interactively with a JSON schema  - pure Python
  actions.py   the action schema the model answers with, and applying those
               actions to the board through the built-in router  - pcbnew
  assistant.py the request -> context -> model -> actions -> results loop
"""
