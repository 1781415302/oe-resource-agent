"""Create a deterministic source package; record checks and validation scope."""
import ast
import gzip
import hashlib
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.3"
EXCLUDED = {"state", "results", "__pycache__", ".git", "dist", "cache"}


def main():
    files = sorted(path for path in ROOT.rglob("*") if path.is_file()
                   and not any(part in EXCLUDED for part in path.relative_to(ROOT).parts))
    for path in files:
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))
        if path.suffix in (".py", ".c", ".h", ".sh") and b"\r\n" in path.read_bytes():
            raise ValueError(f"Linux file has CRLF: {path}")
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
    output = ROOT.parent / "output"
    output.mkdir(exist_ok=True)
    archive = output / f"oe-resource-agent-{VERSION}.tar.gz"
    with archive.open("wb") as dest, gzip.GzipFile(filename="", mode="wb", fileobj=dest, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as tar:
            for path in files:
                info = tar.gettarinfo(str(path), "oe-resource-agent/" + path.relative_to(ROOT).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o755 if path.suffix == ".sh" or path.name == "agent" else 0o644
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    record = {"version": VERSION, "archive": str(archive), "sha256": sha, "source_files": len(files),
              "static_checks": ["Python grammar 3.9", "Linux line endings", "JSON parse"],
              "guest_validation": "see validation/ if supplied; static packaging is not runtime proof",
              "sources": {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}
    (output / f"oe-resource-agent-{VERSION}-package.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    (output / (archive.name + ".sha256")).write_text(sha + "  " + archive.name + "\n", encoding="ascii")
    print(json.dumps({key: value for key, value in record.items() if key != "sources"}, indent=2))


if __name__ == "__main__":
    main()
