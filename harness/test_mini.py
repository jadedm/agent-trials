"""Tests for mini.py against a fake OpenAI-compatible server. No network, no API key.

Run: python3 -m unittest discover -s harness -v
Case numbers match the test plan on issue #1.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MINI = str(Path(__file__).resolve().parent / "mini.py")
KEY = "test-key-5b1f9c"


def tool_call(command, call_id="call_1", name="bash", raw_args=None):
    args = raw_args if raw_args is not None else json.dumps({"command": command})
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def reply(calls=None, content=None, usage=(10, 5)):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
    }


def done(text="done"):
    return reply(content=text)


class FakeServer:
    """Serves scripted responses in order and records each request.

    A script item is a dict (sent as 200 JSON), or a tuple (status, body, headers).
    Once the script runs out, the last item repeats.
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                index = min(len(outer.requests) - 1, len(outer.script) - 1)
                item = outer.script[index]
                status, payload, headers = item if isinstance(item, tuple) else (200, item, {})
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MiniTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.dir.name)
        (self.cwd / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        self.dir.cleanup()

    def serve(self, *script):
        server = FakeServer(script)
        self.servers.append(server)
        return server

    def run_mini(self, server_url, task="fix it", **env_overrides):
        env = {k: v for k, v in os.environ.items() if k not in ("MINI_API_KEY", "CEREBRAS_API_KEY")}
        env.update({"MINI_API_KEY": KEY, "MINI_BASE_URL": server_url, "MINI_RETRY_BASE": "0.01"})
        env.update(env_overrides)
        env = {k: v for k, v in env.items() if v is not None}
        return subprocess.run(
            [sys.executable, MINI, task], cwd=self.cwd, env=env,
            capture_output=True, text=True, timeout=60,
        )

    def tool_messages(self, request):
        return [m for m in request["body"]["messages"] if m["role"] == "tool"]

    # 1
    def test_happy_path_fixes_the_bug(self):
        fix = "sed -i.bak 's/a - b/a + b/' calc.py && rm calc.py.bak"
        server = self.serve(reply([tool_call(fix)]), done("fixed add"))
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("a + b", (self.cwd / "calc.py").read_text())
        self.assertIn("fixed add", result.stdout)

    # 2
    def test_tool_result_goes_back_to_the_model(self):
        server = self.serve(reply([tool_call("echo marker-123", "call_x")]), done())
        self.run_mini(server.url)
        self.assertEqual(len(server.requests), 2)
        tools = self.tool_messages(server.requests[1])
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["tool_call_id"], "call_x")
        self.assertIn("exit code: 0", tools[0]["content"])
        self.assertIn("marker-123", tools[0]["content"])

    # 3
    def test_step_limit(self):
        server = self.serve(reply([tool_call("true")]))
        result = self.run_mini(server.url, MINI_MAX_STEPS="3")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(server.requests), 3)
        self.assertIn("step limit of 3 reached", result.stderr)

    # 4
    def test_command_timeout(self):
        server = self.serve(reply([tool_call("sleep 30")]), done())
        start = time.monotonic()
        result = self.run_mini(server.url, MINI_CMD_TIMEOUT="1")
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("timed out after 1s", self.tool_messages(server.requests[1])[0]["content"])

    # 5
    def test_timeout_kills_the_process_group(self):
        marker = self.cwd / "survived"
        command = f"(sleep 3; touch {marker}) & sleep 30"
        server = self.serve(reply([tool_call(command)]), done())
        self.run_mini(server.url, MINI_CMD_TIMEOUT="1")
        time.sleep(4)
        self.assertFalse(marker.exists(), "background child outlived the command timeout")

    # 6
    def test_output_truncation(self):
        command = "python3 -c \"print('START' + 'x' * 50000 + 'END')\""
        server = self.serve(reply([tool_call(command)]), done())
        self.run_mini(server.url, MINI_OUTPUT_CAP="1000")
        content = self.tool_messages(server.requests[1])[0]["content"]
        self.assertLess(len(content), 1200)
        self.assertIn("bytes cut", content)
        self.assertIn("START", content)
        self.assertIn("END", content)

    # 7
    def test_retries_429_then_500(self):
        server = self.serve((429, "slow down", {}), (500, "oops", {}), done())
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(server.requests), 3)

    # 7, Retry-After
    def test_retry_after_is_honoured(self):
        server = self.serve((429, "slow down", {"Retry-After": "1"}), done())
        start = time.monotonic()
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(time.monotonic() - start, 1)

    # 8
    def test_gives_up_after_four_attempts(self):
        server = self.serve((500, "down", {}))
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(server.requests), 4)
        self.assertIn("HTTP 500", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # 9
    def test_no_retry_on_401(self):
        server = self.serve((401, "bad key", {}))
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(server.requests), 1)
        self.assertIn("HTTP 401", result.stderr)

    # 10
    def test_missing_key(self):
        server = self.serve(done())
        result = self.run_mini(server.url, MINI_API_KEY=None)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(server.requests), 0)
        self.assertIn("MINI_API_KEY", result.stderr)

    # 11
    def test_key_never_logged(self):
        server = self.serve((401, f"invalid key {KEY}", {}))
        result = self.run_mini(server.url)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        server2 = self.serve(reply([tool_call("echo hi")]), done())
        result2 = self.run_mini(server2.url)
        self.assertNotIn(KEY, result2.stdout + result2.stderr)
        self.assertEqual(server2.requests[0]["headers"]["Authorization"], f"Bearer {KEY}")

    # 12
    def test_token_usage_reported(self):
        server = self.serve(reply([tool_call("true")], usage=(100, 20)), reply(content="ok", usage=(150, 30)))
        result = self.run_mini(server.url)
        self.assertIn("tokens: prompt=250 completion=50 total=300", result.stderr)

    # 14
    def test_malformed_tool_arguments(self):
        server = self.serve(reply([tool_call(None, raw_args="{not json")]), done())
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not valid JSON", self.tool_messages(server.requests[1])[0]["content"])

    # 15
    def test_unknown_tool(self):
        server = self.serve(reply([tool_call("x", name="open")]), done())
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.tool_messages(server.requests[1])[0]["content"]
        self.assertIn("unknown tool 'open'", content)
        self.assertIn("bash", content)

    # 16
    def test_failing_command_reports_code_and_both_streams(self):
        server = self.serve(reply([tool_call("ls nope; echo out; echo err >&2; exit 3")]), done())
        self.run_mini(server.url)
        content = self.tool_messages(server.requests[1])[0]["content"]
        self.assertIn("exit code: 3", content)
        self.assertIn("out", content)
        self.assertIn("err", content)

    # 17
    def test_several_tool_calls_in_one_response(self):
        calls = [tool_call("echo first", "call_a"), tool_call("echo second", "call_b")]
        server = self.serve(reply(calls), done())
        self.run_mini(server.url)
        messages = server.requests[1]["body"]["messages"]
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual([c["id"] for c in messages[2]["tool_calls"]], ["call_a", "call_b"])
        self.assertEqual([m["tool_call_id"] for m in messages[3:]], ["call_a", "call_b"])
        self.assertIn("first", messages[3]["content"])
        self.assertIn("second", messages[4]["content"])

    # 18, non-JSON
    def test_non_json_response(self):
        server = self.serve((200, "<html>gateway</html>", {}))
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # 18, connection refused
    def test_connection_refused(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = self.run_mini(f"http://127.0.0.1:{port}/v1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot reach", result.stderr)
        self.assertEqual(result.stderr.count("retrying"), 3)
        self.assertNotIn("Traceback", result.stderr)

    # 18, missing choices
    def test_response_without_choices(self):
        server = self.serve({"error": "nope"})
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no choices", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # 19
    def test_settings_precedence(self):
        server = self.serve(done())
        self.run_mini(server.url, CEREBRAS_API_KEY="other-key", MINI_MODEL="some-model")
        request = server.requests[0]
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["body"]["model"], "some-model")

    # 19, fallback key
    def test_cerebras_key_used_when_mini_key_missing(self):
        server = self.serve(done())
        self.run_mini(server.url, MINI_API_KEY=None, CEREBRAS_API_KEY="fallback-key")
        self.assertEqual(server.requests[0]["headers"]["Authorization"], "Bearer fallback-key")


    # 20, review: background processes do not survive a step that exits normally
    def test_background_child_killed_after_normal_exit(self):
        marker = self.cwd / "survived"
        server = self.serve(reply([tool_call(f"(sleep 2; touch {marker}) &")]), done())
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(3)
        self.assertFalse(marker.exists(), "background child outlived its step")

    # 21, review: commands cannot read the API key
    def test_key_not_in_command_environment(self):
        command = 'echo "m=$MINI_API_KEY c=$CEREBRAS_API_KEY"; env'
        server = self.serve(reply([tool_call(command)]), done())
        self.run_mini(server.url, CEREBRAS_API_KEY=KEY)
        content = self.tool_messages(server.requests[1])[0]["content"]
        self.assertIn("m= c=", content)
        self.assertNotIn(KEY, content)

    # 22, review: memory stays bounded for a command that prints without end
    def test_noisy_command_memory_bounded(self):
        server = self.serve(reply([tool_call("yes")]), done())
        env = {k: v for k, v in os.environ.items() if k not in ("MINI_API_KEY", "CEREBRAS_API_KEY")}
        env.update({"MINI_API_KEY": KEY, "MINI_BASE_URL": server.url, "MINI_CMD_TIMEOUT": "3",
                    "MINI_OUTPUT_CAP": "1000"})
        probe = ("import resource, subprocess, sys; "
                 "subprocess.run(sys.argv[1:], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                 "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)")
        out = subprocess.run([sys.executable, "-c", probe, sys.executable, MINI, "go"],
                             cwd=self.cwd, env=env, capture_output=True, text=True, timeout=60)
        peak = int(out.stdout.strip())
        peak_mb = peak / 1e6 if sys.platform == "darwin" else peak / 1e3
        self.assertLess(peak_mb, 150, f"peak memory {peak_mb:.0f} MB")
        self.assertLess(len(self.tool_messages(server.requests[1])[0]["content"]), 1200)

    # 23, review: a process that leaves the group does not hang the step
    def test_detached_process_does_not_hang_step(self):
        command = 'python3 -c "import os, time; os.setsid(); time.sleep(10)" & sleep 30'
        server = self.serve(reply([tool_call(command)]), done())
        start = time.monotonic()
        result = self.run_mini(server.url, MINI_CMD_TIMEOUT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - start, 6)

    # 24, review: arguments sent as an object, not a string
    def test_arguments_as_object(self):
        call = {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": {"command": "echo obj-ok"}}}
        server = self.serve(reply([call]), done())
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("obj-ok", self.tool_messages(server.requests[1])[0]["content"])

    # 25, review: a JSON body that is not an object
    def test_response_not_an_object(self):
        server = self.serve([1, 2])
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a JSON object", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    # 26, review: null token counts
    def test_null_usage(self):
        body = done()
        body["usage"] = {"prompt_tokens": None, "completion_tokens": None}
        server = self.serve(body)
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tokens: prompt=0 completion=0", result.stderr)

    # 27, review: a setting that is not a number
    def test_bad_setting(self):
        server = self.serve(done())
        result = self.run_mini(server.url, MINI_MAX_STEPS="abc")
        self.assertEqual(result.returncode, 2)
        self.assertIn("bad setting", result.stderr)
        self.assertEqual(len(server.requests), 0)
        self.assertNotIn("Traceback", result.stderr)

    # 28, review: a tool call without an id still gets a matching id
    def test_tool_call_without_id(self):
        call = tool_call("echo hi")
        del call["id"]
        server = self.serve(reply([call]), done())
        self.run_mini(server.url)
        messages = server.requests[1]["body"]["messages"]
        given = messages[2]["tool_calls"][0]["id"]
        self.assertTrue(given)
        self.assertEqual(messages[3]["tool_call_id"], given)

    # 29, review: token totals are reported when the run fails
    def test_usage_reported_on_failure(self):
        server = self.serve((500, "down", {}))
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertIn("tokens: prompt=0", result.stderr)

    # 30, review: a tiny output cap still bounds the output
    def test_output_cap_minimum(self):
        command = "python3 -c \"print('x' * 5000)\""
        server = self.serve(reply([tool_call(command)]), done())
        self.run_mini(server.url, MINI_OUTPUT_CAP="0")
        self.assertLess(len(self.tool_messages(server.requests[1])[0]["content"]), 300)


    # 31, review: a key that crosses the 200-character cut of an error body is still redacted
    def test_key_redacted_across_cut(self):
        server = self.serve((401, "x" * 190 + KEY + "y" * 50, {}))
        result = self.run_mini(server.url)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(KEY[:8], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
