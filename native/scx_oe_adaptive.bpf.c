/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Fixed, configurable dispatch slice experiment for openEuler sched_ext.
 * Derived from the user's unmodified scx_simple.bpf.c on 2026-10-14.
 *
 * Copyright (c) 2022 Meta Platforms, Inc. and affiliates.
 * Copyright (c) 2022 Tejun Heo <tj@kernel.org>
 * Copyright (c) 2022 David Vernet <dvernet@meta.com>
 *
 * This version retains weighted virtual-time ordering and the original
 * absolute sleep-credit limit. The user-space Agent updates a single runtime control map.
 * This backend retains weighted vtime and CPU-runtime accounting.
 */
#include <scx/common.bpf.h>
#include "oe_adaptive.h"

char _license[] SEC("license") = "GPL";

const volatile bool fifo_sched;
/* Filled by the loader before load; read-only for the whole attachment.
 * SCX_SLICE_DFL is a runtime-filled compatibility enum in this backport,
 * so it cannot appear in a C global initializer.
 */
const volatile u64 initial_slice_ns = 0;
const volatile u64 sleep_credit_ns = 0;

static u64 vtime_now;
UEI_DEFINE(uei);

#define SHARED_DSQ 0

enum stat_index {
        STAT_LOCAL,
        STAT_GLOBAL,
        STAT_SLICE_CAPPED,
        STAT_ACCOUNTING_ERROR,
        STAT_INITIAL_STOP,
        STAT_REFILL_OBSERVED,
        STAT_CONTINUED,
        STAT_WAKE_CANDIDATE,
        STAT_PREEMPT_KICK,
        STAT_KICK_THROTTLED,
        STAT_WAKE_ENQUEUE,
        STAT_SHORT_SLEEPER,
        STAT_BUSY_RUNNING,
        STAT_COUNT,
};

/* During enable, the core runs tasks in bypass mode with its default slice.
 * A stopping callback can be observed without an earlier BPF running callback.
 * Distinguish that initial transition from a real accounting inconsistency.
 */
struct task_ctx {
        u64 runtime_at_start;
        u64 last_burst_ns;
        u32 busy_runs;
        bool running;
        bool seen_running;
        bool slept;
        bool tracked_cpu;
};

struct cpu_state {
        u64 last_kick_ns;
        u32 current_pid;
        u32 busy;
};

struct {
        __uint(type, BPF_MAP_TYPE_ARRAY);
        __uint(max_entries, OE_TRACKED_CPUS);
        __type(key, u32);
        __type(value, struct cpu_state);
} cpu_states SEC(".maps");

struct {
        __uint(type, BPF_MAP_TYPE_TASK_STORAGE);
        __uint(map_flags, BPF_F_NO_PREALLOC);
        __type(key, int);
        __type(value, struct task_ctx);
} task_context SEC(".maps");

struct {
        __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
        __uint(key_size, sizeof(u32));
        __uint(value_size, sizeof(u64));
        __uint(max_entries, STAT_COUNT);
} stats SEC(".maps");

struct {
        __uint(type, BPF_MAP_TYPE_HASH);
        __uint(max_entries, 1);
        __type(key, u32);
        __type(value, struct oe_control);
} control SEC(".maps");

/* Hash replacement publishes the configuration as a new map value. */
static __always_inline u64 configured_slice(void)
{
        u32 key = 0;
        struct oe_control *value = bpf_map_lookup_elem(&control, &key);
        u64 slice;

        if (!value)
                return initial_slice_ns;
        slice = value->slice_ns;
        if (slice < OE_MIN_SLICE_US * 1000ULL || slice > sleep_credit_ns)
                return initial_slice_ns;
        return slice;
}

static void stat_inc(u32 idx)
{
        u64 *counter = bpf_map_lookup_elem(&stats, &idx);

        if (counter)
                (*counter)++;
}

static __always_inline bool wakeup_preempt_enabled(void)
{
        u32 key = 0;
        struct oe_control *value = bpf_map_lookup_elem(&control, &key);

        return value && value->policy_flags == OE_POLICY_WAKEUP_PREEMPT &&
               value->kick_interval_ns == OE_KICK_INTERVAL_NS;
}

