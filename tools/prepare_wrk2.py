"""Build pinned wrk2 with aligned measurement and optional continuous windows."""
import argparse
import difflib
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "44a94c17d8e6a0bac8559b53da76848e430cb7a7"
ARCHIVE_SHA = "3bc11d20d7d3e72a6b67ce26407eb7c34ae816c882669dcedf578d1c27ad4c20"


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Pinned upstream anchor changed")
    return text.replace(old, new)


def instrument(header, source):
    header = replace(header, "    uint64_t stop_at;\n", "    uint64_t stop_at;\n"
        "    uint64_t oe_start_ns, oe_end_ns, oe_start_complete, oe_start_bytes, oe_terminal_exclusions;\n"
        "    errors oe_start_errors;\n")
    source = replace(source, '#include "stats.h"\n', '#include "stats.h"\n#include <time.h>\n'
        'static uint64_t oe_monotonic_ns(void) {\n'
        '    struct timespec ts;\n'
        '    if (clock_gettime(CLOCK_MONOTONIC, &ts)) abort();\n'
        '    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;\n}\n')
    source = replace(source, "    thread->start    = time_us();\n    thread->interval = interval;\n",
        "    thread->oe_start_ns = oe_monotonic_ns();\n"
        "    thread->oe_start_complete = thread->complete;\n"
        "    thread->oe_start_bytes = thread->bytes;\n"
        "    thread->oe_start_errors = thread->errors;\n"
        "    thread->start    = time_us();\n    thread->interval = interval;\n")
    source = replace(source, "    aeMain(loop);\n", "    aeMain(loop);\n    thread->oe_end_ns = oe_monotonic_ns();\n")
    source = replace(source, "    if (now >= thread->stop_at) {\n",
                     "    if (now >= thread->stop_at) {\n        thread->oe_terminal_exclusions++;\n")
    output = r'''
    /* Measurement export is restricted to the one-thread benchmark adapter. */
    if (cfg.threads == 1 && threads[0].oe_start_ns) {
        thread *t = &threads[0];
        printf("OE_WRK2_ALIGNED {\"schema_version\":1,\"start_ns\":%"PRIu64
               ",\"end_ns\":%"PRIu64",\"requests\":%"PRIu64",\"bytes\":%"PRIu64
               ",\"latency_samples\":%"PRId64",\"rate\":%"PRIu64
               ",\"terminal_exclusions\":%"PRIu64",\"connections\":%"PRIu64
               ",\"errors\":{\"connect\":%"PRIu64",\"read\":%"PRIu64
               ",\"write\":%"PRIu64",\"status\":%"PRIu64",\"timeout\":%"PRIu64"}}\n",
               t->oe_start_ns, t->oe_end_ns, t->complete - t->oe_start_complete,
               t->bytes - t->oe_start_bytes, t->latency_histogram->total_count, cfg.rate,
               t->oe_terminal_exclusions, cfg.connections,
               t->errors.connect - t->oe_start_errors.connect,
               t->errors.read - t->oe_start_errors.read, t->errors.write - t->oe_start_errors.write,
               t->errors.status - t->oe_start_errors.status, t->errors.timeout - t->oe_start_errors.timeout);
    }
'''
    source = replace(source, "    return 0;\n}\n\nvoid *thread_main", output + "    return 0;\n}\n\nvoid *thread_main")
    return header, source


