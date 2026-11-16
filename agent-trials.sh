#!/usr/bin/env bash
# Usage: agent-trials.sh <label> <runs> <agent command...>
# Runs one fix-the-failing-test task <runs> times, each in a fresh git repo, each capped at LIMIT
# seconds (default 240). The prompt is appended as the last argument of the agent command.
# The agent runs in its own process group, and that group is killed when the agent exits, times
# out, or the harness is interrupted. A process that leaves the group (setsid) is not reached.
# Runs go to $TRIALS_DIR/<label>/run<N> (default $TMPDIR/agent-trials). One summary line per run.
set -u
if [ $# -lt 3 ]; then
  echo "usage: $0 <label> <runs> <agent command...>" >&2
  exit 2
fi
label="$1"; runs="$2"; shift 2
LIMIT="${LIMIT:-240}"
VERDICT_LIMIT=30

is_positive_int() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; [ "$1" -gt 0 ]; }
is_safe_label() {
  case "$1" in ''|.|..|*[!A-Za-z0-9._:+@-]*) return 1 ;; esac
}
is_positive_int "$runs" || { echo "runs must be a positive whole number, got '$runs'" >&2; exit 2; }
is_positive_int "$LIMIT" || { echo "LIMIT must be a positive whole number, got '$LIMIT'" >&2; exit 2; }
is_safe_label "$label" || { echo "label may use letters, digits and . _ : + @ - only, got '$label'" >&2; exit 2; }

ROOT="${TRIALS_DIR:-${TMPDIR:-/tmp}/agent-trials}/$label"
PROMPT='The test in this folder fails. Run it with: python3 test_calc.py. Find and fix the bug in calc.py, then run the test again to confirm it prints OK. Do not edit test_calc.py.'
# Keep Python from writing __pycache__ into the run folder, so it does not show up as a change.
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$ROOT" || { echo "cannot create $ROOT" >&2; exit 1; }
# Each run cd's into its own folder, so the root must be absolute or run 2 lands inside run 1.
ROOT="$(cd "$ROOT" && pwd)" || { echo "cannot enter $ROOT" >&2; exit 1; }
[ -n "$ROOT" ] || { echo "empty trials root" >&2; exit 1; }

# capped <seconds> <command...>: runs the command in its own process group. Exit 124 on timeout,
# 130 or 143 when interrupted, 127 when the command is missing or cannot be started, 128 + n when
# killed by signal n.
capped() {
  perl -e '
    my $limit = shift @ARGV;
    my $pid = fork();
    exit 127 unless defined $pid;
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

# Ctrl-C reaches this shell as well as the agent. Decide on that, not on the child's exit code,
# which the agent or the code it wrote can set to 130 on its own.
interrupted=0
trap 'interrupted=130' INT
trap 'interrupted=143' TERM
stop_if_interrupted() {
  if [ "$interrupted" -ne 0 ]; then
    echo "$label run $1: interrupted" >&2
    exit "$interrupted"
  fi
}

for i in $(seq 1 "$runs"); do
  T="$ROOT/run$i"; rm -rf "$T"
  if ! { mkdir -p "$T" && cd "$T"; }; then
    echo "cannot use $T" >&2
    exit 1
  fi
  printf 'def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n' > calc.py
  printf 'from calc import add, mul\n\nassert add(2, 3) == 5, "add is wrong"\nassert mul(4, 5) == 20, "mul is wrong"\nprint("OK")\n' > test_calc.py
  if ! { git init -q && git add . && git -c user.name=t -c user.email=t@example.invalid commit -qm init; }; then
    echo "git setup failed in $T" >&2
    exit 1
  fi
  start=$(date +%s)
  capped "$LIMIT" "$@" "$PROMPT" > out.txt 2> err.txt < /dev/null
  rc=$?
  secs=$(( $(date +%s) - start ))
  stop_if_interrupted "$i"
  # Tracked edits and new files both count; ignore the harness's own files.
  changed=$(git status --porcelain | grep -vE ' (out|err|verdict)\.txt$' | cut -c4- | tr '\n' ' ')
  # The verdict reruns the test itself, capped, into a file: a command substitution would wait
  # for any process that escaped the group and still holds the pipe.
  capped "$VERDICT_LIMIT" python3 test_calc.py > verdict.txt 2>&1 < /dev/null
  vrc=$?
  stop_if_interrupted "$i"
  result=$(tail -1 verdict.txt)
  if [ "$vrc" -eq 124 ]; then
    result="test timed out after ${VERDICT_LIMIT}s"
  elif [ "$vrc" -ne 0 ]; then
    result="$result (exit $vrc)"
  fi
  echo "$label run $i: exit=$rc secs=$secs changed=[${changed}] test='${result}'"
  stop_if_interrupted "$i"
done
