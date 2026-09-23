#!/bin/bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
engine="$project_dir/.local/katago/bin/katago"
model="$project_dir/.local/katago/models/kata1-b18c384nbt-s9996604416-d4316597426.bin.gz"
mode="${1:-gtp}"
if [[ $# -gt 0 ]]; then shift; fi

case "$mode" in
  gtp|analysis) ;;
  *) echo "Usage: $0 [gtp|analysis] [additional KataGo arguments]" >&2; exit 2 ;;
esac

if [[ ! -x "$engine" || ! -f "$model" ]]; then
  echo "Local KataGo executable or model is missing. See docs/katago-local.md." >&2
  exit 1
fi

cd "$project_dir"
exec "$engine" "$mode" -config "$project_dir/configs/katago-$mode.cfg" -model "$model" "$@"