def instrument_windows(header, source):
    """Compose the old alignment patch with optional 5s window export."""
    header, source = instrument(header, source)
    header = replace(header, "    errors oe_start_errors;\n",
        "    errors oe_start_errors;\n"
        "    struct hdr_histogram *oe_window_histogram;\n"
        "    uint64_t oe_window_seq;\n"
        "    uint64_t oe_window_start_ns;\n"
        "    uint64_t oe_window_complete;\n"
        "    uint64_t oe_window_bytes;\n"
        "    errors oe_window_errors;\n"
        "    uint64_t oe_window_terminal;\n"
        "    uint64_t oe_terminal_at_ready;\n"
        "    uint64_t oe_window_overflows;\n"
        "    uint64_t oe_overflow_total;\n"
        "    uint64_t oe_export_duration_ns;\n")
    helpers = r'''
static uint64_t oe_window_ms;

static void oe_load_window_ms(void) {
    const char *value = getenv("OE_WRK2_WINDOW_MS");
    if (!value || !value[0]) {
        oe_window_ms = 0;
        return;
    }
    if (strcmp(value, "5000") != 0) {
        fprintf(stderr, "OE_WRK2_WINDOW_MS must be unset or 5000\n");
        exit(1);
    }
    oe_window_ms = 5000;
}

static void oe_io_check(int written) {
    if (written < 0) {
        fprintf(stderr, "OE_WRK2_WINDOW write failed\n");
        exit(1);
    }
}

static void oe_flush_export(thread *t, uint64_t started_ns) {
    uint64_t finished_ns;
    oe_io_check(printf("\n"));
    if (fflush(stdout) != 0) {
        fprintf(stderr, "OE_WRK2_WINDOW flush failed\n");
        exit(1);
    }
    finished_ns = oe_monotonic_ns();
    if (finished_ns < started_ns) {
        fprintf(stderr, "OE_WRK2_WINDOW clock moved backwards\n");
        exit(1);
    }
    t->oe_export_duration_ns += finished_ns - started_ns;
}

static void oe_print_errors(errors start, errors end) {
    oe_io_check(printf("{\"connect\":%" PRIu32 ",\"read\":%" PRIu32
        ",\"write\":%" PRIu32 ",\"status\":%" PRIu32 ",\"timeout\":%" PRIu32 "}",
        (uint32_t)(end.connect - start.connect), (uint32_t)(end.read - start.read),
        (uint32_t)(end.write - start.write), (uint32_t)(end.status - start.status),
        (uint32_t)(end.timeout - start.timeout)));
}

static void oe_print_bins(struct hdr_histogram *histogram) {
    struct hdr_recorded_iter iter;
    int first = 1;
    hdr_recorded_iter_init(&iter, histogram);
    oe_io_check(printf("["));
    while (hdr_recorded_iter_next(&iter)) {
        if (iter.iter.count_at_index <= 0) continue;
        if (!first) oe_io_check(printf(","));
        first = 0;
        oe_io_check(printf("[%" PRId64 ",%" PRId64 "]",
            iter.iter.highest_equivalent_value, iter.iter.count_at_index));
    }
    oe_io_check(printf("]"));
}

static void oe_emit_ready(thread *t) {
    uint64_t started_ns = oe_monotonic_ns();
    oe_io_check(printf(
        "OE_WRK2_STREAM {\"schema_version\":1,\"kind\":\"ready\",\"start_ns\":%" PRIu64
        ",\"window_ms\":%" PRIu64 ",\"offered_rps\":%" PRIu64 ",\"connections\":%" PRIu64
        ",\"client_threads\":1,\"pipeline\":1,\"histogram\":\"wrk2_expected_latency_hdr_3sig\"}",
        t->oe_start_ns, oe_window_ms, cfg.rate, cfg.connections));
    oe_flush_export(t, started_ns);
}

static void oe_emit_window(thread *t, uint64_t end_ns, int is_final) {
    uint64_t started_ns;
    if (end_ns < t->oe_window_start_ns) {
        fprintf(stderr, "OE_WRK2_WINDOW clock moved backwards\n");
        exit(1);
    }
    t->oe_window_seq++;
    started_ns = oe_monotonic_ns();
    oe_io_check(printf(
        "OE_WRK2_STREAM {\"schema_version\":1,\"kind\":\"window\",\"seq\":%" PRIu64
        ",\"start_ns\":%" PRIu64 ",\"end_ns\":%" PRIu64 ",\"final\":%s,\"requests\":%" PRIu64
        ",\"bytes\":%" PRIu64 ",\"errors\":",
        t->oe_window_seq, t->oe_window_start_ns, end_ns, is_final ? "true" : "false",
        t->complete - t->oe_window_complete, t->bytes - t->oe_window_bytes));
    oe_print_errors(t->oe_window_errors, t->errors);
    oe_io_check(printf(",\"terminal_exclusions\":%" PRIu64 ",\"histogram_overflows\":%" PRIu64 ",\"bins_us\":",
        t->oe_window_terminal, t->oe_window_overflows));
    oe_print_bins(t->oe_window_histogram);
    oe_io_check(printf("}"));
    oe_flush_export(t, started_ns);
    hdr_reset(t->oe_window_histogram);
    t->oe_window_start_ns = end_ns;
    t->oe_window_complete = t->complete;
    t->oe_window_bytes = t->bytes;
    t->oe_window_errors = t->errors;
    t->oe_window_terminal = 0;
    t->oe_window_overflows = 0;
}

static void oe_emit_tail(thread *t) {
    if (t->oe_end_ns < t->oe_window_start_ns) {
        fprintf(stderr, "OE_WRK2_WINDOW clock moved backwards\n");
        exit(1);
    }
    if (t->oe_end_ns == t->oe_window_start_ns) return;
    oe_emit_window(t, t->oe_end_ns, 1);
}

static int oe_window_tick(aeEventLoop *loop, long long id, void *data) {
    thread *t = data;
    uint64_t end_ns = oe_monotonic_ns();
    (void)loop;
    (void)id;
    if (end_ns != t->oe_window_start_ns)
        oe_emit_window(t, end_ns, 0);
    return (int)oe_window_ms;
}

static void oe_emit_footer(thread *t) {
    oe_io_check(printf(
        "OE_WRK2_STREAM {\"schema_version\":1,\"kind\":\"footer\",\"start_ns\":%" PRIu64
        ",\"end_ns\":%" PRIu64 ",\"windows\":%" PRIu64 ",\"requests\":%" PRIu64
        ",\"bytes\":%" PRIu64 ",\"errors\":",
        t->oe_start_ns, t->oe_end_ns, t->oe_window_seq,
        t->complete - t->oe_start_complete, t->bytes - t->oe_start_bytes));
    oe_print_errors(t->oe_start_errors, t->errors);
    oe_io_check(printf(",\"terminal_exclusions\":%" PRIu64 ",\"histogram_overflows\":%" PRIu64 ",\"bins_us\":",
        t->oe_terminal_exclusions - t->oe_terminal_at_ready, t->oe_overflow_total));
    oe_print_bins(t->latency_histogram);
    oe_io_check(printf(",\"export_duration_ns\":%" PRIu64 "}\n", t->oe_export_duration_ns));
    if (fflush(stdout) != 0) {
        fprintf(stderr, "OE_WRK2_WINDOW flush failed\n");
        exit(1);
    }
}

/* Upstream record stays first. Mirror disagreement aborts; shared rejection is counted. */
static void oe_record_expected(thread *t, int64_t timing) {
    bool recorded = hdr_record_value(t->latency_histogram, timing);
    bool mirrored;
    if (!(oe_window_ms && t->oe_window_start_ns)) return;
    mirrored = hdr_record_value(t->oe_window_histogram, timing);
    if (recorded != mirrored) {
        fprintf(stderr, "OE_WRK2_WINDOW mirror record diverged\n");
        exit(1);
    }
    if (!recorded) {
        t->oe_window_overflows++;
        t->oe_overflow_total++;
    }
}

'''
    source = replace(source, "int main(int argc, char **argv) {\n", helpers + "int main(int argc, char **argv) {\n")
    source = replace(source,
        "    if (parse_args(&cfg, &url, &parts, headers, argc, argv)) {\n        usage();\n        exit(1);\n    }\n",
        "    if (parse_args(&cfg, &url, &parts, headers, argc, argv)) {\n        usage();\n        exit(1);\n    }\n"
        "    oe_load_window_ms();\n")
    source = replace(source,
        "            cfg.pipeline = script_verify_request(t->L);\n"
        "            cfg.dynamic = !script_is_static(t->L);\n",
        "            cfg.pipeline = script_verify_request(t->L);\n"
        "            cfg.dynamic = !script_is_static(t->L);\n"
        "            if (oe_window_ms && (cfg.threads != 1 || cfg.pipeline != 1 || !cfg.record_all_responses)) {\n"
        "                fprintf(stderr, \"OE_WRK2_WINDOW_MS refused: threads=%\" PRIu64\n"
        "                    \" pipeline=%\" PRIu64 \" record_all_responses=%d\\n\",\n"
        "                    cfg.threads, cfg.pipeline, cfg.record_all_responses ? 1 : 0);\n"
        "                exit(1);\n"
        "            }\n")
    source = replace(source,
        "               t->errors.status - t->oe_start_errors.status, t->errors.timeout - t->oe_start_errors.timeout);\n"
        "    }\n"
        "    return 0;\n",
        "               t->errors.status - t->oe_start_errors.status, t->errors.timeout - t->oe_start_errors.timeout);\n"
        "    }\n"
        "    if (oe_window_ms && threads[0].oe_start_ns)\n"
        "        oe_emit_footer(&threads[0]);\n"
        "    return 0;\n")
    source = replace(source,
        "    hdr_init(1, MAX_LATENCY, 3, &thread->latency_histogram);\n"
        "    hdr_init(1, MAX_LATENCY, 3, &thread->u_latency_histogram);\n",
        "    hdr_init(1, MAX_LATENCY, 3, &thread->latency_histogram);\n"
        "    hdr_init(1, MAX_LATENCY, 3, &thread->u_latency_histogram);\n"
        "    if (oe_window_ms && hdr_init(1, MAX_LATENCY, 3, &thread->oe_window_histogram)) {\n"
        "        fprintf(stderr, \"OE_WRK2_WINDOW histogram init failed\\n\");\n"
        "        exit(1);\n"
        "    }\n")
    source = replace(source, "    aeMain(loop);\n    thread->oe_end_ns = oe_monotonic_ns();\n",
        "    aeMain(loop);\n    thread->oe_end_ns = oe_monotonic_ns();\n"
        "    if (oe_window_ms && thread->oe_window_start_ns) oe_emit_tail(thread);\n")
    source = replace(source,
        "    thread->oe_start_errors = thread->errors;\n    thread->start    = time_us();\n",
        "    thread->oe_start_errors = thread->errors;\n"
        "    if (oe_window_ms) {\n"
        "        thread->oe_window_start_ns = thread->oe_start_ns;\n"
        "        thread->oe_window_complete = thread->complete;\n"
        "        thread->oe_window_bytes = thread->bytes;\n"
        "        thread->oe_window_errors = thread->errors;\n"
        "        thread->oe_window_terminal = 0;\n"
        "        thread->oe_window_overflows = 0;\n"
        "        thread->oe_terminal_at_ready = thread->oe_terminal_exclusions;\n"
        "        oe_emit_ready(thread);\n"
        "        if (aeCreateTimeEvent(loop, (long long)oe_window_ms, oe_window_tick, thread, NULL) == AE_ERR) {\n"
        "            fprintf(stderr, \"OE_WRK2_WINDOW timer failed\\n\");\n"
        "            exit(1);\n"
        "        }\n"
        "    }\n"
        "    thread->start    = time_us();\n")
    source = replace(source, "        hdr_record_value(thread->latency_histogram, expected_latency_timing);\n",
        "        oe_record_expected(thread, expected_latency_timing);\n")
    source = replace(source,
        "        thread->oe_terminal_exclusions++;\n        aeStop(thread->loop);\n",
        "        thread->oe_terminal_exclusions++;\n"
        "        if (oe_window_ms && thread->oe_window_start_ns) thread->oe_window_terminal++;\n"
        "        aeStop(thread->loop);\n")
    return header, source