static void maybe_preempt(struct task_struct *p, u64 enq_flags)
{
        struct task_ctx *ctx;
        struct cpu_state *cpu;
        u64 previous, now;
        u32 key;
        s32 target_cpu;

        if (!wakeup_preempt_enabled() || !(enq_flags & SCX_ENQ_WAKEUP) ||
            (p->flags & PF_KTHREAD))
                return;
        stat_inc(STAT_WAKE_ENQUEUE);
        ctx = bpf_task_storage_get(&task_context, p, 0, 0);
        if (!ctx || !ctx->slept || !ctx->seen_running || !ctx->last_burst_ns ||
            ctx->last_burst_ns > configured_slice() / 2)
                return;
        stat_inc(STAT_SHORT_SLEEPER);
        /* Single-CPU affinity bypasses select_cpu in the core. At enqueue,
         * the task's rq association already identifies the wakeup target. */
        target_cpu = scx_bpf_task_cpu(p);
        if (target_cpu < 0)
                return;
        key = (u32)target_cpu;
        cpu = bpf_map_lookup_elem(&cpu_states, &key);
        if (!cpu || !cpu->busy || cpu->current_pid == p->pid)
                return;
        stat_inc(STAT_WAKE_CANDIDATE);
        now = bpf_ktime_get_ns();
        previous = cpu->last_kick_ns;
        if (now - previous < OE_KICK_INTERVAL_NS ||
            __sync_val_compare_and_swap(&cpu->last_kick_ns, previous, now) != previous) {
                stat_inc(STAT_KICK_THROTTLED);
                return;
        }
        /* Queue order remains weighted vtime. The kick requests a reschedule;
         * it neither changes affinity nor bypasses real-time scheduling. */
        scx_bpf_kick_cpu(target_cpu, SCX_KICK_PREEMPT);
        stat_inc(STAT_PREEMPT_KICK);
}

s32 BPF_STRUCT_OPS(oe_adaptive_select_cpu, struct task_struct *p,
                   s32 prev_cpu, u64 wake_flags)
{
        bool is_idle = false;
        s32 cpu;

        cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &is_idle);
        if (is_idle) {
                stat_inc(STAT_LOCAL);
                scx_bpf_dsq_insert(p, SCX_DSQ_LOCAL, configured_slice(), 0);
        }
        return cpu;
}

void BPF_STRUCT_OPS(oe_adaptive_enqueue, struct task_struct *p, u64 enq_flags)
{
        stat_inc(STAT_GLOBAL);

        if (fifo_sched) {
                scx_bpf_dsq_insert(p, SHARED_DSQ, configured_slice(), enq_flags);
        } else {
                u64 vtime = p->scx.dsq_vtime;

                /* Keep the original absolute sleep bonus, independent of slice. */
                if (time_before(vtime, vtime_now - sleep_credit_ns))
                        vtime = vtime_now - sleep_credit_ns;

                scx_bpf_dsq_insert_vtime(p, SHARED_DSQ, configured_slice(), vtime,
                                         enq_flags);
        }
        maybe_preempt(p, enq_flags);
}

void BPF_STRUCT_OPS(oe_adaptive_dispatch, s32 cpu, struct task_struct *prev)
{
        if (scx_bpf_dsq_move_to_local(SHARED_DSQ))
                return;
        /* With no queued competitor, the core can keep prev running without
         * a new running callback and refill its exhausted slice with 20ms.
         * Supply our configured slice explicitly on that continuation path.
         */
        if (prev && (prev->scx.flags & SCX_TASK_QUEUED)) {
                prev->scx.slice = configured_slice();
                stat_inc(STAT_CONTINUED);
        }
}

void BPF_STRUCT_OPS(oe_adaptive_running, struct task_struct *p)
{
        struct task_ctx *tctx = bpf_task_storage_get(&task_context, p, 0, 0);

        if (!tctx) {
                stat_inc(STAT_ACCOUNTING_ERROR);
                scx_bpf_error("oe_adaptive: missing task context in running");
                return;
        }
        tctx->running = true;
        tctx->seen_running = true;
        tctx->runtime_at_start = p->se.sum_exec_runtime;
        tctx->slept = false;
        tctx->tracked_cpu = false;
        if (wakeup_preempt_enabled()) {
                u32 cpu = bpf_get_smp_processor_id();
                struct cpu_state *state = bpf_map_lookup_elem(&cpu_states, &cpu);

                if (state) {
                        tctx->tracked_cpu = true;
                        state->current_pid = p->pid;
                        state->busy = tctx->busy_runs >= 2 && !(p->flags & PF_KTHREAD);
                        if (state->busy)
                                stat_inc(STAT_BUSY_RUNNING);
                }
        }
        /* The core can initialize/refill a task with its default slice.
         * Cap that budget before it executes so accounting uses our budget.
         * This is not preemption of an already running competing task.
         */
        if (p->scx.slice > configured_slice()) {
                p->scx.slice = configured_slice();
                stat_inc(STAT_SLICE_CAPPED);
        }

        if (fifo_sched)
                return;
        if (time_before(vtime_now, p->scx.dsq_vtime))
                vtime_now = p->scx.dsq_vtime;
}

