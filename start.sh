#!/bin/sh
# ronbun を起動する。必要なものが揃っているかを先に確かめる。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。"; exit 1
fi
# PyMuPDF は .venv に入れる（システムの Python を汚さない）
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
if ! "$PY" -c "import pymupdf" 2>/dev/null; then
  echo "初回のみ: .venv を作って PyMuPDF を入れます"
  [ -x .venv/bin/python ] || python3 -m venv .venv || exit 1
  PY=.venv/bin/python
  "$PY" -m pip install -q -r requirements.txt || exit 1
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
exec "$PY" tools/server.py
