/* SPDX-License-Identifier: GPL-2.0 */
#ifndef OE_ADAPTIVE_H
#define OE_ADAPTIVE_H

#define OE_AGENT_VERSION "0.1.3"
#define OE_MIN_SLICE_US 100ULL
#define OE_MAX_SLICE_US 50000ULL
#define OE_STAT_COUNT 13
#define OE_POLICY_WAKEUP_PREEMPT 1ULL
#define OE_KICK_INTERVAL_NS 200000ULL
#define OE_TRACKED_CPUS 1024

struct oe_control {
        u64 generation;
        u64 slice_ns;
        u64 max_slice_ns;
        u64 heartbeat_ns;
        u64 policy_flags;
        u64 kick_interval_ns;
};

#endif
