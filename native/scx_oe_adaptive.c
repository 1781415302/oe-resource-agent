/* SPDX-License-Identifier: GPL-2.0 */
/* Uses the user's openEuler SCX_OPS/UEI compatibility interfaces.
 * Derived lifecycle: Copyright (c) 2022 Meta Platforms, Inc. and affiliates;
 * Tejun Heo <tj@kernel.org>; David Vernet <dvernet@meta.com>.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <bpf/bpf.h>
#include <scx/common.h>
#include "oe_adaptive.h"
#include "scx_oe_adaptive.bpf.skel.h"

static volatile sig_atomic_t exit_requested;
static bool verbose;

static uint64_t monotonic_ns(void)
{
        struct timespec stamp;

        if (clock_gettime(CLOCK_MONOTONIC, &stamp)) {
                perror("clock_gettime");
                exit(1);
        }
        return (uint64_t)stamp.tv_sec * 1000000000ULL + stamp.tv_nsec;
}

static bool parse_unsigned(const char *text, uint64_t *value)
{
        unsigned long long parsed;
        char *end;
        size_t i;

        if (!text || !text[0])
                return false;
        for (i = 0; text[i]; i++)
                if (!isdigit((unsigned char)text[i]))
                        return false;
        errno = 0;
        parsed = strtoull(text, &end, 10);
        if (errno || *end)
                return false;
        *value = parsed;
        return true;
}

static int libbpf_print(enum libbpf_print_level level, const char *format, va_list args)
{
        if (level == LIBBPF_DEBUG && !verbose)
                return 0;
        return vfprintf(stderr, format, args);
}

static void stop_handler(int signum)
{
        (void)signum;
        exit_requested = 1;
}

static void help(const char *program)
{
        printf("scx_oe_adaptive %s\n", OE_AGENT_VERSION);
        printf("Usage: %s --pin-dir DIR [--slice-us N] [--lease-sec N] [--wakeup-preempt 0|1]\n", program);
        puts("  --describe       Resolve kernel defaults without attaching SCX");
        puts("  --version        Display version");
        puts("  --get-pin PATH --expected-id ID");
        puts("  --set-pin PATH --expected-id ID --expected-generation GEN --slice-us N");
        puts("  --heartbeat-pin PATH --expected-id ID --expected-generation GEN");
}

static void print_control(const struct oe_control *value, unsigned int id)
{
        printf("{\"ok\":true,\"map_id\":%u,\"generation\":%" PRIu64
               ",\"slice_ns\":%" PRIu64 ",\"max_slice_ns\":%" PRIu64
               ",\"heartbeat_ns\":%" PRIu64 ",\"policy_flags\":%" PRIu64
               ",\"wakeup_preempt\":%s,\"kick_interval_ns\":%" PRIu64 "}\n", id,
               (uint64_t)value->generation, (uint64_t)value->slice_ns,
               (uint64_t)value->max_slice_ns, (uint64_t)value->heartbeat_ns,
               (uint64_t)value->policy_flags, value->policy_flags & OE_POLICY_WAKEUP_PREEMPT ? "true" : "false",
               (uint64_t)value->kick_interval_ns);
}

static int control_command(const char *pin, uint64_t expected_id,
                           uint64_t expected_generation, bool have_generation,
                           uint64_t slice_us, int action, bool have_preempt, uint64_t preempt)
{
        struct bpf_map_info info = {};
        struct oe_control value;
        struct stat lock_stat;
        __u32 size = sizeof(info), key = 0;
        char lock_path[PATH_MAX];
        int fd = -1, lock_fd = -1, ret = 1;

        if (!expected_id || expected_id > UINT32_MAX || (action != 0 && !have_generation)) {
                fputs("Expected map ID/generation required.\n", stderr);
                return 2;
        }
        fd = bpf_obj_get(pin);
        if (fd < 0 || bpf_obj_get_info_by_fd(fd, &info, &size)) {
                perror("opening control map");
                goto out;
        }
        if (info.id != expected_id || info.type != BPF_MAP_TYPE_HASH ||
            info.key_size != sizeof(key) || info.value_size != sizeof(value) ||
            info.max_entries != 1 || strcmp(info.name, "control")) {
                fputs("Control map identity or shape mismatch.\n", stderr);
                goto out;
        }
        if (action != 0) {
                snprintf(lock_path, sizeof(lock_path), "/run/oeagent-control-%u.lock", info.id);
                lock_fd = open(lock_path, O_CREAT | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
                if (lock_fd < 0 || fstat(lock_fd, &lock_stat) || !S_ISREG(lock_stat.st_mode) ||
                    lock_stat.st_uid != geteuid() || lock_stat.st_nlink != 1 || flock(lock_fd, LOCK_EX)) {
                        perror("locking control update");
                        goto out;
                }
        }
        if (bpf_map_lookup_elem(fd, &key, &value)) {
                perror("reading control map");
                goto out;
        }
        if (value.policy_flags & ~OE_POLICY_WAKEUP_PREEMPT ||
            value.kick_interval_ns != OE_KICK_INTERVAL_NS) {
                fputs("Control policy flags or kick interval invalid.\n", stderr);
                goto out;
        }
        if (action != 0) {
                if (value.generation != expected_generation) {
                        fputs("Stale control generation.\n", stderr);
                        ret = 3;
                        goto out;
                }
                if (action == 1) {
                        if (slice_us < OE_MIN_SLICE_US || slice_us > OE_MAX_SLICE_US ||
                            slice_us * 1000ULL > value.max_slice_ns || value.generation == UINT64_MAX) {
                                fputs("Slice outside validated kernel bounds.\n", stderr);
                                ret = 2;
                                goto out;
                        }
                        value.slice_ns = slice_us * 1000ULL;
                        if (have_preempt)
                                value.policy_flags = preempt ? OE_POLICY_WAKEUP_PREEMPT : 0;
                        value.generation++;
                }
                value.heartbeat_ns = monotonic_ns();
                if (bpf_map_update_elem(fd, &key, &value, BPF_EXIST)) {
                        perror("updating control map");
                        goto out;
                }
                if (bpf_map_lookup_elem(fd, &key, &value)) {
                        perror("reading updated control map");
                        goto out;
                }
        }
        print_control(&value, info.id);
        ret = 0;
out:
        if (lock_fd >= 0)
                close(lock_fd);
        if (fd >= 0)
                close(fd);
        return ret;
}

static int stats(struct scx_oe_adaptive *skel, __u64 *totals, __u64 *per_cpu, int nr_cpus)
{
        __u32 index;
        int cpu;

        memset(totals, 0, OE_STAT_COUNT * sizeof(*totals));
        for (index = 0; index < OE_STAT_COUNT; index++) {
                if (bpf_map_lookup_elem(bpf_map__fd(skel->maps.stats), &index, per_cpu))
                        return -1;
                for (cpu = 0; cpu < nr_cpus; cpu++)
                        totals[index] += per_cpu[cpu];
        }
        return 0;
}

int main(int argc, char **argv)
{
        static const struct option options[] = {
                {"pin-dir", required_argument, NULL, 'p'},
                {"slice-us", required_argument, NULL, 's'},
                {"lease-sec", required_argument, NULL, 'l'},
                {"wakeup-preempt", required_argument, NULL, 'w'},
                {"describe", no_argument, NULL, 'd'},
                {"version", no_argument, NULL, 'V'},
                {"get-pin", required_argument, NULL, 'g'},
                {"set-pin", required_argument, NULL, 'u'},
                {"heartbeat-pin", required_argument, NULL, 'b'},
                {"expected-id", required_argument, NULL, 'i'},
                {"expected-generation", required_argument, NULL, 'n'},
                {"verbose", no_argument, NULL, 'v'},
                {"help", no_argument, NULL, 'h'},
                {NULL, 0, NULL, 0}
        };
        struct scx_oe_adaptive *skel;
        struct bpf_link *link;
        struct bpf_map_info info = {};
        struct oe_control value;
        struct stat pin_stat;
        __u32 size = sizeof(info), key = 0;
        __u64 *per_cpu, totals[OE_STAT_COUNT], ecode;
        const char *pin_dir = NULL, *command_pin = NULL;
        char pin_path[PATH_MAX];
        uint64_t slice_us = 0, lease_seconds = 15, expected_id = 0, expected_generation = 0, preempt = 0;
        bool describe = false, failed = false, have_generation = false, have_preempt = false;
        int action = -1, option, nr_cpus, control_fd;

        while ((option = getopt_long(argc, argv, "h", options, NULL)) != -1) {
                switch (option) {
                case 'p': pin_dir = optarg; break;
                case 's': if (!parse_unsigned(optarg, &slice_us)) return 2; break;
                case 'l': if (!parse_unsigned(optarg, &lease_seconds) || lease_seconds > 120) return 2; break;
                case 'w': if (!parse_unsigned(optarg, &preempt) || preempt > 1) return 2; have_preempt = true; break;
                case 'd': describe = true; break;
                case 'V': puts(OE_AGENT_VERSION); return 0;
                case 'g': action = 0; command_pin = optarg; break;
                case 'u': action = 1; command_pin = optarg; break;
                case 'b': action = 2; command_pin = optarg; break;
                case 'i': if (!parse_unsigned(optarg, &expected_id)) return 2; break;
                case 'n': if (!parse_unsigned(optarg, &expected_generation)) return 2; have_generation = true; break;
                case 'v': verbose = true; break;
                case 'h': help(argv[0]); return 0;
                default: help(argv[0]); return 2;
                }
        }
        if (optind != argc)
                return 2;
        libbpf_set_print(libbpf_print);
        if (command_pin)
                return control_command(command_pin, expected_id, expected_generation, have_generation, slice_us, action, have_preempt, preempt);
        if (!describe && (!pin_dir || lstat(pin_dir, &pin_stat) || !S_ISDIR(pin_stat.st_mode) ||
                          pin_stat.st_uid != geteuid() || (pin_stat.st_mode & 0077))) {
                fputs("Provide an owned private pin directory on bpffs (mode 0700).\n", stderr);
                return 2;
        }
        signal(SIGINT, stop_handler);
        signal(SIGTERM, stop_handler);
        skel = SCX_OPS_OPEN(oe_adaptive_ops, scx_oe_adaptive);
        value = (struct oe_control){ .generation = 0,
                                     .max_slice_ns = skel->rodata->__SCX_SLICE_DFL,
                                     .heartbeat_ns = monotonic_ns(),
                                     .policy_flags = preempt ? OE_POLICY_WAKEUP_PREEMPT : 0,
                                     .kick_interval_ns = OE_KICK_INTERVAL_NS };
        if (!value.max_slice_ns || (slice_us && (slice_us < OE_MIN_SLICE_US ||
            slice_us > OE_MAX_SLICE_US || slice_us * 1000ULL > value.max_slice_ns))) {
                fputs("Invalid kernel default or initial slice.\n", stderr);
                scx_oe_adaptive__destroy(skel);
                return 2;
        }
        value.slice_ns = slice_us ? slice_us * 1000ULL : value.max_slice_ns;
        skel->rodata->initial_slice_ns = value.slice_ns;
        skel->rodata->sleep_credit_ns = value.max_slice_ns;
        printf("OE_ADAPTIVE_CONFIG {\"version\":\"%s\",\"scheduler\":\"oe_adaptive\","
               "\"slice_ns\":%" PRIu64 ",\"max_slice_ns\":%" PRIu64 ",\"lease_seconds\":%" PRIu64
               ",\"wakeup_preempt\":%s,\"kick_interval_ns\":%" PRIu64 "}\n",
               OE_AGENT_VERSION, (uint64_t)value.slice_ns, (uint64_t)value.max_slice_ns, lease_seconds,
               preempt ? "true" : "false", (uint64_t)value.kick_interval_ns);
        fflush(stdout);
        if (describe) {
                scx_oe_adaptive__destroy(skel);
                return 0;
        }
        nr_cpus = libbpf_num_possible_cpus();
        per_cpu = nr_cpus > 0 ? calloc(nr_cpus, sizeof(*per_cpu)) : NULL;
        if (!per_cpu) {
                fputs("Cannot allocate CPU statistics.\n", stderr);
                scx_oe_adaptive__destroy(skel);
                return 1;
        }
        SCX_OPS_LOAD(skel, oe_adaptive_ops, scx_oe_adaptive, uei);
        control_fd = bpf_map__fd(skel->maps.control);
        if (bpf_map_update_elem(control_fd, &key, &value, BPF_NOEXIST) ||
            bpf_obj_get_info_by_fd(control_fd, &info, &size) ||
            snprintf(pin_path, sizeof(pin_path), "%s/control", pin_dir) >= (int)sizeof(pin_path) ||
            bpf_map__pin(skel->maps.control, pin_path)) {
                perror("initializing and pinning control map");
                free(per_cpu);
                scx_oe_adaptive__destroy(skel);
                return 1;
        }
        link = SCX_OPS_ATTACH(skel, oe_adaptive_ops, scx_oe_adaptive);
        printf("OE_ADAPTIVE_READY {\"map_id\":%u,\"generation\":0,\"slice_ns\":%" PRIu64 "}\n",
               info.id, (uint64_t)value.slice_ns);
        fflush(stdout);
        while (!exit_requested && !UEI_EXITED(skel, uei)) {
                if (stats(skel, totals, per_cpu, nr_cpus) ||
                    bpf_map_lookup_elem(control_fd, &key, &value) || totals[3] ||
                    (value.policy_flags & ~OE_POLICY_WAKEUP_PREEMPT) || value.kick_interval_ns != OE_KICK_INTERVAL_NS) {
                        fputs("CPU accounting or control read failed; unloading.\n", stderr);
                        failed = true;
                        break;
                }
                if (lease_seconds && monotonic_ns() - value.heartbeat_ns > lease_seconds * 1000000000ULL) {
                        fputs("OE_ADAPTIVE_LEASE_EXPIRED: unloading to kernel default scheduler.\n", stderr);
                        failed = true;
                        break;
                }
                printf("OE_ADAPTIVE_STATS {\"generation\":%" PRIu64 ",\"slice_ns\":%" PRIu64
                       ",\"local\":%" PRIu64 ",\"global\":%" PRIu64 ",\"accounting_errors\":%" PRIu64
                       ",\"wakeup_preempt\":%s,\"eligible_wakes\":%" PRIu64
                       ",\"preempt_kicks\":%" PRIu64 ",\"kick_throttled\":%" PRIu64
                       ",\"wakeup_enqueues\":%" PRIu64 ",\"short_sleepers\":%" PRIu64 ",\"busy_running\":%" PRIu64 "}\n",
                       (uint64_t)value.generation, (uint64_t)value.slice_ns,
                       (uint64_t)totals[0], (uint64_t)totals[1], (uint64_t)totals[3],
                       value.policy_flags & OE_POLICY_WAKEUP_PREEMPT ? "true" : "false",
                       (uint64_t)totals[7], (uint64_t)totals[8], (uint64_t)totals[9],
                       (uint64_t)totals[10], (uint64_t)totals[11], (uint64_t)totals[12]);
                fflush(stdout);
                sleep(1);
        }
        if (stats(skel, totals, per_cpu, nr_cpus) || totals[3])
                failed = true;
        bpf_link__destroy(link);
        ecode = UEI_REPORT(skel, uei);
        if (bpf_map__unpin(skel->maps.control, pin_path)) {
                perror("unpinning owned control map");
                failed = true;
        }
        scx_oe_adaptive__destroy(skel);
        free(per_cpu);
        return failed || ecode ? 1 : 0;
}
