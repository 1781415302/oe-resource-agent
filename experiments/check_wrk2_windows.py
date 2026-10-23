"""Run one owned nginx and windowed wrk2 client and check the export protocol."""
import argparse
import hashlib
import json
import os
import platform
import select
import shutil
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 18088
OFFERED_RPS = 2000
CONNECTIONS = 32
PAGE_BYTES = 4096
WINDOW_MS = 5000
SERVICE_CPUS = [0]
CLIENT_CPUS = [1]
CONTROLLER_CPUS = [2]
MAX_WINDOW_NS = 8_000_000_000
STREAM_PREFIX = "OE_WRK2_STREAM "
ALIGNED_PREFIX = "OE_WRK2_ALIGNED "
ERROR_KEYS = ("connect", "read", "write", "status", "timeout")


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _cpu_allowed_list(pid):
    text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    prefix = "Cpus_allowed_list:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise RuntimeError(f"PID {pid} 缺少 Cpus_allowed_list")


def _marker(lines, prefix):
    selected = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(selected) != 1:
        raise ValueError(f"需要恰好一条 {prefix.strip()} 记录，实际 {len(selected)}")
    return json.loads(selected[0])


def _bin_issues(bins, label):
    issues = []
    if not isinstance(bins, list):
        return [f"{label} bins_us 不是数组"]
    previous = None
    for item in bins:
        if not isinstance(item, (list, tuple)) or len(item) != 2 or not _integer(item[0]) or not _integer(item[1]):
            issues.append(f"{label} 桶格式无效")
            break
        value, count = item
        if value < 0 or count <= 0 or (previous is not None and value <= previous):
            issues.append(f"{label} 桶必须为非负最高等价值、正计数且严格升序")
            break
        previous = value
    return issues


def _counter_issues(record, label):
    issues = []
    for key in ("requests", "bytes", "terminal_exclusions", "histogram_overflows"):
        if not _integer(record.get(key)) or record[key] < 0:
            issues.append(f"{label} {key} 不是非负整数")
    errors = record.get("errors")
    if not isinstance(errors, dict) or set(errors) != set(ERROR_KEYS):
        issues.append(f"{label} errors 字段不完整")
        return issues
    for key in ERROR_KEYS:
        if not _integer(errors[key]) or errors[key] < 0:
            issues.append(f"{label} errors.{key} 不是非负整数")
    return issues


