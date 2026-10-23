"""Inspect cached code without importing/remediating the Agent package first."""
import hashlib
import importlib.util
import json
import marshal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
rows = []
for source in sorted((ROOT / "oeagent").glob("*.py")):
    cache = Path(importlib.util.cache_from_source(str(source)))
    data = cache.read_bytes() if cache.is_file() else b""
    flags = int.from_bytes(data[4:8], "little") if len(data) >= 16 else None
    matches = len(data) >= 16 and marshal.loads(data[16:]) == compile(source.read_bytes(), str(source), "exec")
    rows.append({"file": source.relative_to(ROOT).as_posix(), "cached": bool(data),
                 "cached_code_matches_source": matches, "flags": flags,
                 "checked_hash_matches": flags == 3 and data[8:16] == importlib.util.source_hash(source.read_bytes()),
                 "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
result = {"schema_version": 1, "python": sys.version, "all_cached_code_matches": all(row["cached_code_matches_source"] for row in rows),
          "all_checked_hash": all(row["checked_hash_matches"] for row in rows), "files": rows}
target = ROOT / "state" / (sys.argv[1] if len(sys.argv) > 1 else "python-cache-audit.json")
target.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps({key: value for key, value in result.items() if key != "files"} | {"different": [row["file"] for row in rows if not row["cached_code_matches_source"]]}, indent=2))
