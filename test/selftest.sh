#!/usr/bin/env bash
# Checks the harness itself with fake agents and no network:
#   1. an agent that fixes the bug must be reported as test='OK'
#   2. an agent that does nothing must be reported as failing
#   3. an agent that sleeps past LIMIT must be killed and reported as exit=124
#   4. a background process left by a timed-out agent must be killed too
#   5. a background process left by an agent that exits normally must be killed too
#   6. an agent that leaves the test hanging must not hang the harness
#   7. a relative TRIALS_DIR must not nest run 2 inside run 1
#   8. the prompt must arrive as the agent's last argument
#   9. a test that prints OK but exits non-zero must not be a pass
#  10. a detached process started by the test must not hang the verdict
#  11. labels that leave TRIALS_DIR must be refused
#  12. an unusable TRIALS_DIR must stop the harness
#  13. zero or non-numeric runs and LIMIT must be refused
#  14. an agent or test exiting 130 must not stop the trial set
#  15. a real Ctrl-C must stop the trial set and kill the agent
#  16. TERM must stop the trial set with exit 143
#  17. labels with ':' (model names) must be accepted
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
harness="$here/../agent-trials.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export TRIALS_DIR="$work/trials"
passed=0

fail() { echo "FAIL: $1" >&2; exit 1; }
expect() {
  local name="$1" line="$2" pattern="$3"
  echo "$line"
  # shellcheck disable=SC2254
  case "$line" in
    $pattern) passed=$((passed + 1)) ;;
    *) fail "$name" ;;
  esac
}

# The harness appends the prompt as the last argument; most fakes ignore it.
fix='python3 -c "import pathlib; p = pathlib.Path(\"calc.py\"); p.write_text(p.read_text().replace(\"a - b\", \"a + b\"))"'

expect "fixing agent reported as a pass" \
  "$("$harness" fixer 1 sh -c "$fix" _)" "*exit=0 *changed=\[calc.py \]*test='OK'*"

expect "no-op agent reported as a failure" \
  "$("$harness" noop 1 true)" "*test='AssertionError: add is wrong (exit 1)'*"

expect "agent past LIMIT killed with exit=124" \
  "$(LIMIT=1 "$harness" sleeper 1 sh -c 'sleep 30' _)" "*exit=124 *"

marker="$work/timeout-child-survived"
LIMIT=1 "$harness" orphan-timeout 1 sh -c "(sleep 5; touch '$marker') & sleep 30" _ > /dev/null
sleep 6
[ ! -e "$marker" ] || fail "background child of a timed-out agent survived"
echo "timed-out agent's background child was killed"; passed=$((passed + 1))

marker="$work/exit-child-survived"
"$harness" orphan-exit 1 sh -c "(sleep 3; touch '$marker') &" _ > /dev/null
sleep 4
[ ! -e "$marker" ] || fail "background child of an agent that exited survived"
echo "exited agent's background child was killed"; passed=$((passed + 1))

