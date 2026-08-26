#!/usr/bin/env bash

# Restart the one MobileWorld emulator owned by this container. Every
# operation in this file is container-local; it never targets a host process or
# another container.
set -u
set -o pipefail

STATE_DIR="${MOBILEWORLD_EMULATOR_STATE_DIR:-/run/mobileworld-emulator}"
PID_FILE="$STATE_DIR/emulator.pid"
STATE_FILE="$STATE_DIR/emulator.state.json"
PROXY_PID_FILE="$STATE_DIR/proxy-chain.pid"
PROC_ROOT="${MOBILEWORLD_PROC_ROOT:-/proc}"
EMULATOR_LOG_PATH="${MOBILEWORLD_EMULATOR_LOG_PATH:-/var/log/emulator.log}"
PROXY_LOG_PATH="${MOBILEWORLD_PROXY_LOG_PATH:-/var/log/proxy_chain.log}"
PROXY_CHAIN_SCRIPT="${MOBILEWORLD_PROXY_CHAIN_SCRIPT:-/app/docker/proxy_chain.py}"

AVD_NAME="${AVD_NAME:-}"
GENERATION_ID="${MOBILEWORLD_EMULATOR_GENERATION_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
EMULATOR_TIMEOUT="${EMULATOR_TIMEOUT:-600}"
SHUTDOWN_TIMEOUT="${EMULATOR_SHUTDOWN_TIMEOUT:-15}"
TERM_TIMEOUT="${EMULATOR_TERM_TIMEOUT:-5}"
KILL_TIMEOUT="${EMULATOR_KILL_TIMEOUT:-5}"
ROOT_RECONNECT_TIMEOUT="${EMULATOR_ROOT_RECONNECT_TIMEOUT:-30}"
POLL_INTERVAL="${EMULATOR_POLL_INTERVAL:-2}"
PROXY_STARTUP_WAIT="${MOBILEWORLD_PROXY_STARTUP_WAIT:-1}"

is_nonnegative_number() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    printf 'ERROR: emulator generation log: %s\n' "$EMULATOR_LOG_PATH" >&2
    exit 1
}

case "$AVD_NAME" in
    ''|*[!A-Za-z0-9_.-]*) fail "AVD_NAME is missing or contains unsupported characters" ;;
esac
case "$GENERATION_ID" in
    ''|*[!A-Za-z0-9_.-]*) fail "emulator generation ID contains unsupported characters" ;;
esac
for numeric_value in \
    "$EMULATOR_TIMEOUT" \
    "$SHUTDOWN_TIMEOUT" \
    "$TERM_TIMEOUT" \
    "$KILL_TIMEOUT" \
    "$ROOT_RECONNECT_TIMEOUT" \
    "$POLL_INTERVAL" \
    "$PROXY_STARTUP_WAIT"; do
    is_nonnegative_number "$numeric_value" || fail "restart timeout values must be integers"
done

umask 077
mkdir -p "$STATE_DIR" || fail "cannot create emulator state directory"
mkdir -p "$(dirname "$EMULATOR_LOG_PATH")" || fail "cannot create emulator log directory"
touch "$EMULATOR_LOG_PATH" || fail "cannot open emulator generation log"

process_tokens() {
    local pid="$1"
    local cmdline_path="$PROC_ROOT/$pid/cmdline"
    [ -r "$cmdline_path" ] || return 1
    tr '\0' '\n' < "$cmdline_path"
}