void BPF_STRUCT_OPS(oe_adaptive_stopping, struct task_struct *p, bool runnable)
{
        struct task_ctx *tctx;
        u64 used;

        if (fifo_sched)
                return;

        tctx = bpf_task_storage_get(&task_context, p, 0, 0);
        if (!tctx) {
                stat_inc(STAT_ACCOUNTING_ERROR);
                scx_bpf_error("oe_adaptive: missing task context in stopping");
                return;
        }
        if (!tctx->running) {
                if (!tctx->seen_running)
                        stat_inc(STAT_INITIAL_STOP);
                else {
                        stat_inc(STAT_ACCOUNTING_ERROR);
                        scx_bpf_error("oe_adaptive: unpaired stopping pid=%d remaining=%llu",
                                      p->pid, p->scx.slice);
                }
                return;
        }
        tctx->running = false;

        /* The core can refill a task's slice during enable or task attribute
         * changes. Remaining slice is therefore not a reliable execution
         * clock. Charge the increase in the kernel's cumulative CPU runtime,
         * updated by update_curr_scx() before stopping is called.
         * This also avoids charging unused time when a task yields.
         */
        if (p->se.sum_exec_runtime < tctx->runtime_at_start || !p->scx.weight) {
                stat_inc(STAT_ACCOUNTING_ERROR);
                scx_bpf_error("oe_adaptive: invalid runtime or weight pid=%d", p->pid);
                return;
        }
        if (p->scx.slice > configured_slice())
                stat_inc(STAT_REFILL_OBSERVED);
        used = p->se.sum_exec_runtime - tctx->runtime_at_start;
        tctx->last_burst_ns = used;
        tctx->slept = !runnable;
        if (!runnable)
                tctx->busy_runs = 0;
        else if (tctx->busy_runs < 255)
                tctx->busy_runs++;
        if (tctx->tracked_cpu) {
                u32 cpu = bpf_get_smp_processor_id();
                struct cpu_state *state = bpf_map_lookup_elem(&cpu_states, &cpu);

                if (state && state->current_pid == p->pid) {
                        state->busy = 0;
                        state->current_pid = 0;
                }
                tctx->tracked_cpu = false;
        }
        p->scx.dsq_vtime += used * 100 / p->scx.weight;
}

void BPF_STRUCT_OPS(oe_adaptive_enable, struct task_struct *p)
{
        struct task_ctx *tctx = bpf_task_storage_get(&task_context, p, 0, 0);

        if (!tctx) {
                stat_inc(STAT_ACCOUNTING_ERROR);
                scx_bpf_error("oe_adaptive: missing task context in enable");
                return;
        }
        tctx->running = false;
        tctx->seen_running = false;
        tctx->slept = false;
        tctx->tracked_cpu = false;
        tctx->busy_runs = 0;
        tctx->last_burst_ns = 0;
        p->scx.dsq_vtime = vtime_now;
}

s32 BPF_STRUCT_OPS(oe_adaptive_init_task, struct task_struct *p,
                   struct scx_init_task_args *args)
{
        if (bpf_task_storage_get(&task_context, p, 0,
                                BPF_LOCAL_STORAGE_GET_F_CREATE))
                return 0;
        return -ENOMEM;
}

s32 BPF_STRUCT_OPS_SLEEPABLE(oe_adaptive_init)
{
        return scx_bpf_create_dsq(SHARED_DSQ, -1);
}

void BPF_STRUCT_OPS(oe_adaptive_exit, struct scx_exit_info *ei)
{
        UEI_RECORD(uei, ei);
}

SCX_OPS_DEFINE(oe_adaptive_ops,
               .select_cpu = (void *)oe_adaptive_select_cpu,
               .enqueue    = (void *)oe_adaptive_enqueue,
               .dispatch   = (void *)oe_adaptive_dispatch,
               .running    = (void *)oe_adaptive_running,
               .stopping   = (void *)oe_adaptive_stopping,
               .enable     = (void *)oe_adaptive_enable,
               .init_task  = (void *)oe_adaptive_init_task,
               .init       = (void *)oe_adaptive_init,
               .exit       = (void *)oe_adaptive_exit,
               .name       = "oe_adaptive");
