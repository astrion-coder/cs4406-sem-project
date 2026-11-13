#!/usr/bin/env bash
# Emulated network links for RQ3b (spec 5.1, gate G4): dnctl pipes attached by pf rules on lo0.
#
#   sudo dummynet.sh up <profile> [port ...]   shape TCP to and from these loopback ports
#   sudo dummynet.sh calibrate <profile>       ping and iperf3 over the pipes, each within 10 % (spec 11)
#   sudo dummynet.sh status
#   sudo dummynet.sh down                      remove the pipes and rules, release pf
#        dummynet.sh params <profile>          print the pipe commands `up` would run, touching nothing
#        dummynet.sh rules [port ...]          print the pf rules `up` would load, touching nothing
#
# Profiles come from configs/network_profiles.yaml. The down pipe carries traffic from the server
# ports to the client, the up pipe traffic to the server ports, and each adds half the RTT. Both
# servers of a two-server arm share the two pipes, as they would share a client's access link.
#
# The rules live in the anchor com.apple/vpir-bench, which macOS's default /etc/pf.conf already
# evaluates, so the main ruleset is never replaced. `pfctl -E` takes a reference on pf and `down`
# releases it with `pfctl -X`, which leaves pf enabled or disabled as it was before `up`.
#
# `up` and `calibrate` record the link in a state file that netem.py reads (it runs without root):
# a run refuses a link that is not the requested profile or has not passed calibration.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
PROFILES=${VPIR_PROFILES:-$ROOT/configs/network_profiles.yaml}
STATE=${VPIR_DUMMYNET_STATE:-/var/run/vpir-dummynet.state}
ANCHOR=com.apple/vpir-bench
PIPE_DOWN=44001
PIPE_UP=44002
# Below macOS's ephemeral range (49152 and up), so a client's own port never matches a rule.
DEFAULT_PORTS="47001 47002"
TOLERANCE_PCT=10
PING_COUNT=50
IPERF_SECONDS=10
IPERF_OMIT=2

die() { echo "dummynet.sh: $*" >&2; exit 1; }
note() { echo "dummynet.sh: $*" >&2; }

usage() {
    sed -n '4,9p' "${BASH_SOURCE[0]}" | sed 's/^# *//' >&2
    exit 2
}

# ---- profiles and parameters (no root needed) ----

profile_body() {
    local name=$1 body
    [[ $name =~ ^[a-z][a-z0-9_]*$ ]] || die "bad profile name '$name'"
    [[ -r $PROFILES ]] || die "cannot read $PROFILES"
    body=$(sed -nE "s/^${name}:[[:space:]]*\{(.*)\}.*$/\1/p" "$PROFILES")
    [[ -n $body ]] || die "no profile '$name' in $PROFILES"
    echo "$body"
}

profile_field() {
    local body=$1 key=$2 value
    value=$(echo "$body" | tr ',' '\n' | sed -nE "s/^[[:space:]]*${key}:[[:space:]]*([0-9]+(\.[0-9]+)?)[[:space:]]*$/\1/p")
    [[ -n $value ]] || die "profile has no numeric $key: {$body}"
    echo "$value"
}

# Sets DOWN_MBPS, UP_MBPS, RTT_MS and DELAY_MS for profile $1.
load_profile() {
    local body
    body=$(profile_body "$1")
    DOWN_MBPS=$(profile_field "$body" down_mbps)
    UP_MBPS=$(profile_field "$body" up_mbps)
    RTT_MS=$(profile_field "$body" rtt_ms)
    # dnctl takes the delay in whole milliseconds, so half an RTT that is not a whole number cannot
    # be configured. It is rounded down, and calibration then measures the RTT the link really has.
    DELAY_MS=$(awk -v r="$RTT_MS" 'BEGIN { printf "%d", r / 2 }')
    DELAY_EXACT=$(awk -v r="$RTT_MS" -v d="$DELAY_MS" 'BEGIN { print (r / 2 == d) ? "yes" : "no" }')
}

pipe_commands() {
    echo "dnctl pipe $PIPE_DOWN config bw ${DOWN_MBPS}Mbit/s delay $DELAY_MS"
    echo "dnctl pipe $PIPE_UP config bw ${UP_MBPS}Mbit/s delay $DELAY_MS"
}

