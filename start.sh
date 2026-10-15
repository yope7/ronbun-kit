#!/bin/sh
# ronbun を起動する。必要なものが揃っているかを先に確かめる。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。"; exit 1
fi
if ! python3 -c "import fitz" 2>/dev/null; then
  echo "PyMuPDF を入れます（pip install -r requirements.txt）"
  python3 -m pip install -r requirements.txt || exit 1
fi
if [ -z "$CLAUDE_BIN" ] && ! command -v claude >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/claude" ]; then
  echo "claude コマンドが見つかりません。Claude Code を入れてログインしてください:"
  echo "  https://docs.claude.com/en/docs/claude-code"
  exit 1
fi

PORT="${PORT:-8800}"
URL="http://127.0.0.1:$PORT/site/"
(
  sleep 1
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
) >/dev/null 2>&1 &
exec python3 tools/server.py