def build(archive, *, windowed):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise SystemExit("Upstream archive hash differs; use the recorded LF export")
    cache_name = f"wrk2-window-{time.time_ns()}" if windowed else f"wrk2-aligned-{time.time_ns()}"
    directory = ROOT / "cache" / cache_name
    directory.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if sum(row.size for row in members) > 32 * 1024 * 1024:
            raise ValueError("Archive too large")
        for row in members:
            target = (directory / row.name).resolve()
            if not target.is_relative_to(directory.resolve()) or not (row.isfile() or row.isdir()):
                raise ValueError("Invalid upstream archive member")
        for row in members:
            target = directory / row.name
            if row.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(row) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(row.mode & 0o777)
    tree = directory / "wrk2-44a94c17"
    paths = [tree / "src/wrk.h", tree / "src/wrk.c"]
    original = [path.read_text() for path in paths]
    changed = instrument_windows(*original) if windowed else instrument(*original)
    patch = ""
    for path, before, after in zip(paths, original, changed):
        patch += "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                           fromfile="a/" + path.relative_to(tree).as_posix(),
                                           tofile="b/" + path.relative_to(tree).as_posix()))
        path.write_text(after)
    state = ROOT / "state"
    state.mkdir(exist_ok=True)
    patch_name = "wrk2-window-measurement.patch" if windowed else "wrk2-measurement.patch"
    log_name = "wrk2-window-build.log" if windowed else "wrk2-build.log"
    manifest_name = "wrk2-window-build.json" if windowed else "wrk2-build.json"
    (state / patch_name).write_text(patch)
    with (state / log_name).open("w") as log:
        subprocess.run(["make", "-C", str(tree), "-j2"], stdout=log, stderr=subprocess.STDOUT, check=True)
    binary = tree / "wrk"
    evidence = {"schema_version": 1, "upstream_commit": COMMIT, "upstream_archive_sha256": ARCHIVE_SHA,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(), "binary": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "scope": "one client thread only; calibration/end snapshots, request scheduling and HDR algorithm unchanged",
        "compiler": subprocess.check_output(["gcc", "--version"], text=True),
        "patched_sources": {path.relative_to(tree).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}
    if windowed:
        evidence["measurement_protocol"] = "oe_wrk2_windows_v1"
        evidence["window_ms"] = 5000
    (state / manifest_name).write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", nargs="?", default="/tmp/wrk2-upstream-44a94c17-lf.tar.gz")
    parser.add_argument("--windowed", action="store_true")
    args = parser.parse_args(argv)
    if platform.system() != "Linux":
        raise SystemExit("wrk2 build requires Linux")
    evidence = build(Path(args.archive), windowed=args.windowed)
    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
