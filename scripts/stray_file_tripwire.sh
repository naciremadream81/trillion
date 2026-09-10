#!/usr/bin/env bash
#
# Stray-file tripwire — a diagnostic wrapper, NOT a test.
#
# Watches the working tree while a command runs and reports any file that
# appeared and that git would show as new and untracked. Written after a
# literal ":memory:.ses" turned up in the repo root: ONNX Runtime's telemetry
# failed to persist its device ID, fell back to the string ":memory:" as an
# identifier, and its session store used that as a path prefix. Nothing on
# disk contained the suffix, so grep could not find the writer — only a
# filesystem watch could.
#
# Usage:
#   scripts/stray_file_tripwire.sh                      # watch the test suite
#   scripts/stray_file_tripwire.sh .venv/bin/python serve.py
#
# Exits non-zero if anything was caught, so it can gate CI.

set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

if ! command -v inotifywait >/dev/null 2>&1; then
    echo "stray-file tripwire: inotifywait not found (apt install inotify-tools)" >&2
    exit 127
fi

# Directories never worth watching: either git's own store, or churn that is
# expected to be noisy. Excluded at watch-setup time via inotifywait's "@"
# prefix so we don't burn a watch descriptor per file under .venv.
excluded_dirs=(
    .git .venv venv desktop/.venv node_modules vendor
    generated-projects __pycache__ .pytest_cache .ruff_cache
)

watch_args=(--monitor --recursive --event create --event moved_to
            --format '%w%f')
for d in "${excluded_dirs[@]}"; do
    [ -e "$d" ] && watch_args+=("@$repo_root/$d")
done

events="$(mktemp)"
watcher_log="$(mktemp)"
cleanup() {
    [ -n "${watcher_pid:-}" ] && kill "$watcher_pid" 2>/dev/null
    rm -f "$events" "$watcher_log"
}
trap cleanup EXIT

inotifywait "${watch_args[@]}" --outfile "$events" "$repo_root" 2>"$watcher_log" &
watcher_pid=$!

# inotifywait announces readiness on stderr. Starting the command before the
# watches exist would miss exactly the early-import writes we care about --
# the original artifact appeared 0.8s into a test run.
for _ in $(seq 1 100); do
    grep -q 'Watches established' "$watcher_log" 2>/dev/null && break
    kill -0 "$watcher_pid" 2>/dev/null || { echo "tripwire: watcher died" >&2; cat "$watcher_log" >&2; exit 1; }
    sleep 0.1
done
if ! grep -q 'Watches established' "$watcher_log" 2>/dev/null; then
    echo "stray-file tripwire: watches never established" >&2
    cat "$watcher_log" >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    cmd=("$@")
else
    python="$repo_root/.venv/bin/python"
    [ -x "$python" ] || python="python3"
    cmd=("$python" -m unittest discover -s tests -q)
fi

echo "stray-file tripwire: watching $repo_root while running: ${cmd[*]}"
"${cmd[@]}"
cmd_status=$?

# inotify delivery is asynchronous; give the queue a moment to drain before
# reading, or a write from the command's final milliseconds is missed.
sleep 1
kill "$watcher_pid" 2>/dev/null
wait "$watcher_pid" 2>/dev/null
watcher_pid=""

# A path is only interesting if git would report it as new and untracked --
# that is precisely what ":memory:.ses" was. Ignored churn (*.db, caches) and
# files already tracked are not news.
stray=()
while IFS= read -r path; do
    [ -n "$path" ] || continue
    [ -e "$path" ] || continue
    rel="${path#"$repo_root"/}"
    git check-ignore -q -- "$rel" 2>/dev/null && continue
    git ls-files --error-unmatch -- "$rel" >/dev/null 2>&1 && continue
    stray+=("$rel")
done < <(sort -u "$events")

echo
if [ "${#stray[@]}" -eq 0 ]; then
    echo "stray-file tripwire: clean — no new untracked files appeared."
    exit "$cmd_status"
fi

echo "stray-file tripwire: ${#stray[@]} stray file(s) appeared:"
for rel in "${stray[@]}"; do
    printf '  %s (%s bytes)\n' "$rel" "$(stat -c%s -- "$rel" 2>/dev/null || echo '?')"
done
echo
echo "To find the writer, re-run the command under a syscall trace, e.g.:"
echo "  strace -f -e trace=openat,creat -o /tmp/trace.log ${cmd[*]}"
echo "  grep -n 'O_CREAT' /tmp/trace.log | grep '${stray[0]##*/}'"
exit 1