is_emulator_process() {
    local pid="$1"
    local expected_avd="$2"
    local token
    local basename_token
    local executable_seen=0
    local expected_avd_seen=0
    local expect_avd_value=0
    local token_index=0
    local shell_wrapper=0

    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$pid" -gt 1 ] || return 1

    while IFS= read -r token; do
        [ -n "$token" ] || continue
        if [ "$token_index" -eq 0 ]; then
            basename_token="${token##*/}"
            case "$basename_token" in
                emulator|emulator64-*|qemu-system-*) executable_seen=1 ;;
                bash|sh) shell_wrapper=1 ;;
            esac
        elif [ "$token_index" -eq 1 ] && [ "$shell_wrapper" -eq 1 ]; then
            basename_token="${token##*/}"
            case "$basename_token" in
                emulator|emulator64-*|qemu-system-*) executable_seen=1 ;;
            esac
        fi
        token_index=$((token_index + 1))
        if [ "$expect_avd_value" -eq 1 ]; then
            [ "$token" = "$expected_avd" ] && expected_avd_seen=1
            expect_avd_value=0
        elif [ "$token" = "-avd" ]; then
            expect_avd_value=1
        elif [ "$token" = "@$expected_avd" ] || \
            [[ "$token" == *"/$expected_avd.avd" ]] || \
            [[ "$token" == *"/$expected_avd.avd/"* ]]; then
            expected_avd_seen=1
        fi
    done < <(process_tokens "$pid")

    [ "$executable_seen" -eq 1 ] && [ "$expected_avd_seen" -eq 1 ]
}

is_proxy_process() {
    local pid="$1"
    local token

    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$pid" -gt 1 ] || return 1

    while IFS= read -r token; do
        if [ "$token" = "$PROXY_CHAIN_SCRIPT" ]; then
            return 0
        fi
    done < <(process_tokens "$pid")
    return 1
}

append_unique_pid() {
    local candidate="$1"
    local expected_avd="$2"
    local existing
    for existing in "${EMULATOR_PIDS[@]:-}"; do
        [ "$existing" = "$candidate" ] && return 0
    done
    EMULATOR_PIDS+=("$candidate")
    EMULATOR_PID_AVDS["$candidate"]="$expected_avd"
}

discover_emulator_pids() {
    local tracked_pid=''
    local tracked_avd=''
    local tracked_generation=''
    local proc_dir
    local pid

    EMULATOR_PIDS=()
    declare -gA EMULATOR_PID_AVDS=()
    if [ -r "$PID_FILE" ]; then
        IFS=$'\t' read -r tracked_pid tracked_avd tracked_generation < "$PID_FILE" || true
        if [[ "$tracked_avd" =~ ^[A-Za-z0-9_.-]+$ ]] && \
            is_emulator_process "$tracked_pid" "$tracked_avd"; then
            append_unique_pid "$tracked_pid" "$tracked_avd"
        fi
    fi

    # The PID file covers hardened generations. The exact /proc fallback is
    # needed once when replacing an older image generation whose ADB transport
    # may already be offline and therefore absent from `adb devices`.
    for proc_dir in "$PROC_ROOT"/[0-9]*; do
        [ -d "$proc_dir" ] || continue
        pid="${proc_dir##*/}"
        if is_emulator_process "$pid" "$AVD_NAME"; then
            append_unique_pid "$pid" "$AVD_NAME"
        fi
    done
}

emulator_pid_is_alive() {
    local pid="$1"
    local expected_avd="${EMULATOR_PID_AVDS[$pid]:-}"
    [ -n "$expected_avd" ] && \
        is_emulator_process "$pid" "$expected_avd" && \
        kill -0 "$pid" 2>/dev/null
}

wait_for_emulator_pids_to_exit() {
    local timeout="$1"
    local started
    local now
    local pid
    local any_alive

    started=$(date +%s)
    while true; do
        any_alive=0
        for pid in "${EMULATOR_PIDS[@]:-}"; do
            if emulator_pid_is_alive "$pid"; then
                any_alive=1
                break
            fi
        done
        [ "$any_alive" -eq 0 ] && return 0

        now=$(date +%s)
        [ $((now - started)) -ge "$timeout" ] && return 1
        sleep 1
    done
}