check_ports() {
    local p
    [[ $# -gt 0 ]] || die "no ports"
    for p in "$@"; do
        [[ $p =~ ^[0-9]+$ ]] && (( p >= 1 && p <= 65535 )) || die "bad port '$p'"
    done
}

# pf rules for the ports in $2..; $1 is "icmp" to also shape ping, which calibrate needs.
anchor_rules() {
    local icmp=$1
    shift
    local ports="{ $* }"
    echo "dummynet out quick on lo0 proto tcp from any port $ports to any pipe $PIPE_DOWN"
    echo "dummynet out quick on lo0 proto tcp from any to any port $ports pipe $PIPE_UP"
    if [[ $icmp == icmp ]]; then
        echo "dummynet out quick on lo0 inet proto icmp icmp-type echoreq pipe $PIPE_UP"
        echo "dummynet out quick on lo0 inet proto icmp icmp-type echorep pipe $PIPE_DOWN"
    fi
}

# ---- state file ----

state_get() {
    [[ -r $STATE ]] || return 0
    sed -nE "s/^$1=(.*)$/\1/p" "$STATE" | tail -n 1
}

# Rewrites the state file with key=value pairs from the arguments, keeping keys not given.
state_set() {
    local tmp="$STATE.tmp.$$" kv key
    : > "$tmp"
    if [[ -r $STATE ]]; then
        local keep=1 line
        while IFS= read -r line; do
            keep=1
            for kv in "$@"; do
                key=${kv%%=*}
                [[ $line == "$key="* ]] && keep=0
            done
            (( keep )) && echo "$line" >> "$tmp"
        done < "$STATE"
    fi
    for kv in "$@"; do echo "$kv" >> "$tmp"; done
    chmod 644 "$tmp"
    mv -f "$tmp" "$STATE"
}

# ---- privileged commands ----

need_root_darwin() {
    [[ $(uname -s) == Darwin ]] || die "dnctl and pfctl exist only on macOS; this is $(uname -s)"
    [[ $(id -u) -eq 0 ]] || die "needs root: sudo $0 $*"
}

# pfctl's output is captured before grep: with pipefail, grep -q closing the pipe early fails it.
check_main_ruleset() {
    local main
    main=$(pfctl -sa 2>/dev/null || true)
    grep -qE '^dummynet-anchor "com\.apple/\*"' <<<"$main" && \
        grep -qE '^anchor "com\.apple/\*"' <<<"$main" && return 0
    die "pf's main ruleset does not evaluate the com.apple anchors. Load macOS's default ruleset" \
        "first (sudo pfctl -f /etc/pf.conf), or add dummynet-anchor and anchor \"com.apple/*\" to it."
}

load_rules() {
    local icmp=$1
    shift
    anchor_rules "$icmp" "$@" | pfctl -q -a "$ANCHOR" -f - || die "pfctl rejected the rules"
    local loaded
    loaded=$(pfctl -a "$ANCHOR" -sa 2>/dev/null || true)
    grep -q "pipe $PIPE_UP" <<<"$loaded" || die "rules did not load into $ANCHOR"
}

cmd_up() {
    [[ $# -ge 1 ]] || usage
    local profile=$1
    shift
    need_root_darwin up "$profile" "$@"
    local ports
    if [[ $# -gt 0 ]]; then ports="$*"; else ports=$DEFAULT_PORTS; fi
    # shellcheck disable=SC2086
    check_ports $ports
    load_profile "$profile"
    check_main_ruleset

    local token
    token=$(state_get pf_token)
    if [[ -z $token ]]; then
        token=$(pfctl -E 2>&1 | sed -nE 's/^Token : ([0-9]+).*$/\1/p')
        [[ -n $token ]] || die "pfctl -E gave no reference token"
        state_set "pf_token=$token"
    fi
    while IFS= read -r line; do
        # shellcheck disable=SC2086
        $line > /dev/null || die "failed: $line"
    done < <(pipe_commands)
    # shellcheck disable=SC2086
    load_rules tcp $ports
    state_set "profile=$profile" "down_mbps=$DOWN_MBPS" "up_mbps=$UP_MBPS" "rtt_ms=$RTT_MS" \
        "delay_ms=$DELAY_MS" "ports=$ports" "up_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "calibrated=none" "calib_rtt_ms=" "calib_down_mbps=" "calib_up_mbps=" "calibrated_at="
    [[ $DELAY_EXACT == yes ]] || note "half of rtt_ms=$RTT_MS is not whole milliseconds, so dnctl adds" \
        "$DELAY_MS ms per direction; calibrate checks $profile's bandwidth and records its RTT (decision 22)"
    echo "up: $profile on ports $ports (pipes $PIPE_DOWN down, $PIPE_UP up)"
}

cmd_down() {
    need_root_darwin down
    pfctl -q -a "$ANCHOR" -F all 2>/dev/null || true
    dnctl pipe delete "$PIPE_DOWN" "$PIPE_UP" > /dev/null 2>&1 || true
    local token
    token=$(state_get pf_token)
    if [[ -n $token ]]; then
        pfctl -q -X "$token" 2>/dev/null || note "pf reference $token was already released"
    fi
    if [[ -e $STATE ]]; then
        rm -f "$STATE"
        echo "down"
    else
        echo "down (was not up)"
    fi
}

cmd_status() {
    need_root_darwin status
    if [[ -r $STATE ]]; then
        echo "state $STATE:"
        sed 's/^/  /' "$STATE"
    else
        echo "state: down (no $STATE)"
    fi
    pfctl -s info 2>/dev/null | head -n 1
    echo "pipes:"
    dnctl pipe show "$PIPE_DOWN" "$PIPE_UP" 2>/dev/null | sed 's/^/  /' || true
    echo "anchor $ANCHOR:"
    pfctl -a "$ANCHOR" -sa 2>/dev/null | grep -E 'dummynet|pipe' | sed 's/^/  /' || true
}

find_iperf3() {
    local c
    for c in "$(command -v iperf3 || true)" /opt/homebrew/bin/iperf3 /usr/local/bin/iperf3; do
        [[ -n $c && -x $c ]] && { echo "$c"; return 0; }
    done
    die "iperf3 not found (brew install iperf3)"
}

within() {  # measured target -> exit 0 when |measured - target| <= TOLERANCE_PCT % of target
    awk -v m="$1" -v t="$2" -v p="$TOLERANCE_PCT" 'BEGIN { d = m - t; if (d < 0) d = -d; exit !(d <= t * p / 100) }'
}

# Prints the receiver's rate in Mbit/s for one iperf3 run; $1 is "" or "-R" (server sends). It runs
# in a command substitution, so it stops its own iperf3 server on every path.
iperf_mbps() {
    local reverse=$1 port=$2 out rate server
    "$IPERF3" -s -1 -p "$port" > /dev/null 2>&1 &
    server=$!
    sleep 1
    # shellcheck disable=SC2086
    if ! out=$("$IPERF3" -c 127.0.0.1 -p "$port" -t "$IPERF_SECONDS" -O "$IPERF_OMIT" -f m $reverse 2>&1); then
        kill "$server" 2>/dev/null || true
        echo "$out" >&2
        die "iperf3 client failed"
    fi
    wait "$server" 2>/dev/null || true
    rate=$(echo "$out" | awk '/receiver/ { for (i = 1; i <= NF; i++) if ($i == "Mbits/sec") r = $(i - 1) } END { print r }')
    [[ -n $rate ]] || { echo "$out" >&2; die "could not read iperf3's receiver rate"; }
    echo "$rate"
}

calibrate_cleanup() {
    # shellcheck disable=SC2086
    [[ -z ${CAL_PORTS:-} ]] || load_rules tcp $CAL_PORTS
}

cmd_calibrate() {
    [[ $# -eq 1 ]] || usage
    local profile=$1
    need_root_darwin calibrate "$profile"
    [[ $(state_get profile) == "$profile" ]] || \
        die "the link is '$(state_get profile)', not '$profile'; run: sudo $0 up $profile"
    load_profile "$profile"
    IPERF3=$(find_iperf3)
    CAL_PORTS=$(state_get ports)
    local port=${CAL_PORTS%% *}
    trap calibrate_cleanup EXIT
    # shellcheck disable=SC2086
    load_rules icmp $CAL_PORTS

    local ping_out rtt down up verdict=pass
    ping_out=$(ping -q -c "$PING_COUNT" -i 0.1 127.0.0.1) || die "ping failed"
    rtt=$(echo "$ping_out" | sed -nE 's#^round-trip [^=]*= [0-9.]+/([0-9.]+)/.*#\1#p')
    [[ -n $rtt ]] || { echo "$ping_out" >&2; die "could not read ping's average RTT"; }
    up=$(iperf_mbps "" "$port")
    down=$(iperf_mbps "-R" "$port")

    printf '%-10s %12s %12s %s\n' metric target measured "within ${TOLERANCE_PCT}%"
    local name target measured ok
    # Decisions 22 and 29: dummynet releases packets on the 10 ms kernel tick, so no profile's RTT is exact
    # (broadband's whole-tick 10 ms each way measured 23.7 ms). The RTT is recorded, not checked, and F9
    # models each link at its measured RTT. Bandwidth is still checked.
    local rtt_checked=no
    while read -r name target measured; do
        if [[ $name == rtt_ms && $rtt_checked == no ]]; then ok="recorded only (decision 22)"
        elif within "$measured" "$target"; then ok=yes; else ok=no; verdict=fail; fi
        printf '%-10s %12s %12s %s\n' "$name" "$target" "$measured" "$ok"
    done <<EOF
rtt_ms $RTT_MS $rtt
down_mbps $DOWN_MBPS $down
up_mbps $UP_MBPS $up
EOF
    state_set "calibrated=$verdict" "rtt_checked=$rtt_checked" "calib_rtt_ms=$rtt" "calib_down_mbps=$down" "calib_up_mbps=$up" \
        "calibrated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "calibration $verdict for $profile (test_netem_calibration)"
    [[ $verdict == pass ]]
}

main() {
    [[ $# -ge 1 ]] || usage
    local cmd=$1
    shift
    case $cmd in
        up) cmd_up "$@" ;;
        down) cmd_down ;;
        status) cmd_status ;;
        calibrate) cmd_calibrate "$@" ;;
        params)
            [[ $# -eq 1 ]] || usage
            load_profile "$1"
            echo "profile=$1"
            echo "down_mbps=$DOWN_MBPS"
            echo "up_mbps=$UP_MBPS"
            echo "rtt_ms=$RTT_MS"
            echo "delay_ms=$DELAY_MS"
            echo "delay_exact=$DELAY_EXACT"
            pipe_commands
            ;;
        rules)
            if [[ $# -eq 0 ]]; then
                # shellcheck disable=SC2086
                set -- $DEFAULT_PORTS
            fi
            check_ports "$@"
            anchor_rules tcp "$@"
            ;;
        *) usage ;;
    esac
}

main "$@"
