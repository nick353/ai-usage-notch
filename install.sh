#!/bin/bash
# AI Usage Notch（AI トークン消費サイドノッチ）を Hammerspoon に登録する。何度流しても同じ結果になる。
set -eu
cd "$(dirname "$0")"
BASE="$(pwd)"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HS=/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs

chmod +x scripts/collect.py install.sh uninstall.sh doctor.sh

mkdir -p "$HOME/.hammerspoon"
INIT="$HOME/.hammerspoon/init.lua"
touch "$INIT"
TMP="$(mktemp)"
awk '/^-- >>> ai-usage-notch >>>/{skip=1} !skip{print} /^-- <<< ai-usage-notch <<</{skip=0}' "$INIT" > "$TMP"
cat >> "$TMP" <<EOF
-- >>> ai-usage-notch >>>
-- AI Usage Notch（各AIのトークン消費サイドノッチ）。消すときは $BASE/uninstall.sh
AIUsageNotch = dofile("$BASE/hammerspoon/usage_notch.lua")
-- <<< ai-usage-notch <<<
EOF
mv "$TMP" "$INIT"

if pgrep -x Hammerspoon >/dev/null; then
  ( "$HS" -t 5 -c 'hs.reload()' >/dev/null 2>&1 & )
  sleep 3
else
  open -a Hammerspoon
  sleep 4
fi
echo "設定を反映しました: $INIT"
