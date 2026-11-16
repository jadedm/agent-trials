#!/usr/bin/env bash
# Usage: agent-trials.sh <label> <runs> <agent command...>
# Runs one fix-the-failing-test task <runs> times, each in a fresh git repo, each capped at LIMIT
# seconds (default 240). The prompt is appended as the last argument of the agent command.
# The agent runs in its own process group, and that group is killed when the agent exits, times
# out, or the harness is interrupted, so nothing the agent started outlives its run.
# Runs go to $TRIALS_DIR/<label>/run<N> (default $TMPDIR/agent-trials). One summary line per run.
set -u
if [ $# -lt 3 ]; then
  echo "usage: $0 <label> <runs> <agent command...>" >&2
  exit 2
fi
label="$1"; runs="$2"; shift 2
case "$runs" in
  ''|*[!0-9]*) echo "runs must be a positive number, got '$runs'" >&2; exit 2 ;;
esac
LIMIT="${LIMIT:-240}"
VERDICT_LIMIT=30
ROOT="${TRIALS_DIR:-${TMPDIR:-/tmp}/agent-trials}/$label"
PROMPT='The test in this folder fails. Run it with: python3 test_calc.py. Find and fix the bug in calc.py, then run the test again to confirm it prints OK. Do not edit test_calc.py.'
# Keep Python from writing __pycache__ into the run folder, so it does not show up as a change.
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$ROOT"
# Each run cd's into its own folder, so the root must be absolute or run 2 lands inside run 1.
ROOT="$(cd "$ROOT" && pwd)"

# capped <seconds> <command...>: runs the command in its own process group. Exit 124 on timeout,
# 130 or 143 when interrupted, 127 when the command is missing, 128 + n when killed by signal n.
capped() {
  perl -e '
    my $limit = shift @ARGV;
    my $pid = fork();
    if ($pid == 0) { setpgrp(0, 0); exec @ARGV or exit 127; }
    setpgrp($pid, $pid);
    sub stop { kill "TERM", -$pid; sleep 3; kill "KILL", -$pid; waitpid($pid, 0); }
    local $SIG{ALRM} = sub { stop(); exit 124; };
    local $SIG{INT} = sub { stop(); exit 130; };
    local $SIG{TERM} = sub { stop(); exit 143; };
    alarm $limit; waitpid($pid, 0); my $status = $?; alarm 0;
    kill "KILL", -$pid;
    exit(($status & 127) ? 128 + ($status & 127) : $status >> 8);
  ' "$@"
}

for i in $(seq 1 "$runs"); do
  T="$ROOT/run$i"; rm -rf "$T"; mkdir -p "$T"; cd "$T" || exit 1
  printf 'def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n' > calc.py
  printf 'from calc import add, mul\n\nassert add(2, 3) == 5, "add is wrong"\nassert mul(4, 5) == 20, "mul is wrong"\nprint("OK")\n' > test_calc.py
  git init -q && git add . && git -c user.name=t -c user.email=t@example.invalid commit -qm init
  start=$(date +%s)
  capped "$LIMIT" "$@" "$PROMPT" > out.txt 2> err.txt < /dev/null
  rc=$?
  secs=$(( $(date +%s) - start ))
  if [ "$rc" -eq 130 ] || [ "$rc" -eq 143 ]; then
    echo "$label run $i: interrupted" >&2
    exit "$rc"
  fi
  # Tracked edits and new files both count; ignore the run's own log files.
  changed=$(git status --porcelain | grep -vE ' (out|err)\.txt$' | cut -c4- | tr '\n' ' ')
  # The verdict reruns the test itself, capped, since the agent may have left it hanging.
  vout=$(capped "$VERDICT_LIMIT" python3 test_calc.py 2>&1 < /dev/null)
  vrc=$?
  result=$(printf '%s\n' "$vout" | tail -1)
  if [ "$vrc" -eq 124 ]; then result="test timed out after ${VERDICT_LIMIT}s"; fi
  echo "$label run $i: exit=$rc secs=$secs changed=[${changed}] test='${result}'"
done
