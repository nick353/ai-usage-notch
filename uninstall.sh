#!/bin/bash
# AI Usage Notch を Hammerspoon の設定から外す（録画ノッチには触らない）。
set -eu
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HS=/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs
INIT="$HOME/.hammerspoon/init.lua"

if [ -f "$INIT" ]; then
  TMP="$(mktemp)"
  awk '/^-- >>> ai-usage-notch >>>/{skip=1} !skip{print} /^-- <<< ai-usage-notch <<</{skip=0}' "$INIT" > "$TMP"
  mv "$TMP" "$INIT"
fi

if pgrep -x Hammerspoon >/dev/null; then
  ( "$HS" -t 5 -c 'hs.reload()' >/dev/null 2>&1 & )
  sleep 2
fi
echo "AI Usage Notch を外しました"
