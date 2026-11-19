#!/bin/bash
# AI Usage Notch の点検 / Health check
set -u
cd "$(dirname "$0")"
HS=/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs
[ -x "$HS" ] || HS="$HOME/Applications/Hammerspoon.app/Contents/Frameworks/hs/hs"
case "$(defaults read -g AppleLanguages 2>/dev/null | sed -n 2p)" in *ja*) JA=1 ;; *) JA= ;; esac
t() { if [ -n "$JA" ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
BAD=0
ok() { printf '✓ %s\n' "$1"; }
ng() { printf '✗ %s\n' "$1"; BAD=$((BAD + 1)); }

[ -x "$HS" ] && ok "$(t 'Hammerspoon が入っている' 'Hammerspoon is installed')" || ng "$(t 'Hammerspoon が無い: brew install --cask hammerspoon' 'Hammerspoon missing: brew install --cask hammerspoon')"
OUT="$(python3 scripts/collect.py 2>/dev/null)"
if [ -n "$OUT" ]; then
  ok "$(t '数字を集める仕組みが動く' 'The collector runs')"
  echo "$OUT" | python3 -c "import json,sys;d=json.load(sys.stdin);print('   '+', '.join(i['label']+('' if i.get('present',True) else ' (hidden)') for i in d['items']))"
else
  ng "$(t '数字を集める仕組みでエラー: python3 scripts/collect.py' 'Collector error: run python3 scripts/collect.py')"
fi
/usr/bin/grep -q "ai-usage-notch" "$HOME/.hammerspoon/init.lua" 2>/dev/null && ok "$(t 'Hammerspoon の設定に入っている' 'Registered in ~/.hammerspoon/init.lua')" || ng "$(t 'Hammerspoon の設定に無い: ./install.sh' 'Not registered: run ./install.sh')"
if pgrep -x Hammerspoon >/dev/null; then
  TMPO="$(mktemp)"; "$HS" -t 5 -c 'return AIUsageNotch and "loaded" or "missing"' >"$TMPO" 2>/dev/null & P=$!
  for _ in 1 2 3 4 5 6 7 8; do sleep 1; kill -0 $P 2>/dev/null || break; done; kill $P 2>/dev/null
  OUT="$(tail -1 "$TMPO")"; rm -f "$TMPO"
  [ "$OUT" = "loaded" ] && ok "$(t '出っぱりが読み込み済み' 'The notch is loaded')" || ng "$(t '出っぱりが読み込まれていない（Hammerspoon の Console を見てください）' 'The notch is not loaded (check the Hammerspoon Console)')"
else
  ng "$(t 'Hammerspoon が動いていない' 'Hammerspoon is not running')"
fi
exit $BAD
