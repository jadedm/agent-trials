#!/usr/bin/env python3
"""A minimal coding-agent harness.

Usage: mini.py <task...>

Sends the task to an OpenAI-compatible chat completions endpoint with one tool, `bash`. Runs each
command the model asks for in the current folder and sends back the exit code and output. Stops
when the model answers without a tool call (exit 0) or at the step limit (exit 2). Exits 1 when the
endpoint fails.

Settings, as environment variables:
  MINI_API_KEY, else CEREBRAS_API_KEY   API key (required; removed from the commands' environment)
  MINI_BASE_URL     default https://api.cerebras.ai/v1
  MINI_MODEL        default gpt-oss-120b
  MINI_MAX_STEPS    model calls before giving up, default 30
  MINI_CMD_TIMEOUT  seconds per command, default 60
  MINI_OUTPUT_CAP   bytes of command output sent back (head and tail), default 10000, minimum 100
  MINI_RETRY_BASE   first retry wait in seconds, default 1
"""

import http.client
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

SYSTEM_PROMPT = (
    "You are a coding agent working in the current directory. Use the bash tool to inspect files, "
    "run commands and make edits. Run one command at a time and read its output before the next. "
    "There is no apply_patch command: edit files with sed, a heredoc or a short python3 script. "
    "Make the smallest change that completes the task, and do not add comments or docstrings. "
    "When the task is done, reply with a short summary and no tool call. "
    f"The shell runs on {platform.system()} ({platform.machine()}); use command flags that work there."
)

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a bash command in the task folder. Returns the exit code and output.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command to run."}},
            "required": ["command"],
        },
    },
}

KEY_VARS = ("MINI_API_KEY", "CEREBRAS_API_KEY")
ATTEMPTS = 4
RETRY_AFTER_CAP = 30
RETRYABLE = {429, 500, 502, 503, 504}
MIN_OUTPUT_CAP = 100
READER_GRACE = 2


class ApiError(Exception):
    """The endpoint failed in a way the loop cannot recover from."""


def log(text):
    print(text, file=sys.stderr, flush=True)


def settings():
    """Reads the environment. Raises ValueError on a setting that is not a number."""
    env = os.environ
    return {
        "key": env.get("MINI_API_KEY") or env.get("CEREBRAS_API_KEY") or "",
        "url": env.get("MINI_BASE_URL", "https://api.cerebras.ai/v1").rstrip("/") + "/chat/completions",
        "model": env.get("MINI_MODEL", "gpt-oss-120b"),
        "max_steps": int(env.get("MINI_MAX_STEPS", "30")),
        "cmd_timeout": float(env.get("MINI_CMD_TIMEOUT", "60")),
        "output_cap": max(int(env.get("MINI_OUTPUT_CAP", "10000")), MIN_OUTPUT_CAP),
        "retry_base": float(env.get("MINI_RETRY_BASE", "1")),
    }


def command_env(key):
    """The environment for model-run commands: no API key under any name."""
    return {k: v for k, v in os.environ.items() if k not in KEY_VARS and v != key}


def retry_wait(attempt, base, retry_after):
    if retry_after and retry_after.isdigit():
        return min(int(retry_after), RETRY_AFTER_CAP)
    return base * (2**attempt)


def error_detail(err, key):
    try:
        body = err.read().decode(errors="replace")
    except Exception:
        return ""
    return body.replace(key, "[key]")[:200]


