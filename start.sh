#!/bin/zsh
# Video Studio launcher: starts the GUI server (which supervises the ComfyUI engine) and opens the browser.
cd "$(dirname "$0")"
source .venv/bin/activate
mkdir -p logs
if lsof -ti tcp:7860 >/dev/null; then echo "Already running → http://127.0.0.1:7860"; open http://127.0.0.1:7860; exit 0; fi
nohup python app/server.py >> logs/server.log 2>&1 &
echo "Starting… (engine takes ~20s to warm up)"
for i in {1..30}; do curl -sf http://127.0.0.1:7860/api/status >/dev/null && break; sleep 1; done
open http://127.0.0.1:7860
echo "Video Studio → http://127.0.0.1:7860   (stop with ./stop.sh)"
