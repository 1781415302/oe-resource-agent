"""Export the actual registered interfaces; no observation or control actions."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from oeagent.tools import create_registry

if __name__ == "__main__":
    output = ROOT / "schemas/tool-catalog.json"
    output.write_text(json.dumps({"schema_version": 1, "tools": create_registry().list()},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
