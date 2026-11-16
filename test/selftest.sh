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
  "$("$harness" noop 1 true)" "*test='AssertionError: add is wrong'*"

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

echo "selftest: $passed of 8 passed"
