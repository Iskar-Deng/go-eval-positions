#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-$root/.venv/bin/python}"
libs="$("$python" -c 'import sysconfig; from pathlib import Path; print(":".join(str(p) for p in sorted((Path(sysconfig.get_paths()["purelib"])/"nvidia").glob("*/lib"))))')"
export LD_LIBRARY_PATH="$libs${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mode="${1:-analysis}"
if [[ $# -gt 0 ]]; then shift; fi
exec "${KATAGO_BIN:-$root/.local/katago/bin/katago}" "$mode" \
  -model "${KATAGO_MODEL:-$root/.local/katago/models/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz}" \
  -config "${KATAGO_ANALYSIS_CONFIG:-$root/configs/katago-analysis.cfg}" "$@"