signal_live_emulator_pids() {
    local signal="$1"
    local pid
    for pid in "${EMULATOR_PIDS[@]:-}"; do
        if emulator_pid_is_alive "$pid"; then
            printf 'INFO: sending %s to prior emulator pid %s\n' "$signal" "$pid"
            kill "-$signal" "$pid" 2>/dev/null || true
        fi
    done
}

list_emulator_serials() {
    adb devices 2>&1 | awk 'NR > 1 && $1 ~ /^emulator-[0-9]+$/ {print $1}'
}

shutdown_previous_emulator() {
    local serial

    discover_emulator_pids
    if [ "${#EMULATOR_PIDS[@]}" -gt 0 ]; then
        printf 'INFO: stopping prior emulator process(es): %s\n' "${EMULATOR_PIDS[*]}"
    fi

    while IFS= read -r serial; do
        [ -n "$serial" ] || continue
        if ! adb -s "$serial" emu kill; then
            printf 'WARNING: graceful ADB shutdown failed for %s; bounded PID shutdown will continue\n' \
                "$serial" >&2
        fi
    done < <(list_emulator_serials)

    if ! wait_for_emulator_pids_to_exit "$SHUTDOWN_TIMEOUT"; then
        signal_live_emulator_pids TERM
        wait_for_emulator_pids_to_exit "$TERM_TIMEOUT" || true
    fi
    if ! wait_for_emulator_pids_to_exit 0; then
        signal_live_emulator_pids KILL
        wait_for_emulator_pids_to_exit "$KILL_TIMEOUT" || true
    fi
    if ! wait_for_emulator_pids_to_exit 0; then
        fail "prior emulator did not exit after bounded TERM/KILL escalation"
    fi

    rm -f "$PID_FILE" "$STATE_FILE"
}

online_emulator_serials() {
    adb devices 2>&1 | awk 'NR > 1 && $1 ~ /^emulator-[0-9]+$/ && $2 == "device" {print $1}'
}

wait_for_new_emulator() {
    local pid="$1"
    local timeout="$2"
    local started
    local now
    local serial
    local boot_completed
    local online=()

    started=$(date +%s)
    while true; do
        kill -0 "$pid" 2>/dev/null || return 1
        mapfile -t online < <(online_emulator_serials)
        if [ "${#online[@]}" -eq 1 ]; then
            serial="${online[0]}"
            boot_completed=$(adb -s "$serial" shell getprop sys.boot_completed 2>&1 || true)
            if [ "$(printf '%s' "$boot_completed" | tr -d '\r\n[:space:]')" = "1" ]; then
                DEVICE_ID="$serial"
                return 0
            fi
        elif [ "${#online[@]}" -gt 1 ]; then
            printf 'ERROR: more than one online emulator appeared during restart\n' >&2
            return 1
        fi

        now=$(date +%s)
        [ $((now - started)) -ge "$timeout" ] && return 1
        sleep "$POLL_INTERVAL"
    done
}

wait_for_device_reconnect() {
    local serial="$1"
    local timeout="$2"
    local started
    local now
    local state
    local boot_completed

    started=$(date +%s)
    while true; do
        state=$(adb -s "$serial" get-state 2>&1 || true)
        boot_completed=$(adb -s "$serial" shell getprop sys.boot_completed 2>&1 || true)
        if [ "$(printf '%s' "$state" | tr -d '\r\n[:space:]')" = "device" ] && \
            [ "$(printf '%s' "$boot_completed" | tr -d '\r\n[:space:]')" = "1" ]; then
            return 0
        fi

        now=$(date +%s)
        [ $((now - started)) -ge "$timeout" ] && return 1
        sleep "$POLL_INTERVAL"
    done
}

disable_animation() {
    local serial="$1"
    local setting
    for setting in window_animation_scale transition_animation_scale animator_duration_scale; do
        if ! adb -s "$serial" shell settings put global "$setting" 0.0; then
            printf 'WARNING: failed to disable Android animation setting %s\n' "$setting" >&2
        fi
    done
}

