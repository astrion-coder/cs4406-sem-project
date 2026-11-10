#!/usr/bin/env bash
# One serve-mode session for an arm, end to end, with nothing left running afterwards:
# start its server(s) from build/adapters on 127.0.0.1:0, wait for the ready line, run
# --mode client to write records and a corpus, then stop the servers with SIGTERM.
#
#   bash .claude/skills/verify-vpir-bench/scripts/serve_session.sh ARM [QUERIES]
#
# Writes $VERIFY_RUN/serve-<ARM>/: server<k>.jsonl (summary record), server<k>.stderr,
# client.jsonl, client.stderr and corpus.bin. Exits 0 only when the client and every
# server exit 0. Kills only the servers it started.
set -uo pipefail
# macOS's bash 3.2 treats an empty array as unbound under -u, hence the ${a[@]+"${a[@]}"} forms.

: "${VERIFY_RUN:?set VERIFY_RUN first}"
arm=${1:?usage: serve_session.sh ARM [QUERIES]}
queries=${2:-8}
repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$repo"

case $arm in
	S0-go) bin=simplepir-adapter servers=1 db=1048576 rb=32 ;;
	S0-cpp | S1 | S2) bin=vsp-adapter servers=1 db=1048576 rb=32 ;;
	S3) bin=apir-adapter servers=1 db=1048576 rb=0 ;;
	S4) bin=apir-adapter servers=1 db=65536 rb=0 ;;
	M0 | M1) bin=apir-adapter servers=2 db=1048576 rb=32 ;;
	*) echo "unknown arm $arm" >&2; exit 64 ;;
esac
out="$VERIFY_RUN/serve-$arm"
mkdir -p "$out"
common=(--arm "$arm" --db-bytes "$db" --record-bytes "$rb" --seed 1234)

pids=()
trap 'for p in ${pids[@]+"${pids[@]}"}; do kill "$p" 2>/dev/null; done' EXIT
for ((s = 0; s < servers; s++)); do
	id=()
	[ "$servers" -eq 2 ] && id=(--server-id "$s")
	"build/adapters/$bin" --mode serve "${common[@]}" --workers 2 --listen 127.0.0.1:0 ${id[@]+"${id[@]}"} \
		--out "$out/server$s.jsonl" 2> "$out/server$s.stderr" &
	pids+=($!)
done

addrs=()
for ((s = 0; s < servers; s++)); do
	line=""
	for _ in $(seq 1200); do
		line=$(grep -m1 '^vpir-serve ready ' "$out/server$s.stderr" || true)
		[ -n "$line" ] && break
		kill -0 "${pids[$s]}" 2>/dev/null || { echo "server $s exited before its ready line"; tail -5 "$out/server$s.stderr"; exit 1; }
		sleep 0.5
	done
	[ -n "$line" ] || { echo "server $s printed no ready line in 600 s"; exit 1; }
	echo "server $s: $line"
	addrs+=("${line#vpir-serve ready }")
done

connect=$(IFS=,; echo "${addrs[*]}")
"build/adapters/$bin" --mode client "${common[@]}" --connect "$connect" --session-queries "$queries" \
	--out "$out/client.jsonl" --corpus-out "$out/corpus.bin" 2> "$out/client.stderr"
client_rc=$?
echo "client: exit $client_rc, $(wc -l < "$out/client.jsonl" | tr -d ' ') records, corpus $(wc -c < "$out/corpus.bin" | tr -d ' ') bytes"

rc=$client_rc
for ((s = 0; s < servers; s++)); do kill -TERM "${pids[$s]}"; done
for ((s = 0; s < servers; s++)); do
	wait "${pids[$s]}"
	code=$?
	echo "server $s: exit $code, $(wc -l < "$out/server$s.jsonl" | tr -d ' ') summary record(s)"
	[ "$code" -eq 0 ] || rc=1
done
pids=()
exit "$rc"
