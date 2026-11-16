# agent-trials

Checks whether a coding agent and a model can complete a basic tool-using loop: run a test, read a
file, edit it, run the test again. Each run gets a fresh git repo with a one-line bug in `calc.py`
and a plain `python3` test, so no test framework or network access is needed.

It proves tool calls work and shows how long the loop takes. It does not prove a model can handle a
real ticket.

Requirements: bash, perl, python3 and git. Tested on macOS; CI runs the self-test on Ubuntu.

## Usage

```bash
./agent-trials.sh <label> <runs> <agent command...>
```

The prompt is appended as the last argument, so the command must end where the prompt goes.

```bash
# opencode with a model served by Ollama (needs an Ollama provider in your opencode config)
./agent-trials.sh opencode-ollama 3 opencode run --standalone --auto -m ollama/gpt-oss:20b

# Goose with a model served by Ollama
GOOSE_MODE=auto ./agent-trials.sh goose-qwen 3 goose run --no-session --provider ollama \
  --model qwen3-coder:30b --with-builtin developer --max-turns 30 -t

# opencode with a model hosted on Cerebras
CEREBRAS_API_KEY=... examples/cerebras.sh gpt-oss-120b 3
```

Settings, as environment variables:

- `LIMIT`: seconds per run, default 240.
- `TRIALS_DIR`: where run folders go, default `$TMPDIR/agent-trials`.

The agent command runs from inside each run folder, so give it as a command on your `PATH` or an
absolute path, not `./something`. The agents run with approvals turned off in that folder. Run them
on a machine where that is acceptable.

## Output

One line per run:

```
opencode-cerebras-gpt-oss-120b run 1: exit=0 secs=6 changed=[calc.py ] test='OK'
```

- `test='OK'` is the verdict: the script reruns the test itself after the agent stops.
- `exit` is the agent's own exit code: 124 when the run hit the limit and was killed (such a run
  takes LIMIT + 3 seconds, the grace between TERM and KILL), 127 when the agent command was not
  found, 128 plus the signal number when the agent died from a signal.
- `test='test timed out after 30s'` means the agent left the code hanging; the verdict run is capped
  at 30 seconds.
- `changed` lists edited and new files, from `git status`.

Each run folder keeps `out.txt`, `err.txt` and the git repo. Check `git -C <run folder> diff` before
trusting a pass, since an agent can pass by editing the test or by rewriting more than the bug.

## Example results

One machine, one day (November 2026), opencode as the agent, three runs each. Every run made the
same one-line fix (`a - b` to `a + b`) and nothing else.

| Model | Where | Passed | Seconds per run |
|---|---|---|---|
| gpt-oss 20B | Ollama on an Apple Silicon laptop | 3 of 3 | 57, 17, 14 |
| gpt-oss 120B | Cerebras | 3 of 3 | 6, 5, 6 |

The first local run includes loading the model into memory. On Cerebras most of each run is agent
startup and running the test, not model time: a single request for a short function took 0.07
seconds of model time.

## A minimal harness

`harness/mini.py` is a coding agent in about 320 lines of standard-library Python, small enough to
read in one sitting. It offers the model one tool, `bash`, runs each command in the task folder, and
sends back the exit code and output, keeping only the head and tail of long output. Each command
runs in its own process group, which is killed when the command times out or finishes, so nothing
started in the background survives into the next step. The API key is removed from the commands'
environment. It stops when
the model answers without a tool call, or at a step limit. It works with any OpenAI-compatible chat
completions endpoint and defaults to Cerebras.

```bash
CEREBRAS_API_KEY=... ./agent-trials.sh mini 3 python3 "$PWD/harness/mini.py"
```

Use an absolute path, since each trial runs in its own folder. Settings (`MINI_BASE_URL`,
`MINI_MODEL`, `MINI_MAX_STEPS`, `MINI_CMD_TIMEOUT` and others) are listed at the top of the file.
Each step and the total token count go to stderr, so `err.txt` in a run folder shows what the run did
and cost.

What three versions of the system prompt did, on the same bug with `gpt-oss-120b` on Cerebras, three
runs each, all passing:

| Prompt | Seconds per run | Tokens per run | What the transcripts showed |
|---|---|---|---|
| Basic instructions | 7, 7, 7 | 6,359 to 6,929 | Tried `apply_patch` 2 or 3 times, then rewrote the file and added a docstring |
| Plus "no `apply_patch`, smallest change, no docstrings" | 8, 6, 8 | 6,050 to 7,145 | One-line diffs; still tried `apply_patch`; GNU `sed -i` failed on macOS |
| Plus the operating system | 5, 6, 5 | 4,113 to 5,183 | Used `sed -i ''` first time; still tries `apply_patch` once or twice |

`gpt-oss` models appear to have been trained with Codex's `apply_patch` tool, and keep reaching for
it even when told it does not exist. Offering an `apply_patch` tool is the obvious next experiment.

Tests run against a fake server, with no network or key:

```bash
python3 -m unittest discover -s harness -v
```

## Agent notes

- **opencode** works with Cerebras through its built-in `cerebras` provider. Pass `--standalone`,
  or the background service will not see `CEREBRAS_API_KEY`.
- **Codex CLI** (0.164) cannot use Cerebras. It only accepts `wire_api = "responses"` for custom
  providers, and Cerebras returns 404 on `/v1/responses`.
- The agent runs in its own process group, and the whole group is killed when the agent exits,
  times out or the harness is interrupted. An earlier version killed only the top process, and some
  agents start a wrapper script that left the real binary running.

## Tests

```bash
test/selftest.sh
```

Runs the harness with fake agents and no network, in about a minute. It checks that a fix passes, a
no-op fails, an agent past `LIMIT` is killed, background processes an agent leaves behind are killed
whether it timed out or exited, a hanging test does not hang the harness, a relative `TRIALS_DIR`
works, and the prompt arrives as the last argument.

## License

MIT. See [LICENSE](LICENSE).