find_existing_proxy_pid() {
    local recorded_pid=''
    local proc_dir
    local pid

    if [ -r "$PROXY_PID_FILE" ]; then
        IFS= read -r recorded_pid < "$PROXY_PID_FILE" || true
        if is_proxy_process "$recorded_pid" && kill -0 "$recorded_pid" 2>/dev/null; then
            printf '%s\n' "$recorded_pid"
            return 0
        fi
    fi

    for proc_dir in "$PROC_ROOT"/[0-9]*; do
        [ -d "$proc_dir" ] || continue
        pid="${proc_dir##*/}"
        if is_proxy_process "$pid" && kill -0 "$pid" 2>/dev/null; then
            printf '%s\n' "$pid"
            return 0
        fi
    done
    return 1
}

ensure_proxy_chain() {
    local upstream="$1"
    local local_port="$2"
    local proxy_pid=''
    local proxy_pid_tmp

    proxy_pid=$(find_existing_proxy_pid || true)
    if [ -n "$proxy_pid" ]; then
        printf 'INFO: reusing existing in-container proxy chain pid %s on port %s\n' \
            "$proxy_pid" "$local_port"
    else
        mkdir -p "$(dirname "$PROXY_LOG_PATH")" || fail "cannot create proxy log directory"
        touch "$PROXY_LOG_PATH" || fail "cannot open proxy log"
        UPSTREAM_PROXY="$upstream" LOCAL_PORT="$local_port" \
            nohup /usr/bin/python3 "$PROXY_CHAIN_SCRIPT" >> "$PROXY_LOG_PATH" 2>&1 &
        proxy_pid=$!
        sleep "$PROXY_STARTUP_WAIT"
        if ! is_proxy_process "$proxy_pid" || ! kill -0 "$proxy_pid" 2>/dev/null; then
            fail "in-container proxy chain failed to stay running"
        fi
        printf 'INFO: started one in-container proxy chain pid %s on port %s\n' \
            "$proxy_pid" "$local_port"
    fi

    proxy_pid_tmp="$PROXY_PID_FILE.tmp.$$"
    printf '%s\n' "$proxy_pid" > "$proxy_pid_tmp" || fail "cannot write proxy PID state"
    mv "$proxy_pid_tmp" "$PROXY_PID_FILE" || fail "cannot install proxy PID state"
}

write_emulator_pid_state() {
    local pid="$1"
    local pid_file_tmp="$PID_FILE.tmp.$$"
    printf '%s\t%s\t%s\n' "$pid" "$AVD_NAME" "$GENERATION_ID" > "$pid_file_tmp" || \
        fail "cannot write emulator PID state"
    mv "$pid_file_tmp" "$PID_FILE" || fail "cannot install emulator PID state"
}

write_ready_state() {
    local pid="$1"
    local serial="$2"
    local state_file_tmp="$STATE_FILE.tmp.$$"
    local ready_at
    ready_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '{"generation_id":"%s","pid":%s,"avd_name":"%s","device_id":"%s",' \
        "$GENERATION_ID" "$pid" "$AVD_NAME" "$serial" > "$state_file_tmp" || \
        fail "cannot write emulator ready state"
    printf '"log_path":"%s","ready":true,"ready_at":"%s"}\n' \
        "$EMULATOR_LOG_PATH" "$ready_at" >> "$state_file_tmp" || \
        fail "cannot complete emulator ready state"
    mv "$state_file_tmp" "$STATE_FILE" || fail "cannot install emulator ready state"
}

shutdown_previous_emulator

EMULATOR_OPTIONS=(-no-audio -no-snapshot -gpu swiftshader_indirect)
if [ "$AVD_NAME" = "Pixel_6_API_33" ]; then
    EMULATOR_OPTIONS+=(-grpc 8554)
fi
if [ "${ENABLE_VNC:-false}" = "true" ] || [ "${ENABLE_VNC:-false}" = "1" ]; then
    export DISPLAY=:0
