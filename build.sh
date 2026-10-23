#!/usr/bin/env bash
set -euo pipefail
agent_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$agent_root/state"
exec > >(tee -a "$agent_root/state/build.log") 2>&1
trap 'echo "Agent后端构建在第 $LINENO 行失败；查看 state/build.log。" >&2' ERR
if [[ $# -gt 1 ]]; then
    echo '用法：bash build.sh [实际内核源码目录]' >&2
    exit 2
fi
python3 "$agent_root/tools/build_native.py" "$@"