def post_once(cfg, body):
    """One request. Returns (parsed JSON, None) or (None, (retryable, message, retry_after))."""
    request = urllib.request.Request(
        cfg["url"],
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read()
    except urllib.error.HTTPError as err:
        message = f"HTTP {err.code} from {cfg['url']}: {error_detail(err, cfg['key'])}"
        return None, (err.code in RETRYABLE, message, err.headers.get("Retry-After"))
    except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
        reason = getattr(err, "reason", None) or type(err).__name__
        return None, (True, f"cannot reach {cfg['url']}: {reason}", None)
    try:
        data = json.loads(raw)
    except ValueError:
        return None, (False, f"response from {cfg['url']} is not JSON", None)
    if not isinstance(data, dict):
        return None, (False, f"response from {cfg['url']} is not a JSON object", None)
    return data, None


def call_model(cfg, messages):
    body = {"model": cfg["model"], "messages": messages, "tools": [BASH_TOOL]}
    for attempt in range(ATTEMPTS):
        data, failure = post_once(cfg, body)
        if failure is None:
            return data
        retryable, message, retry_after = failure
        last_try = attempt == ATTEMPTS - 1
        if not retryable or last_try:
            raise ApiError(message)
        wait = retry_wait(attempt, cfg["retry_base"], retry_after)
        log(f"{message}; retrying in {wait:g}s")
        time.sleep(wait)
    raise ApiError("unreachable")


class Capture:
    """Keeps the first and last bytes of a stream, so memory stays bounded however much it prints."""

    def __init__(self, cap):
        self.head_limit = (cap + 1) // 2
        self.tail_limit = cap // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def add(self, chunk):
        with self.lock:
            self.total += len(chunk)
            room = self.head_limit - len(self.head)
            self.head += chunk[:room]
            self.tail += chunk[room:] if room > 0 else chunk
            del self.tail[: max(len(self.tail) - self.tail_limit, 0)]

    def drain(self, fd):
        """Reads until end of stream, then closes fd. The caller hands over its own duplicate, so a
        thread still reading after the step ends never reads from a reused file number."""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                self.add(chunk)
        except OSError:
            return
        finally:
            os.close(fd)

    def text(self):
        with self.lock:
            cut = self.total - len(self.head) - len(self.tail)
            head = self.head.decode(errors="replace")
            tail = self.tail.decode(errors="replace")
        if cut <= 0:
            return head + tail
        return f"{head}\n[... {cut} bytes cut ...]\n{tail}"


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_bash(command, cfg):
    # Own session, so killing the group kills everything the command started, not just the shell.
    proc = subprocess.Popen(
        ["bash", "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=command_env(cfg["key"]),
    )
    capture = Capture(cfg["output_cap"])
    reader = threading.Thread(target=capture.drain, args=(os.dup(proc.stdout.fileno()),), daemon=True)
    proc.stdout.close()
    reader.start()
    try:
        proc.wait(timeout=cfg["cmd_timeout"])
        status = f"exit code: {proc.returncode}"
    except subprocess.TimeoutExpired:
        kill_group(proc.pid)
        proc.wait()
        status = f"timed out after {cfg['cmd_timeout']:g}s and was killed"
    # Nothing started in the background survives the step.
    kill_group(proc.pid)
    # A process that left the group can still hold the pipe open; do not wait for it.
    reader.join(READER_GRACE)
    return f"{status}\n{capture.text()}"


def parse_args(function):
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw, None
    try:
        return json.loads(raw or ""), None
    except (ValueError, TypeError) as err:
        return None, f"error: arguments are not valid JSON ({err}). Send {{\"command\": \"...\"}}."


def run_tool_call(call, cfg):
    function = call.get("function") or {}
    name = function.get("name")
    if name != "bash":
        return f"error: unknown tool {name!r}. The only tool is bash."
    args, problem = parse_args(function)
    if problem:
        return problem
    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command:
        return 'error: missing "command". Send {"command": "..."}.'
    log(f"$ {command}")
    result = run_bash(command, cfg)
    log(f"  {result.splitlines()[0]}")
    return result


def as_int(value):
    return value if isinstance(value, int) else 0


def report_usage(usage):
    log(f"tokens: prompt={usage['prompt']} completion={usage['completion']} "
        f"total={usage['prompt'] + usage['completion']}")


def first_message(data):
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ApiError("response has no choices[0].message")
    if not isinstance(message, dict):
        raise ApiError("response has no choices[0].message")
    return message


def tool_calls(message, step):
    calls = [c for c in (message.get("tool_calls") or []) if isinstance(c, dict)]
    for index, call in enumerate(calls):
        call["id"] = call.get("id") or f"call_{step}_{index}"
    return calls


def run(task, cfg, usage):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
    for step in range(1, cfg["max_steps"] + 1):
        log(f"step {step}")
        data = call_model(cfg, messages)
        reported = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage["prompt"] += as_int(reported.get("prompt_tokens"))
        usage["completion"] += as_int(reported.get("completion_tokens"))
        message = first_message(data)
        calls = tool_calls(message, step)
        assistant = {"role": "assistant", "content": message.get("content")}
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)
        if not calls:
            print(message.get("content") or "")
            return 0
        for call in calls:
            result = run_tool_call(call, cfg)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    log(f"step limit of {cfg['max_steps']} reached")
    return 2


def main(argv):
    task = " ".join(argv).strip()
    if not task:
        log("usage: mini.py <task...>")
        return 2
    try:
        cfg = settings()
    except ValueError as err:
        log(f"bad setting: {err}")
        return 2
    if not cfg["key"]:
        log("set MINI_API_KEY or CEREBRAS_API_KEY")
        return 2
    usage = {"prompt": 0, "completion": 0}
    try:
        return run(task, cfg, usage)
    except ApiError as err:
        log(f"error: {err}")
        return 1
    finally:
        report_usage(usage)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
