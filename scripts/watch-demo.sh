#!/usr/bin/env bash
# Independent SSH evidence: reads Linux files/tools directly, never the dashboard API.
set -u
once=false
if [[ "${1:-}" == "--once" ]]; then once=true; fi
while true; do
    if [[ -t 1 ]]; then printf '\033[H\033[2J'; fi
    printf '=== openEuler independent SSH evidence ===\n'
    date -Iseconds
    uname -a
    printf 'boot_id: '
    cat /proc/sys/kernel/random/boot_id
    printf 'sched_ext: '
    cat /sys/kernel/sched_ext/state
    printf 'schedstats enabled: '
    cat /proc/sys/kernel/sched_schedstats
    agent_pid=$(systemctl show oe-live-dashboard.service --property MainPID --value)
    printf '\n--- owned processes (PID PPID CPU COMMAND) ---\n'
    if [[ "$agent_pid" =~ ^[1-9][0-9]*$ && -d "/proc/$agent_pid" ]]; then
        # Workloads are spawned by different Python threads; PPID covers them all.
        children=$(ps --ppid "$agent_pid" -o pid=)
        ps -p "$agent_pid" -o pid,ppid,psr,comm
        for child_pid in $children; do
            [[ -r "/proc/$child_pid/comm" ]] || continue
            comm=$(cat "/proc/$child_pid/comm")
            case "$comm" in
                nginx|sysbench|wrk|scx_oe_adaptive|oe-compute|oe-load-*|oe-burst-http)
                    ps -p "$child_pid" -o pid,ppid,psr,comm --no-headers
                    if [[ "$comm" == nginx || "$comm" == sysbench ]]; then
                        printf 'PID %s schedstat (run_ns wait_ns schedule_count): ' "$child_pid"
                        cat "/proc/$child_pid/schedstat" 2>/dev/null || true
                        taskset -pc "$child_pid" 2>/dev/null || true
                    fi
                    ;;
            esac
        done
    else
        printf 'Dashboard service is stopped.\n'
    fi
    printf '\n--- SCX control Map (kernel readback) ---\n'
    found=false
    for pin in /sys/fs/bpf/oeagent/*/control; do
        [[ -e "$pin" ]] || continue
        found=true
        bpftool -j map show pinned "$pin"
        bpftool -j map lookup pinned "$pin" key hex 00 00 00 00
    done
    if ! "$found"; then printf 'No Agent SCX control Map is attached.\n'; fi
    printf '\n--- Cgroup CPU weights and membership ---\n'
    for mount in /sys/fs/cgroup/cpu,cpuacct /sys/fs/cgroup/cpu; do
        [[ -d "$mount" ]] || continue
        for name in auto_latency auto_batch live-low live-high; do
            group="$mount/oeagent/$name"
            [[ -d "$group" ]] || continue
            printf '%s cpu.shares: ' "$name"
            cat "$group/cpu.shares"
            printf 'members: '
            tr '\n' ' ' <"$group/cgroup.procs"
            printf '\n'
        done
        break
    done
    printf '\n--- eBPF TC hook on lo ---\n'
    tc -s filter show dev lo egress
    if [[ -e /sys/fs/bpf/oeagent_net/oe_net_stats_map ]]; then
        bpftool -j map lookup pinned /sys/fs/bpf/oeagent_net/oe_net_stats_map key hex 00 00 00 00
    fi
    if "$once"; then break; fi
    printf '\nRefresh: 2 seconds. Ctrl+C to exit.\n'
    sleep 2
done
