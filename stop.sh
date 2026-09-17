#!/bin/zsh
lsof -ti tcp:7860 | xargs kill 2>/dev/null
lsof -ti tcp:8188 | xargs kill 2>/dev/null
echo "stopped"
