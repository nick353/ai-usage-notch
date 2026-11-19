#!/bin/bash
# AI Usage Notch の点検
set -u
cd "$(dirname "$0")"
HS=/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs
BAD=0
ok() { printf '○ %s\n' "$1"; }
ng() { printf '× %s\n' "$1"; BAD=$((BAD + 1)); }

python3 scripts/collect.py >/dev/null 2>&1 && ok "データ取得スクリプトが正常に動く" || ng "データ取得スクリプトでエラー"
/usr/bin/grep -q "ai-usage-notch" "$HOME/.hammerspoon/init.lua" 2>/dev/null && ok "Hammerspoon の設定に入っている" || ng "Hammerspoon の設定に無い"

if pgrep -x Hammerspoon >/dev/null; then
  OUT="$("$HS" -t 10 -c 'return (AIUsageNotch and "loaded" or "missing") .. " " .. (WorkRecorder and "loaded" or "missing")' 2>/dev/null | tail -1)"
  set -- $OUT
  [ "${1:-}" = "loaded" ] && ok "AI使用量ノッチが読み込み済み" || ng "AI使用量ノッチが読み込まれていない"
  [ "${2:-}" = "loaded" ] && ok "既存の作業録画ノッチも正常に共存している" || ng "既存の作業録画ノッチが見つからない"
else
  ng "Hammerspoon が動いていない"
fi
exit $BAD