else
    EMULATOR_OPTIONS+=(-no-window)
fi

printf '\n===== MobileWorld emulator generation %s avd=%s started=%s =====\n' \
    "$GENERATION_ID" "$AVD_NAME" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EMULATOR_LOG_PATH"
printf 'INFO: starting emulator generation %s for AVD %s; log=%s\n' \
    "$GENERATION_ID" "$AVD_NAME" "$EMULATOR_LOG_PATH"
nohup emulator -avd "$AVD_NAME" "${EMULATOR_OPTIONS[@]}" >> "$EMULATOR_LOG_PATH" 2>&1 &
NEW_EMULATOR_PID=$!
write_emulator_pid_state "$NEW_EMULATOR_PID"

if ! wait_for_new_emulator "$NEW_EMULATOR_PID" "$EMULATOR_TIMEOUT"; then
    fail "new emulator process did not reach the unique online/boot-complete postcondition"
fi

# `adb root` commonly restarts adbd and can return non-zero while the same
# emulator is reconnecting. Its return code is therefore diagnostic only;
# the exact-device reconnect/boot postcondition below is authoritative.
ROOT_OUTPUT=''
ROOT_RETURN_CODE=0
ROOT_OUTPUT=$(adb -s "$DEVICE_ID" root 2>&1) || ROOT_RETURN_CODE=$?
if [ -n "$ROOT_OUTPUT" ]; then
    printf 'INFO: adb root diagnostic: %s\n' "$(printf '%s' "$ROOT_OUTPUT" | tail -c 1000)"
fi
if [ "$ROOT_RETURN_CODE" -ne 0 ]; then
    printf 'WARNING: adb root returned %s; verifying device reconnect instead\n' \
        "$ROOT_RETURN_CODE" >&2
fi
if ! wait_for_device_reconnect "$DEVICE_ID" "$ROOT_RECONNECT_TIMEOUT"; then
    fail "emulator did not reconnect as an online boot-complete device after adb root"
fi

if ! adb -s "$DEVICE_ID" shell input keyevent 82; then
    printf 'WARNING: failed to send the post-boot unlock keyevent\n' >&2
fi
disable_animation "$DEVICE_ID"

# Configure Android system-wide HTTP proxy if this container received one.
# Reuse the one exact proxy_chain.py process across emulator generations so a
# health restart cannot duplicate listeners. Do not print the upstream URL;
# it may contain transport credentials.
if [ -n "${HTTP_PROXY:-${http_proxy:-}}" ]; then
    UPSTREAM="${HTTP_PROXY:-$http_proxy}"
    LOCAL_PROXY_PORT="${LOCAL_PROXY_PORT:-38888}"
    is_nonnegative_number "$LOCAL_PROXY_PORT" || fail "LOCAL_PROXY_PORT must be an integer"
    ensure_proxy_chain "$UPSTREAM" "$LOCAL_PROXY_PORT"

    adb -s "$DEVICE_ID" shell settings put global http_proxy \
        "10.0.2.2:${LOCAL_PROXY_PORT}" || fail "cannot configure Android HTTP proxy"
    adb -s "$DEVICE_ID" shell settings put global global_http_proxy_host \
        "10.0.2.2" || fail "cannot configure Android proxy host"
    adb -s "$DEVICE_ID" shell settings put global global_http_proxy_port \
        "$LOCAL_PROXY_PORT" || fail "cannot configure Android proxy port"
    printf 'INFO: Android proxy configured through the single in-container chain on port %s\n' \
        "$LOCAL_PROXY_PORT"
fi

write_ready_state "$NEW_EMULATOR_PID" "$DEVICE_ID"
printf 'READY: generation=%s device=%s pid=%s log=%s\n' \
    "$GENERATION_ID" "$DEVICE_ID" "$NEW_EMULATOR_PID" "$EMULATOR_LOG_PATH"
exit 0
