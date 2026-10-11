#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo_root/.venv-lora"
export MAMBA_ROOT_PREFIX="${MAMBA_ROOT_PREFIX:-${TMPDIR:-/tmp}/msdr-mamba-root}"

if ! command -v micromamba >/dev/null 2>&1; then
  echo 'micromamba is required: https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html' >&2
  exit 1
fi

if [[ -x "$runtime/bin/python" ]]; then
  micromamba install -y -p "$runtime" -f "$repo_root/environment-lora.yml"
else
  micromamba create -y -p "$runtime" -f "$repo_root/environment-lora.yml"
fi
"$runtime/bin/python" -m pip install -e "$repo_root"
"$runtime/bin/python" "$repo_root/scripts/validate_lora_iq.py"
echo "LoRa runtime ready. Start msdr with: $runtime/bin/msdr"
