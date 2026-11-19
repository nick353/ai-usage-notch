#!/bin/bash
# AI Usage Notch（AI トークン消費サイドノッチ）を Hammerspoon に登録する。何度流しても同じ結果になる。
set -eu
cd "$(dirname "$0")"
BASE="$(pwd)"
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HS=/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs

chmod +x scripts/collect.py install.sh uninstall.sh doctor.sh

case "$(defaults read -g AppleLanguages 2>/dev/null | sed -n 2p)" in *ja*) JA=1 ;; *) JA= ;; esac
msg() { if [ -n "$JA" ]; then echo "$1"; else echo "$2"; fi; }

if [ ! -d /Applications/Hammerspoon.app ] && [ ! -d "$HOME/Applications/Hammerspoon.app" ]; then
  msg "Hammerspoon が見つかりません。先に入れてください: brew install --cask hammerspoon" \
      "Hammerspoon is not installed. Install it first: brew install --cask hammerspoon"
  exit 1
fi
[ -x "$HS" ] || HS="$HOME/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs"
command -v python3 >/dev/null || { msg "python3 が見つかりません: xcode-select --install" "python3 not found: xcode-select --install"; exit 1; }
[ -f config.json ] || cp config.example.json config.json

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
msg "入れました: $INIT
画面の右端（真ん中より少し下）にマウスを寄せてください。Claude の枠を初めて読むとき、キーチェーンの許可を聞かれたら「常に許可」を押してください。" \
    "Installed: $INIT
Move your mouse to the right edge of the screen, a little below the middle. If macOS asks to let 'security' read the Claude Code keychain item, click Always Allow."
