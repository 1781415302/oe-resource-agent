"""CLI and persistent newline-delimited JSON tool session."""
import argparse
import contextlib
import json
import os
import platform
import select
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .protocol import ToolError
from .runtime import AgentRuntime, ResourceLock, load_profile, run_agent
from .tools import create_registry, profile_path


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def serve(profile_name):
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise ToolError("ROOT_REQUIRED", "实际控制会话需要 Linux root。")
    directory = Path(__file__).resolve().parents[1] / "results" / f"session-{time.time_ns()}-{os.getpid()}"
    runtime = AgentRuntime(load_profile(profile_path(profile_name)), directory, emit_changes=False)
    registry = create_registry(runtime, runtime.log)
    stopping = threading.Event()
    errors = []
    previous = {}
    def request_stop(signum, frame):
        runtime.stop_requested = True
        stopping.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.signal(sig, request_stop)
    def heartbeat():
        while not stopping.wait(2):
            try:
                runtime.backend.heartbeat()
            except BaseException as exc:
                runtime.log({"event": "session_heartbeat_error", "message": str(exc)})
                runtime.backend.stop()
                stopping.set()
                break
    def automatic():
        try:
            runtime.run(43200)
        except BaseException as exc:
            errors.append(str(exc))
        finally:
            stopping.set()
    with ResourceLock():
        worker = threading.Thread(target=automatic if runtime.feedback else heartbeat, daemon=True)
        worker.start()
        buffered = b""
        eof = False
        discarding = False
        try:
            while not stopping.is_set():
                if b"\n" not in buffered and not eof:
                    if not select.select([sys.stdin], [], [], .5)[0]:
                        continue
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    eof = not chunk
                    buffered += chunk
                if b"\n" in buffered:
                    raw, buffered = buffered.split(b"\n", 1)
                elif eof:
                    if not buffered:
                        break
                    raw, buffered = buffered, b""
                else:
                    if len(buffered) > 65536:
                        discarding, buffered = True, b""
                    continue
                if discarding or len(raw) > 65536:
                    discarding = False
                    emit({"schema_version": 1, "ok": False, "error": {"code": "REQUEST_TOO_LARGE"}})
                    continue
                try:
                    line = raw.decode("utf-8")
                    request = json.loads(line)
                    with contextlib.redirect_stdout(sys.stderr):
                        result = registry.call(request["tool"], request.get("arguments", {}), request.get("id"))
                    emit(result)
                except (ValueError, KeyError, TypeError) as exc:
                    emit({"schema_version": 1, "ok": False, "error": {"code": "INVALID_REQUEST", "message": str(exc)}})
        finally:
            stopping.set()
            runtime.stop_requested = True
            worker.join(timeout=20)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if worker.is_alive():
                runtime.backend.stop()
                raise ToolError("CONTROL_SESSION_STOP_FAILED", "控制线程未在限时内停止。")
            if not runtime.feedback:
                runtime.backend.stop()
                runtime.log({"event": "control_session_closed"})
                runtime.log.close()
        if errors:
            raise ToolError("CONTROL_SESSION_FAILED", "; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description="openEuler 自适应资源管控 Agent")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    tools = sub.add_parser("tools")
    tools.add_argument("action", choices=["list"])
    tool = sub.add_parser("tool")
    tool.add_argument("name")
    tool.add_argument("--input", default="{}")
    run = sub.add_parser("run")
    run.add_argument("--profile", default="nginx-cpu")
    run.add_argument("--duration", type=float, default=60)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--log-dir", type=Path)
    session = sub.add_parser("serve")
    session.add_argument("--profile", default="nginx-cpu")
    skills = sub.add_parser("skills")
    skills.add_argument("action", choices=["list", "run"])
    skills.add_argument("name", nargs="?")
    skills.add_argument("--input", default="{}")
    args = parser.parse_args()
    registry = create_registry()
    try:
        if args.command == "capabilities":
            emit(registry.call("system.capabilities", {}))
        elif args.command == "tools":
            emit({"schema_version": 1, "tools": registry.list()})
        elif args.command == "tool":
            with contextlib.redirect_stdout(sys.stderr):
                result = registry.call(args.name, json.loads(args.input))
            emit(result)
            return 0 if result["ok"] else 1
        elif args.command == "run":
            if not 1 <= args.duration <= 43200:
                raise ToolError("INVALID_DURATION", "运行时长应在1秒到12小时之间。")
            emit(run_agent(profile_path(args.profile), args.duration, args.log_dir, args.dry_run))
        elif args.command == "serve":
            serve(args.profile)
        elif args.command == "skills":
            root = Path(__file__).resolve().parents[1] / "skills"
            manifests = [json.loads(path.read_text()) for path in sorted(root.glob("*/skill.json"))]
            if args.action == "list":
                emit({"schema_version": 1, "skills": manifests})
            else:
                manifest = next((item for item in manifests if item["id"] == args.name), None)
                if not manifest:
                    raise ToolError("SKILL_UNAVAILABLE", "Skill 未注册。")
                arguments = json.loads(args.input)
                from .protocol import validate
                validate(arguments, manifest["inputSchema"])
                with contextlib.redirect_stdout(sys.stderr):
                    result = registry.call(manifest["entry_tool"], arguments)
                emit(result)
                return 0 if result["ok"] else 1
    except (ToolError, ValueError, OSError) as exc:
        emit({"schema_version": 1, "ok": False, "error": {"code": getattr(exc, "code", "EXECUTION_FAILED"), "message": str(exc)}})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
