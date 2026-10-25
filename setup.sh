#!/usr/bin/env bash
# Darkmoon setup for macOS / Linux
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
$PY -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# Linux: pywebview needs GTK or Qt. On Ubuntu:  sudo apt install python3-gi gir1.2-webkit2-4.1
if command -v ollama >/dev/null; then
  echo "[ok] Ollama found — building the darkmoon model (pulls qwen3.5:9b the first time)"
  ollama pull qwen3.5:9b && ollama create darkmoon -f models/darkmoon.Modelfile && echo "[ok] darkmoon ready — try: ollama run darkmoon"
else
  echo "[ ] Install Ollama: https://ollama.com/download"
fi
command -v grok   >/dev/null && echo "[ok] Grok CLI found (run 'grok login' once)" || echo "[ ] Grok CLI not found (optional)"
command -v claude >/dev/null && echo "[ok] Claude Code found (run 'claude' once to sign in)" || echo "[ ] Claude Code not found (optional)"
echo "Done. Start with ./start.sh"