def validate_records(stream_rows, aligned):
    """Check raw Ready/Window/Footer conservation without trusting a saved verdict."""
    issues = []
    if not stream_rows or stream_rows[0].get("kind") != "ready":
        issues.append("缺少 Ready")
        return issues
    if stream_rows[-1].get("kind") != "footer":
        issues.append("缺少 Footer")
        return issues
    if any(row.get("kind") != "window" for row in stream_rows[1:-1]):
        issues.append("流记录顺序必须是 Ready、窗口、Footer")
        return issues
    ready, footer = stream_rows[0], stream_rows[-1]
    windows = stream_rows[1:-1]
    if ready.get("schema_version") != 1 or footer.get("schema_version") != 1:
        issues.append("schema_version 必须为 1")
    if ready.get("window_ms") != WINDOW_MS or ready.get("offered_rps") != OFFERED_RPS:
        issues.append("Ready 窗口或计划速率与阶段一不一致")
    if ready.get("connections") != CONNECTIONS or ready.get("client_threads") != 1 or ready.get("pipeline") != 1:
        issues.append("Ready 连接、线程或 pipeline 不符合单客户端协议")
    if ready.get("histogram") != "wrk2_expected_latency_hdr_3sig":
        issues.append("Ready 直方图标识不正确")
    if not _integer(ready.get("start_ns")) or ready["start_ns"] < 0:
        issues.append("Ready start_ns 无效")
    issues.extend(_counter_issues(footer, "Footer"))
    issues.extend(_bin_issues(footer.get("bins_us"), "Footer"))
    if not _integer(footer.get("start_ns")) or not _integer(footer.get("end_ns")) or not _integer(footer.get("windows")):
        issues.append("Footer 时间或窗口数无效")
        return issues
    if not _integer(footer.get("export_duration_ns")) or footer["export_duration_ns"] <= 0:
        issues.append("Footer.export_duration_ns 必须为正整数且不含 Footer 自身")
    if footer["start_ns"] != ready.get("start_ns") or footer["end_ns"] <= footer["start_ns"]:
        issues.append("Footer 区间没有接上 Ready")
    if footer["windows"] != len(windows) or not windows:
        issues.append("Footer.windows 与窗口条数不一致")
        return issues
    merged = {}
    totals = {key: 0 for key in ("requests", "bytes", "terminal_exclusions", "histogram_overflows")}
    error_totals = {key: 0 for key in ERROR_KEYS}
    previous_end = footer["start_ns"]
    for index, window in enumerate(windows, 1):
        label = f"window {index}"
        if window.get("schema_version") != 1 or window.get("kind") != "window":
            issues.append(f"{label} 类型无效")
            continue
        if window.get("seq") != index:
            issues.append(f"{label} seq 不连续")
        if not isinstance(window.get("final"), bool):
            issues.append(f"{label} final 不是布尔值")
        elif window["final"] and index != len(windows):
            issues.append("final 窗口只能是最后一窗")
        if not _integer(window.get("start_ns")) or not _integer(window.get("end_ns")):
            issues.append(f"{label} 时间无效")
            continue
        if window["start_ns"] != previous_end or window["end_ns"] <= window["start_ns"]:
            issues.append(f"{label} 没有与前一边界精确相接")
        duration_ns = window["end_ns"] - window["start_ns"]
        if duration_ns <= 0 or duration_ns > MAX_WINDOW_NS:
            issues.append(f"{label} 时长必须为正且不超过 8 秒")
        issues.extend(_counter_issues(window, label))
        issues.extend(_bin_issues(window.get("bins_us"), label))
        if issues:
            continue
        counted = sum(item[1] for item in window["bins_us"])
        if window["requests"] != counted + window["terminal_exclusions"] + window["histogram_overflows"]:
            issues.append(f"{label} 请求数与桶、终止排除、溢出不守恒")
        if window["histogram_overflows"] != 0:
            issues.append(f"{label} 直方图溢出")
        for key in totals:
            totals[key] += window[key]
        for key in ERROR_KEYS:
            error_totals[key] += window["errors"][key]
        for value, count in window["bins_us"]:
            merged[value] = merged.get(value, 0) + count
        previous_end = window["end_ns"]
    if previous_end != footer["end_ns"]:
        issues.append("最后一窗没有结束在 Footer end_ns")
    if any(totals[key] != footer.get(key) for key in totals):
        issues.append("Footer 计数与窗口合计不一致")
    if isinstance(footer.get("errors"), dict) and any(error_totals[key] != footer["errors"].get(key) for key in ERROR_KEYS):
        issues.append("Footer 错误计数与窗口合计不一致")
    merged_bins = [[value, merged[value]] for value in sorted(merged)]
    if merged_bins != footer.get("bins_us"):
        issues.append("Footer 主直方图与窗口桶合并不一致")
    if footer.get("histogram_overflows") != 0:
        issues.append("Footer 直方图溢出")
    footer_bins = footer.get("bins_us")
    counted = None
    if isinstance(footer_bins, list) and all(isinstance(item, (list, tuple)) and len(item) == 2 and _integer(item[1]) for item in footer_bins):
        counted = sum(item[1] for item in footer_bins)
    if counted is None or not _integer(footer.get("requests")) or not _integer(footer.get("terminal_exclusions")) or not _integer(footer.get("histogram_overflows")):
        issues.append("Footer 请求数与桶、终止排除、溢出不守恒")
    elif footer["requests"] != counted + footer["terminal_exclusions"] + footer["histogram_overflows"]:
        issues.append("Footer 请求数与桶、终止排除、溢出不守恒")
    if not counted:
        issues.append("没有延迟样本，无法核对窗口分布")
    for key in ("start_ns", "end_ns", "requests", "bytes", "terminal_exclusions", "connections", "rate"):
        if not _integer(aligned.get(key)) or aligned[key] < 0:
            issues.append(f"旧对齐字段 {key} 无效")
    if aligned.get("start_ns") != footer.get("start_ns") or aligned.get("end_ns") != footer.get("end_ns"):
        issues.append("旧对齐区间与 Footer 不一致")
    if aligned.get("requests") != footer.get("requests") or aligned.get("bytes") != footer.get("bytes"):
        issues.append("旧对齐请求或字节与 Footer 不一致")
    if aligned.get("terminal_exclusions") != footer.get("terminal_exclusions"):
        issues.append("旧对齐终止排除与 Footer 不一致")
    if aligned.get("rate") != OFFERED_RPS or aligned.get("connections") != CONNECTIONS:
        issues.append("旧对齐速率或连接数不正确")
    if not _integer(aligned.get("latency_samples")) or aligned.get("latency_samples") != counted:
        issues.append("旧对齐延迟样本与主直方图桶计数不一致")
    aligned_errors = aligned.get("errors")
    if not isinstance(aligned_errors, dict) or any(aligned_errors.get(key) != footer.get("errors", {}).get(key) for key in ERROR_KEYS):
        issues.append("旧对齐错误计数与 Footer 不一致")
    return issues