# Run under a 90s bound in its own process group, so a harness that hangs fails this test
# and leaves nothing spinning, instead of hanging the self-test.
hang='printf "while True:\n    pass\n" > calc.py'
line=$(perl -e '
  my $pid = fork();
  if ($pid == 0) { setpgrp(0, 0); exec @ARGV or exit 127; }
  local $SIG{ALRM} = sub { kill "KILL", -$pid; waitpid($pid, 0); exit 124; };
  alarm 90; waitpid($pid, 0); exit($? >> 8);
' "$harness" hanger 1 sh -c "$hang" _) || fail "harness hung on a hanging test"
expect "hanging test reported as timed out" "$line" "*test='test timed out after 30s'*"

(cd "$work" && TRIALS_DIR=rel "$harness" relative 2 true > /dev/null)
[ -d "$work/rel/relative/run2" ] || fail "relative TRIALS_DIR nested run 2 inside run 1"
echo "relative TRIALS_DIR kept runs side by side"; passed=$((passed + 1))

# shellcheck disable=SC2016 # $1 is for the inner sh, not this shell
"$harness" prompt 1 sh -c 'printf "%s" "$1" > got-prompt.txt' _ > /dev/null
grep -q "Find and fix the bug in calc.py" "$TRIALS_DIR/prompt/run1/got-prompt.txt" \
  || fail "prompt was not passed as the last argument"
echo "prompt arrived as the last argument"; passed=$((passed + 1))

# 9: prints OK, then exits 1.
okfail='printf "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n\nimport atexit, os\natexit.register(lambda: os._exit(1))\n" > calc.py'
expect "OK with a non-zero exit is not a pass" \
  "$("$harness" okfail 1 sh -c "$okfail" _)" "*test='OK (exit 1)'*"

# 10: calc.py starts a detached process on import that would hold a pipe for 50 s.
detach='printf "import subprocess\nsubprocess.Popen([\"sleep\", \"50\"], start_new_session=True)\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n" > calc.py'
start=$(date +%s)
line=$(perl -e '
  my $pid = fork();
  if ($pid == 0) { setpgrp(0, 0); exec @ARGV or exit 127; }
  local $SIG{ALRM} = sub { kill "KILL", -$pid; waitpid($pid, 0); exit 124; };
  alarm 90; waitpid($pid, 0); exit($? >> 8);
' "$harness" detach 1 sh -c "$detach" _) || fail "harness hung on a detached process"
took=$(( $(date +%s) - start ))
expect "verdict with a detached process" "$line" "*test='OK'*"
[ "$took" -lt 40 ] || fail "verdict waited ${took}s for a detached process"

# 11: unsafe labels.
for bad in ../escape . .. 'a/b'; do
  if "$harness" "$bad" 1 true > /dev/null 2>&1; then fail "label '$bad' was accepted"; fi
done
[ ! -e "$work/escape" ] || fail "a run was created outside TRIALS_DIR"
echo "unsafe labels refused"; passed=$((passed + 1))

# 12: unusable trials folder. Root ignores folder permissions, so the case cannot be set up there.
if [ "$(id -u)" -eq 0 ]; then
  echo "unusable TRIALS_DIR: skipped as root"; passed=$((passed + 1))
else
  mkdir "$work/locked"; chmod 555 "$work/locked"
  set +e; err=$(TRIALS_DIR="$work/locked/sub" "$harness" locked 1 true 2>&1 > /dev/null); rc=$?; set -e
  chmod 755 "$work/locked"
  [ "$rc" -eq 1 ] || fail "unusable TRIALS_DIR gave exit $rc, not 1"
  case "$err" in *"cannot create"*) ;; *) fail "unusable TRIALS_DIR did not stop at the root check: $err" ;; esac
  echo "unusable TRIALS_DIR stops the harness"; passed=$((passed + 1))
fi

# 13: bad counts.
for args in "0|240" "abc|240" "1|0" "1|abc"; do
  runs="${args%|*}"; limit="${args#*|}"
  set +e; LIMIT="$limit" "$harness" counts "$runs" true > /dev/null 2>&1; rc=$?; set -e
  [ "$rc" -eq 2 ] || fail "runs=$runs LIMIT=$limit gave exit $rc, not 2"
done
echo "bad runs and LIMIT refused"; passed=$((passed + 1))

# 14: exit 130 from the agent, and KeyboardInterrupt from the code under test, are just results.
kbi='printf "raise KeyboardInterrupt\n" > calc.py; exit 130'
out=$("$harness" kbi 2 sh -c "$kbi" _ 2>&1) || true
echo "$out"
case "$out" in *"kbi run 2: exit=130"*) ;; *) fail "exit 130 from the agent or test stopped the trial set" ;; esac
passed=$((passed + 1))

# 15: a real Ctrl-C, sent to the harness's whole process group as a terminal does.
marker="$work/interrupted-child-survived"
# A background job starts with SIGINT ignored, unlike a terminal's foreground job; restore it.
perl -e '$SIG{INT} = "DEFAULT"; setpgrp(0, 0); exec @ARGV' "$harness" intr 3 sh -c "(sleep 6; touch '$marker') & sleep 60" _ \
  > "$work/intr.out" 2> "$work/intr.err" &
pid=$!
sleep 2
kill -INT -"$pid"
set +e; wait "$pid"; rc=$?; set -e
sleep 6
[ "$rc" -eq 130 ] || fail "Ctrl-C gave exit $rc, not 130"
[ ! -d "$TRIALS_DIR/intr/run2" ] || fail "Ctrl-C did not stop the trial set"
[ ! -e "$marker" ] || fail "Ctrl-C left the agent's child running"
echo "Ctrl-C stops the trial set and kills the agent"; passed=$((passed + 1))

# 15b: TERM gives 143.
perl -e 'setpgrp(0, 0); exec @ARGV' "$harness" term 2 sh -c "sleep 60" _ > /dev/null 2>&1 &
pid=$!
sleep 2
kill -TERM -"$pid"
set +e; wait "$pid"; rc=$?; set -e
[ "$rc" -eq 143 ] || fail "TERM gave exit $rc, not 143"
[ ! -d "$TRIALS_DIR/term/run2" ] || fail "TERM did not stop the trial set"
echo "TERM stops the trial set with 143"; passed=$((passed + 1))

# 16: model-style labels.
expect "label with a colon" "$("$harness" "gpt-oss:20b" 1 true)" "gpt-oss:20b run 1:*"

echo "selftest: $passed of 17 passed"
