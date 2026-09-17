#!/bin/zsh
# Tail Video Studio logs.  ./logs.sh [engine|server|all]   (default: all)
cd "$(dirname "$0")"
clean() {  # collapse tqdm carriage-returns and strip ANSI colours
  perl -pe 's/\r(?!\n)/\n/g' | sed -E 's/\x1B\[[0-9;]*[mGKHF]//g' | grep -vE '^\s*$'
}
case "${1:-all}" in
  engine) tail -n 60 -f logs/engine.log | clean ;;
  server) tail -n 60 -f logs/server.log | clean ;;
  *) tail -n 30 -f logs/engine.log logs/server.log | clean ;;
esac
