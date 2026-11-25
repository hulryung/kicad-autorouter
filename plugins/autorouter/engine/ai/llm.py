"""The language models the assistant can use, behind one call.

  codex-cli      OpenAI Codex CLI (`codex exec`), logged in once; no key in the plugin
  claude-cli     Claude Code CLI (`claude -p`), logged in once; no key in the plugin
  anthropic-api  the Claude API through the `anthropic` SDK (ANTHROPIC_API_KEY or `ant auth login`)
  openai-api     the OpenAI API through the `openai` SDK (OPENAI_API_KEY)

Every backend answers with the JSON object the caller's schema describes.
The API backends need their SDK in KiCad's own Python:

  <KiCad python3> -m pip install anthropic      (or openai)

run() keeps the dialog alive while it waits: the CLIs run as processes and
the API calls in a separate process, with poll() called every 0.2 s and cancelled()
checked as often.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import codex
from .codex import CodexError, extract_json

CODEX_CLI, CLAUDE_CLI, ANTHROPIC_API, OPENAI_API = "codex-cli", "claude-cli", "anthropic-api", "openai-api"
KINDS = [CODEX_CLI, CLAUDE_CLI, ANTHROPIC_API, OPENAI_API]
LABELS = {CODEX_CLI: "Codex CLI (OpenAI, logged in)", CLAUDE_CLI: "Claude Code CLI (logged in)",
          ANTHROPIC_API: "Claude API (anthropic SDK, API key)", OPENAI_API: "OpenAI API (openai SDK, API key)"}
DEFAULT_MODEL = {ANTHROPIC_API: "claude-opus-5-5", OPENAI_API: "gpt-5"}
ENV_CLAUDE = "KICAD_AUTOROUTER_CLAUDE"       # tests point this at a fake


class LLMError(CodexError):
    """A model call that gave no usable answer (a CodexError, so callers that
    catch those keep working)."""


@dataclass
class Provider:
    kind: str = CODEX_CLI
    model: str = ""          # "" = the backend's default
    binary: str = ""         # the CLIs: a configured path, "" = found automatically
    effort: str = "high"     # Claude API: low / medium / high / xhigh / max

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, self.kind)


def find_claude(preferred: str = "") -> Optional[str]:
    for cand in (preferred, os.environ.get(ENV_CLAUDE, ""), shutil.which("claude"),
                 str(Path.home() / ".claude" / "local" / "claude"), "/opt/homebrew/bin/claude",
                 "/usr/local/bin/claude", str(Path.home() / ".local" / "bin" / "claude"),
                 str(Path.home() / ".npm-global" / "bin" / "claude")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def status(p: Provider) -> Tuple[bool, str]:
    """Can this backend run here, and a line for the dialog."""
    if p.kind == CODEX_CLI:
        b = codex.find_codex(p.binary)
        return (True, f"codex: {b}  ({codex.codex_version(b)})") if b else \
            (False, "codex CLI not found: install with  npm i -g @openai/codex  and log in once")
    if p.kind == CLAUDE_CLI:
        b = find_claude(p.binary)
        return (True, f"claude: {b}") if b else \
            (False, "Claude Code CLI not found: install it (claude.com/claude-code) and log in once")
    module = "anthropic" if p.kind == ANTHROPIC_API else "openai"
    try:
        __import__(module)
    except ImportError:
        return False, (f"the {module} package is not in KiCad's Python: run  \"{sys.executable}\" -m pip install "
                       f"{module}")
    key = "ANTHROPIC_API_KEY" if p.kind == ANTHROPIC_API else "OPENAI_API_KEY"
    note = "" if os.environ.get(key) else f" ({key} not set: a logged-in profile is needed)"
    return True, f"{module} SDK, model {p.model or DEFAULT_MODEL[p.kind]}{note}"


def as_provider(backend) -> Provider:
    """A Provider, or a codex binary path as the callers passed it before."""
    if isinstance(backend, Provider):
        return backend
    return Provider(CODEX_CLI, binary=str(backend or ""))


def run(backend, prompt: str, workdir: Path, schema: Optional[dict] = None, model: str = "",
        timeout: float = 600.0, on_output: Optional[Callable[[str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None, poll: Optional[Callable[[], None]] = None,
        images: Optional[List[Path]] = None) -> dict:
    """One model call: the prompt (and images), the JSON reply."""
    p = as_provider(backend)
    model = model or p.model
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "llm-provider.txt").write_text(f"{p.kind} {model}\n", encoding="utf-8")
    if p.kind == CODEX_CLI:
        binary = codex.find_codex(p.binary)
        if not binary:
            raise LLMError(status(p)[1])
        return codex.run(binary, prompt, workdir, schema=schema, model=model, timeout=timeout,
                         on_output=on_output, cancelled=cancelled, poll=poll, images=images)
    if p.kind == CLAUDE_CLI:
        return _claude_cli(p, prompt, workdir, schema, model, timeout, on_output, cancelled, poll, images)
    if p.kind not in (ANTHROPIC_API, OPENAI_API):
        raise LLMError(f"Unknown provider: {p.kind}")
    ok, why = status(p)
    if not ok:
        raise LLMError(why)
    (workdir / "llm-prompt.md").write_text(prompt, encoding="utf-8")
    if on_output:
        on_output(f"asking {p.label}, model {model or DEFAULT_MODEL[p.kind]}...")
    text = _api_process(p, prompt, workdir, schema, model, images, timeout, cancelled, poll)
    (workdir / "llm-reply.txt").write_text(text, encoding="utf-8")
    return extract_json(text)


def _api_process(p, prompt, workdir, schema, model, images, timeout, cancelled, poll):
    from .. import isolated
    workdir = workdir.resolve()
    request = workdir / 'api-request.json'
    reply = workdir / 'api-result.json'
    reply.unlink(missing_ok=True)
    request.write_text(json.dumps({'provider': asdict(p), 'prompt': prompt, 'schema': schema,
                                  'model': model, 'images': [str(Path(i).resolve()) for i in images or []],
                                  'timeout': min(60.0, timeout)}), encoding='utf-8')
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from autorouter.engine.ai.llm import _api_worker; "
            "sys.exit(_api_worker(sys.argv[2], sys.argv[3]))")
    try:
        exit_code = isolated.run([isolated.python_executable(), '-c', code,
                                  str(Path(__file__).resolve().parents[3]), str(request), str(reply)],
                                 workdir, timeout, cancelled, poll)
    except (RuntimeError, TimeoutError) as exc:
        raise LLMError(str(exc)) from exc
    if not reply.exists():
        raise LLMError(f'API worker exited {exit_code}; see worker-stderr.log')
    result = json.loads(reply.read_text(encoding='utf-8'))
    if exit_code != 0 or 'error' in result:
        raise LLMError(result.get('error', f'API worker exited {exit_code}'))
    return result['text']


def _api_worker(request_path, reply_path):
    data = json.loads(Path(request_path).read_text(encoding='utf-8'))
    try:
        p = Provider(**data['provider'])
        call = _anthropic_api if p.kind == ANTHROPIC_API else _openai_api
        text = call(p, data['prompt'], data['schema'], data['model'], data['images'], data['timeout'])
        result, code = {'text': text}, 0
    except Exception as exc:
        result, code = {'error': f'{type(exc).__name__}: {exc}'}, 1
    Path(reply_path).write_text(json.dumps(result), encoding='utf-8')
    return code


def _image_b64(path: Path) -> Tuple[str, str]:
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return media, base64.standard_b64encode(path.read_bytes()).decode("ascii")


# ---- Claude Code CLI -----------------------------------------------------------

def _claude_cli(p, prompt, workdir, schema, model, timeout, on_output, cancelled, poll, images):
    binary = find_claude(p.binary)
    if not binary:
        raise LLMError(status(p)[1])
    pics = [Path(i) for i in images or [] if Path(i).is_file()]
    if pics:
        # the CLI reads images with its Read tool; the files are in (or next to) the job folder
        prompt += "\n\nImages of the board for this round (open them with the Read tool):\n" + \
                  "\n".join(f"- {i}" for i in pics)
    cmd = [binary, "-p", "--output-format", "json", "--allowedTools", "Read",
           "--add-dir", str(workdir)] + [a for i in pics for a in ("--add-dir", str(i.parent))]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    if model:
        cmd += ["--model", model]
    (workdir / "claude-prompt.md").write_text(prompt, encoding="utf-8")
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    proc = subprocess.Popen(cmd, cwd=str(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kwargs)
    out = {}

    def talk():
        out["stdout"], out["stderr"] = proc.communicate(prompt)

    t = threading.Thread(target=talk, daemon=True)
    t.start()
    started = time.time()
    try:
        if on_output:
            on_output(f"asking Claude Code ({binary})...")
        while t.is_alive():
            if cancelled and cancelled():
                raise LLMError("Cancelled.")
            if time.time() - started > timeout:
                raise LLMError(f"Claude Code did not answer within {timeout:.0f}s.")
            if poll:
                poll()
            t.join(0.2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        t.join(timeout=3)
        (workdir / "claude-reply.json").write_text(out.get("stdout", ""), encoding="utf-8")
        (workdir / "claude-stderr.log").write_text(out.get("stderr", ""), encoding="utf-8")
    raw = out.get("stdout", "")
    if proc.returncode != 0:
        raise LLMError(f"Claude Code exited with code {proc.returncode}:\n{(out.get('stderr') or raw)[-800:]}")
    try:
        result = json.loads(raw)
    except ValueError:
        raise LLMError(f"Claude Code exited with code {proc.returncode}:\n{(out.get('stderr') or raw)[-800:]}")
    if not isinstance(result, dict):
        raise LLMError("Claude Code returned a non-object result.")
    if result.get("is_error"):
        raise LLMError(f"Claude Code: {str(result.get('result'))[:800]}")
    if isinstance(result.get("structured_output"), dict):
        return result["structured_output"]
    return extract_json(str(result.get("result", "")))


# ---- Claude API ----------------------------------------------------------------

def _anthropic_api(p, prompt, schema, model, images, timeout=60.0) -> str:
    import anthropic
    client = anthropic.Anthropic(timeout=timeout, max_retries=0)        # ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile
    content = []
    for image in images or []:
        image = Path(image)
        if image.is_file():
            media, data = _image_b64(image)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    content.append({"type": "text", "text": prompt})
    output_config = {"effort": p.effort or "high"}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    request = dict(model=model or DEFAULT_MODEL[ANTHROPIC_API], max_tokens=64000,
                   messages=[{"role": "user", "content": content}], output_config=output_config,
                   # a declined request is re-run on a fallback model chosen by the API
                   betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        message = _stream(client, request)
    except anthropic.BadRequestError as e:
        if "format" not in output_config:
            raise LLMError(f"Claude API: {e.message}")
        # a schema the structured-output check does not take: ask for the JSON in words instead
        output_config.pop("format")
        request["messages"][0]["content"][-1]["text"] = (
            prompt + "\n\nAnswer with one JSON object only, matching this schema:\n" + json.dumps(schema))
        message = _stream(client, request)
    except anthropic.AuthenticationError:
        raise LLMError("Claude API: no valid key (set ANTHROPIC_API_KEY, or log in with `ant auth login`).")
    except anthropic.RateLimitError as e:
        raise LLMError(f"Claude API: rate limited, retry after {e.response.headers.get('retry-after', '60')} s.")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise LLMError("Claude API: no connection to the API.")
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise LLMError(f"Claude declined the request ({getattr(details, 'category', None) or 'no category'}).")
    if message.stop_reason == "max_tokens":
        raise LLMError("Claude's answer was cut off at the token limit.")
    return "".join(b.text for b in message.content if b.type == "text")


def _stream(client, request):
    with client.beta.messages.stream(**request) as stream:
        return stream.get_final_message()


# ---- OpenAI API ----------------------------------------------------------------

def _openai_api(p, prompt, schema, model, images, timeout=60.0) -> str:
    import openai
    client = openai.OpenAI(timeout=timeout, max_retries=0)              # OPENAI_API_KEY
    content = [{"type": "input_text", "text": prompt}]
    for image in images or []:
        image = Path(image)
        if image.is_file():
            media, data = _image_b64(image)
            content.append({"type": "input_image", "image_url": f"data:{media};base64,{data}"})
    request = dict(model=model or DEFAULT_MODEL[OPENAI_API], input=[{"role": "user", "content": content}])
    if schema is not None:
        request["text"] = {"format": {"type": "json_schema", "name": "reply", "schema": schema, "strict": True}}
    try:
        response = client.responses.create(**request)
    except openai.AuthenticationError:
        raise LLMError("OpenAI API: no valid key (set OPENAI_API_KEY).")
    except openai.APIStatusError as e:
        raise LLMError(f"OpenAI API error {e.status_code}: {e.message}")
    except openai.APIConnectionError:
        raise LLMError("OpenAI API: no connection to the API.")
    return response.output_text