def _agree(stream, rows):
    issues = []
    ready = stream["ready"]
    footer = stream["footer"]
    if ready is None or footer is None or not stream["complete"] or stream["issues"]:
        issues.append("measurement 解析未形成完整流: " + "; ".join(stream["issues"]))
        return issues
    if ready["start_ns"] != rows[0]["start_ns"] or footer["requests"] != rows[-1]["requests"]:
        issues.append("measurement 解析与原始流不一致")
    if len(stream["windows"]) != len(rows) - 2:
        issues.append("measurement 窗口条数与原始流不一致")
    return issues


def _stop_owned(manager, evidence):
    if manager is None:
        return
    try:
        evidence["cleanup_errors"] = manager.stop_all()
    except Exception as exc:
        evidence["cleanup_errors"] = [str(exc)]


def run_check(duration_seconds):
    bench = ROOT.parent / "euler-bench"
    sys.path[:0] = [str(ROOT), str(bench)]
    run_key = f"check-{os.getpid()}-{time.time_ns()}"
    directory = ROOT / "results" / f"wrk2-windows-{run_key}"
    evidence = {"schema_version": 1, "passed": False, "run_key": run_key, "directory": directory.name,
                "duration_seconds": duration_seconds, "offered_rps": OFFERED_RPS, "issues": [],
                "cleanup_errors": [], "final_sched_ext": None}
    manager = None
    read_sched = None
    parent_window = os.environ.get("OE_WRK2_WINDOW_MS")

    def fail(message):
        evidence["issues"].append(message)
        raise RuntimeError(message)

    try:
        from eulerbench.common import boot_id
        from eulerbench.environment import ensure_port_free
        from eulerbench.metrics import process_stats
        from eulerbench.processes import ProcessManager
        from eulerbench.runner import _prepare_nginx, _wait_ready
        from oeagent.backend import sched_state
        from oeagent.measurement import parse_client_stream, summarize_windows
        from oeagent.runtime import ResourceLock
        read_sched = sched_state
        if sched_state() != "disabled":
            fail("已有 SCX 调度器，拒绝启动且不会卸载它")
        if not {0, 1, 2}.issubset(os.sched_getaffinity(0)):
            fail("CPU0/1/2 不可用")
        manifest = json.loads((ROOT / "state" / "wrk2-window-build.json").read_text(encoding="utf-8"))
        if manifest.get("measurement_protocol") != "oe_wrk2_windows_v1" or manifest.get("window_ms") != WINDOW_MS:
            fail("窗口版 wrk2 构建清单缺少协议或 window_ms")
        binary = Path(manifest["binary"])
        if not binary.is_file() or hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["binary_sha256"]:
            fail("窗口版 wrk2 二进制与清单不一致")
        env_path = Path("/usr/bin/env")
        if not env_path.is_file() or not os.access(env_path, os.X_OK):
            fail("缺少 /usr/bin/env，不能只给本次 wrk2 设置窗口变量")
        nginx = shutil.which("nginx")
        if not nginx:
            fail("缺少 nginx")
        directory.mkdir(parents=True)
        identity = process_stats(os.getpid())
        current_boot = boot_id()
        if not identity or not _integer(identity.get("start_ticks")) or not current_boot:
            fail("无法读取控制进程身份")

        def interrupted(signum, frame):
            raise KeyboardInterrupt(f"Received signal {signum}")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        with ResourceLock():
            if sched_state() != "disabled":
                fail("锁内发现 SCX 调度器，拒绝启动且不会卸载它")
            os.sched_setaffinity(0, set(CONTROLLER_CPUS))
            if os.sched_getaffinity(0) != set(CONTROLLER_CPUS):
                fail("控制线程没有绑定到 CPU2")
            _write_json(directory / "owner.json", {"run_key": run_key, "pid": os.getpid(),
                                                   "start_ticks": identity["start_ticks"], "boot_id": current_boot})
            ensure_port_free(PORT)
            profile = {"page_bytes": PAGE_BYTES, "service_cpus": SERVICE_CPUS, "port": PORT}
            config, page = _prepare_nginx(directory, profile)
            events_path = directory / "driver-events.jsonl"
            client_log = directory / "client.log"
            with events_path.open("w", encoding="utf-8") as events:
                manager = ProcessManager(events, directory)
                try:
                    server = manager.start("server", [nginx, "-p", str(directory) + "/", "-c", str(config)],
                                           SERVICE_CPUS, directory / "nginx.log")
                    url = f"http://127.0.0.1:{PORT}/index.html"
                    _wait_ready(server, url, page)
                    if _cpu_allowed_list(server.pid) != str(SERVICE_CPUS[0]):
                        fail("nginx 实际允许 CPU 不是 service CPU")
                    # taskset execs this argv; env applies only to the wrk2 image.
                    client = manager.start("client", [str(env_path), f"OE_WRK2_WINDOW_MS={WINDOW_MS}", str(binary),
                                                       "-t1", f"-c{CONNECTIONS}", "-T3s", f"-d{duration_seconds}s",
                                                       f"-R{OFFERED_RPS}", url],
                                           CLIENT_CPUS, client_log, pipe=True)
                    if os.environ.get("OE_WRK2_WINDOW_MS") != parent_window:
                        fail("父进程环境被修改")
                    if _cpu_allowed_list(client.pid) != str(CLIENT_CPUS[0]):
                        fail("wrk2 实际允许 CPU 不是 client CPU")
                    deadline = time.monotonic() + duration_seconds + 20
                    with client_log.open("w", encoding="utf-8") as log:
                        while client.poll() is None:
                            if time.monotonic() > deadline:
                                fail("窗口版 wrk2 超时")
                            if server.poll() is not None:
                                fail("nginx 在客户端结束前退出")
                            ready, _, _ = select.select([client.stdout], [], [], 0.5)
                            if not ready:
                                continue
                            line = client.stdout.readline()
                            if not line:
                                break
                            log.write(line)
                            log.flush()
                        remainder = client.stdout.read() if client.stdout else ""
                        if remainder:
                            log.write(remainder)
                            log.flush()
                    returncode = client.wait(timeout=10)
                    if returncode != 0:
                        fail(f"wrk2 退出码为 {returncode}")
                    if server.poll() is not None:
                        fail("nginx 在校验前退出")
                    raw = client_log.read_bytes()
                    if raw and not raw.endswith(b"\n"):
                        fail("客户端日志以半行结束")
                    lines = raw.decode("utf-8", errors="replace").splitlines()
                    stream_rows = []
                    for line in lines:
                        if line.startswith(STREAM_PREFIX):
                            stream_rows.append(json.loads(line[len(STREAM_PREFIX):]))
                    aligned = _marker(lines, ALIGNED_PREFIX)
                    evidence["issues"].extend(validate_records(stream_rows, aligned))
                    parsed = parse_client_stream(client_log)
                    evidence["issues"].extend(_agree(parsed, stream_rows))
                    if not evidence["issues"] and parsed["ready"] is not None:
                        stats = summarize_windows(parsed["windows"], parsed["ready"]["offered_rps"])
                        counted = sum(count for row in parsed["windows"] for _, count in row["bins_us"])
                        if stats["requests"] != parsed["footer"]["requests"] or stats["latency_samples"] != counted:
                            evidence["issues"].append("summarize_windows 与 Footer 不一致")
                    if evidence["issues"]:
                        raise RuntimeError("窗口协议校验失败")
                    evidence["passed"] = True
                    evidence["windows"] = len(stream_rows) - 2
                    evidence["requests"] = stream_rows[-1]["requests"]
                finally:
                    _stop_owned(manager, evidence)
                    manager = None
    except (Exception, KeyboardInterrupt) as exc:
        evidence["passed"] = False
        text = f"{type(exc).__name__}: {exc}"
        if all(str(exc) not in item and text not in item for item in evidence["issues"]):
            evidence["issues"].append(text)
    finally:
        _stop_owned(manager, evidence)
        if evidence["cleanup_errors"]:
            evidence["passed"] = False
            evidence["issues"].extend(item for item in evidence["cleanup_errors"] if item not in evidence["issues"])
        if read_sched is not None:
            try:
                evidence["final_sched_ext"] = read_sched()
            except Exception as exc:
                evidence["passed"] = False
                evidence["issues"].append(f"无法读取调度状态: {exc}")
            if evidence["final_sched_ext"] != "disabled":
                evidence["passed"] = False
                evidence["issues"].append(f"结束时 sched_ext 为 {evidence['final_sched_ext']}")
        if directory.is_dir():
            _write_json(directory / "check.json", evidence)
        print(json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False))
    return 0 if evidence["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check one windowed wrk2 client against local nginx.")
    parser.add_argument("--duration-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    if args.duration_seconds <= 0:
        parser.error("--duration-seconds must be positive")
    if platform.system() != "Linux":
        print(json.dumps({"passed": False, "issues": ["window check requires Linux"]}, ensure_ascii=False))
        return 1
    return run_check(args.duration_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
