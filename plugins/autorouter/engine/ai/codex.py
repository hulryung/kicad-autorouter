"""Running the Codex CLI non-interactively and getting structured JSON back."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

ENV_BINARY = "KICAD_AUTOROUTER_CODEX"      # tests point this at a fake


class CodexError(Exception):
    pass


def find_codex(preferred: str = "") -> Optional[str]:
    """The codex executable: a configured path, the test override, PATH, or
    the usual install places."""
    for cand in (preferred, os.environ.get(ENV_BINARY, ""), shutil.which("codex"),
                 "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
                 str(Path.home() / ".npm-global" / "bin" / "codex"),
                 str(Path.home() / ".local" / "bin" / "codex")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def codex_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_json(text: str) -> dict:
    """The JSON object in a model reply: the whole reply, or the first
    {...} block in it."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise CodexError("The model's reply holds no JSON object:\n" + text[:500])


def run(binary: str, prompt: str, workdir: Path, schema: Optional[dict] = None, model: str = "",
        timeout: float = 600.0, on_output: Optional[Callable[[str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None, poll: Optional[Callable[[], None]] = None,
        images: Optional[List[Path]] = None) -> dict:
    """Run `codex exec` with the prompt on stdin, read-only sandbox, cwd at
    workdir (where context files live), and return the parsed JSON reply.
    on_output gets the CLI's progress lines; poll() is called while waiting
    (the dialog pumps events there)."""
    workdir.mkdir(parents=True, exist_ok=True)
    last = workdir / "codex-last-message.json"
    last.unlink(missing_ok=True)
    cmd = [binary, "exec", "--skip-git-repo-check", "-s", "read-only", "-C", str(workdir),
           "--ephemeral", "-o", str(last)]
    if schema is not None:
        schema_file = workdir / "codex-output-schema.json"
        schema_file.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        cmd += ["--output-schema", str(schema_file)]
    if model:
        cmd += ["-m", model]
    for image in images or []:
        if Path(image).is_file():
            cmd += ["-i", str(image)]
    cmd.append("-")
    (workdir / "codex-prompt.md").write_text(prompt, encoding="utf-8")
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs)
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass
    started = time.time()
    output: List[str] = []
    import threading

    def pump():
        for line in proc.stdout:
            output.append(line.rstrip("\n"))
            if on_output:
                on_output(line.rstrip("\n"))

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    while proc.poll() is None:
        if cancelled and cancelled():
            proc.terminate()
            raise CodexError("Cancelled.")
        if time.time() - started > timeout:
            proc.terminate()
            raise CodexError(f"Codex did not answer within {timeout:.0f}s.")
        if poll:
            poll()
        time.sleep(0.2)
    t.join(timeout=5)
    reply = last.read_text(encoding="utf-8") if last.exists() else ""
    if not reply.strip():
        tail = "\n".join(output[-15:])
        raise CodexError(f"Codex exited with code {proc.returncode} and no final message.\n{tail}")
    (workdir / "codex-reply.txt").write_text(reply, encoding="utf-8")
    return extract_json(reply)
