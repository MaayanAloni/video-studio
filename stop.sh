#!/bin/zsh
lsof -ti tcp:7860 -sTCP:LISTEN | xargs kill 2>/dev/null
lsof -ti tcp:8188 -sTCP:LISTEN | xargs kill 2>/dev/null
echo "stopped"
