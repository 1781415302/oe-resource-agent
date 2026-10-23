"""Run the authorized bounded guest checks and archive their owned evidence."""
import hashlib
import json
import platform
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    sys.path.insert(0, str(ROOT))
    from oeagent import __version__
    from oeagent.backend import sched_state
    if sched_state() != "disabled":
        raise RuntimeError("Run after the active experiment has finished")
    directory = ROOT / "results" / ("acceptance-" + str(time.time_ns()))
    directory.mkdir(parents=True)
    stages = []
    for name, arguments, timeout in (
        ("unit", ["-m", "unittest", "discover", "-s", "tests", "-v"], 30),
        ("native", ["experiments/check_native.py"], 30),
        ("tool-session", ["experiments/check_control_session.py"], 30),
        ("feedback-session", ["experiments/check_feedback_session.py"], 30),
        ("wakeup", ["experiments/check_wakeup.py"], 60),
        ("owned-stop", ["experiments/check_owned_stop.py"], 75),
        ("cgroup-plugin", ["experiments/check_cgroup_plugin.py"], 30),
        ("ebpf-hooks", ["experiments/check_ebpf_hooks.py"], 30),
        ("cache", ["tools/audit_python_cache.py"], 15),
    ):
        with (directory / (name + ".log")).open("w") as stream:
            started = time.monotonic()
            result = subprocess.run([sys.executable, *arguments], cwd=ROOT, stdout=stream,
                stderr=subprocess.STDOUT, timeout=timeout)
        row = {"name": name, "returncode": result.returncode, "elapsed_seconds": time.monotonic() - started}
        stages.append(row)
        print(json.dumps(row), flush=True)
        if result.returncode:
            raise RuntimeError("Validation failed: " + name)
    summary = {"schema_version": 1, "version": __version__, "python": platform.python_version(),
        "status": "passed", "directory": str(directory), "stages": stages, "final_sched_ext": sched_state(),
        "scope": "functional regression; performance requires the separately frozen experiment"}
    (directory / "validation.json").write_text(json.dumps(summary, indent=2) + "\n")
    names = ("build", "installed-sources", "python-cache-audit", "native-validation", "tool-session-validation",
        "feedback-session-validation", "wakeup-validation", "owned-stop-validation",
        "cgroup-plugin-validation", "ebpf-hooks-validation")
    files = list(directory.iterdir())
    folders = []
    for name in names:
        path = ROOT / "state" / (name + ".json")
        files.append(path)
        value = json.loads(path.read_text())
        if name.endswith("validation"):
            if value["status"] != "passed":
                raise RuntimeError("Persisted validation did not pass: " + name)
            if "directory" in value:
                folders.append(Path(value["directory"]).resolve())
    # Retain the two failed wakeup diagnostics preceding the fix as well.
    folders += list((ROOT / "results").glob("wakeup-validation-*"))
    for folder in set(folders):
        if not folder.resolve().is_relative_to(ROOT / "results"):
            raise RuntimeError("Evidence directory outside owned results")
        files += [path for path in folder.rglob("*") if path.is_file()]
    archive = Path("/tmp") / ("agent-" + __version__ + "-validation.tar.gz")
    with tarfile.open(archive, "w:gz") as bundle:
        for path in sorted(set(files)):
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise RuntimeError("Evidence file outside owned project")
            bundle.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
    summary.update(archive=str(archive), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (ROOT / "state/acceptance-validation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
