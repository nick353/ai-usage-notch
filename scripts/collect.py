#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各AIサブスク（Claude / Codex / Gemini / OpenCode）の残り%と消費量を時間軸ごとに高速集計して JSON で返す。
ふだんはこの Mac の中の記録だけを読む。Claude の週・5時間の枠だけは、ほかに記録が無いとき、
Claude Code のログイン情報で Anthropic の使用量の窓口に聞く（config.json の claude_oauth: false で止められる）。
ログイン情報は Anthropic にだけ渡し、保存も表示もしない。"""

import glob
import json
import os
import re
import sqlite3
import subprocess
import urllib.request
import sys
import time
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(HOME, ".cache", "ai-usage-notch", "file-cache.json")
JST = None  # 時刻はこの Mac の地域の時刻で出す


def _detect_lang():
    if "--lang" in sys.argv:
        i = sys.argv.index("--lang")
        if i + 1 < len(sys.argv):
            return "ja" if sys.argv[i + 1].startswith("ja") else "en"
    v = os.environ.get("AIUSAGE_LANG")
    if v:
        return "ja" if v.startswith("ja") else "en"
    try:
        out = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                             capture_output=True, text=True, timeout=3).stdout
        langs = [x.strip(' ",()') for x in out.splitlines()]
        langs = [x for x in langs if x]
        return "ja" if langs and langs[0].startswith("ja") else "en"
    except Exception:
        return "en"


LANG = _detect_lang()


def T(ja, en):
    return ja if LANG == "ja" else en


CLICK = T("｜押すと切替", " | click to switch")
SEP = T("・", " · ")


def usd_note(v):
    return T(f"・目安 ${v:.1f}", f" · ~${v:.1f}") if v and v > 0 else ""


def load_config():
    p = os.path.join(BASE, "config.json")
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
            return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def fmt_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 10_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_short_num(n):
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def fmt_jst(ts):
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%-m/%-d %H:%M")
    except Exception:
        return ""


def parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def rem_status(rem_pct):
    """残り%に応じた色判定（バッテリー残量と同じ直感）"""
    if rem_pct is None:
        return "info"
    if rem_pct <= 20:
        return "danger"
    if rem_pct <= 40:
        return "warn"
    return "ok"


def read_tail_lines(path, max_bytes):
    if not os.path.isfile(path):
        return []
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            if sz > max_bytes:
                f.seek(sz - max_bytes)
                f.readline()
            data = f.read().decode("utf-8", errors="ignore")
        return data.splitlines()
    except Exception:
        return []


def collect_aos_usage(now):
    """~/.config/aos/claude-usage.jsonl から Claude 5h/24h と OpenCode Go 5h/7d/30d を高速集計する。"""
    p = os.path.join(HOME, ".config", "aos", "claude-usage.jsonl")
    lines = read_tail_lines(p, max_bytes=32_000_000)

    dt_now = datetime.fromtimestamp(now, tz=timezone.utc)
    day_prefixes = {
        dt_now.strftime("%Y-%m-%d"),
        (dt_now - timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    go_keywords = ("go_allowance_usd", "grok-", "glm-", "kimi-", "qwen", "minimax", "deepseek")

    claude_5h_tok = 0
    claude_5h_usd = 0.0
    claude_24h_tok = 0
    claude_24h_usd = 0.0
    go_5h = 0.0
    go_7d = 0.0
    go_30d = 0.0
    go_unknown_7d = 0
    seen = set()

    for line in lines:
        if not line:
            continue
        is_recent_day = any(dp in line[:260] for dp in day_prefixes)
        has_go = any(k in line for k in go_keywords)
        if not is_recent_day and not has_go:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if rid:
            if rid in seen:
                continue
            seen.add(rid)
        at = parse_iso(r.get("at"))
        if at is None:
            continue
        age = now - at
        if age < 0 or age > 30 * 86400:
            continue

        go_a = r.get("go_allowance_usd")
        if go_a is not None:
            try:
                val = float(go_a)
                if val >= 0:
                    go_30d += val
                    if age <= 7 * 86400:
                        go_7d += val
                    if age <= 5 * 3600:
                        go_5h += val
            except Exception:
                pass
        elif has_go and age <= 7 * 86400:
            model = str(r.get("model") or "")
            if any(k in model for k in ("grok-", "glm-", "kimi-", "qwen", "minimax", "deepseek")):
                if r.get("ok") or r.get("tokens"):
                    go_unknown_7d += 1

        if age <= 86400:
            model = str(r.get("model") or "")
            actual = r.get("actualModels") or []
            is_claude = (
                "claude" in model.lower()
                or any("claude" in str(m).lower() for m in actual)
                or (r.get("estimated_usd") is not None and not model.startswith("gpt-"))
            )
            if is_claude:
                t = r.get("tokens")
                tok = 0
                if isinstance(t, dict):
                    tok = (
                        int(t.get("input") or 0)
                        + int(t.get("cache_read") or 0)
                        + int(t.get("cache_write") or 0)
                        + int(t.get("output") or 0)
                    )
                usd = r.get("estimated_usd")
                usd_val = float(usd) if isinstance(usd, (int, float)) and usd >= 0 else 0.0
                claude_24h_tok += tok
                claude_24h_usd += usd_val
                if age <= 5 * 3600:
                    claude_5h_tok += tok
                    claude_5h_usd += usd_val

    return {
        "claude_5h_tok": claude_5h_tok,
        "claude_5h_usd": claude_5h_usd,
        "claude_24h_tok": claude_24h_tok,
        "claude_24h_usd": claude_24h_usd,
        "go_5h": go_5h,
        "go_7d": go_7d,
        "go_30d": go_30d,
        "go_unknown_7d": go_unknown_7d,
    }


def collect_claude_projects(now, cache):
    """~/.claude/projects 配下で24時間以内に更新された会話ログから 5h / 24h トークンを集計する。"""
    base = os.path.join(HOME, ".claude", "projects")
    if not os.path.isdir(base):
        return 0, 0
    cutoff_24h = now - 86400
    cutoff_5h = now - 5 * 3600
    proj_cache = cache.setdefault("claude_proj", {})
    tot_5h = 0
    tot_24h = 0
    active_keys = set()

    for root, _, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
            except Exception:
                continue
            if st.st_mtime < cutoff_24h:
                continue
            active_keys.add(fp)
            sig = f"{int(st.st_mtime)}:{st.st_size}"
            ent = proj_cache.get(fp)
            if isinstance(ent, dict) and ent.get("sig") == sig:
                for ts_val, tok_val in ent.get("rows", []):
                    if ts_val >= cutoff_24h:
                        tot_24h += tok_val
                    if ts_val >= cutoff_5h:
                        tot_5h += tok_val
                continue

            rows = []
            seen_req = set()
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if '"usage"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        ts = parse_iso(d.get("timestamp"))
                        if ts is None or ts < cutoff_24h:
                            continue
                        req_id = d.get("requestId")
                        if req_id:
                            if req_id in seen_req:
                                continue
                            seen_req.add(req_id)
                        msg = d.get("message")
                        u = msg.get("usage") if isinstance(msg, dict) else None
                        if isinstance(u, dict):
                            tok = (
                                int(u.get("input_tokens") or 0)
                                + int(u.get("cache_read_input_tokens") or 0)
                                + int(u.get("cache_creation_input_tokens") or 0)
                                + int(u.get("output_tokens") or 0)
                            )
                            if tok > 0:
                                rows.append([int(ts), tok])
                                tot_24h += tok
                                if ts >= cutoff_5h:
                                    tot_5h += tok
            except Exception:
                pass
            proj_cache[fp] = {"sig": sig, "rows": rows}

    for k in list(proj_cache.keys()):
        if k not in active_keys:
            del proj_cache[k]
    return tot_5h, tot_24h


def _claude_token(now):
    """Claude Code のログイン情報から使える鍵を1つ返す（無い・期限切れなら None）。保存も表示もしない。"""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    raw = ""
    fp = os.path.join(HOME, ".claude", ".credentials.json")
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            raw = ""
    if not raw and sys.platform == "darwin":
        try:
            # 初回だけ macOS が「キーチェーンへのアクセスを許可しますか？」と聞く（「常に許可」で次から出ない）
            raw = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            raw = ""
    if not raw:
        return None
    try:
        o = (json.loads(raw) or {}).get("claudeAiOauth") or {}
        exp = o.get("expiresAt")
        if isinstance(exp, (int, float)) and exp / 1000.0 < now:
            return None  # 期限切れ。Claude Code を次に使ったときに自動で新しくなる
        return o.get("accessToken") or None
    except Exception:
        return None


def collect_claude_oauth(now, cache, cfg):
    """Anthropic の使用量の窓口から Claude の5時間・週の枠（使った%）を聞く。5分に1回まで。"""
    if cfg.get("claude_oauth") is False:
        return None
    c = cache.get("claude_oauth") or {}
    interval = max(120, int(cfg.get("claude_oauth_interval_sec") or 300))
    if c.get("at") and now - c["at"] < interval:
        return c.get("data")
    if c.get("retry_after") and now < c["retry_after"]:
        return c.get("data")
    token = _claude_token(now)
    if not token:
        cache["claude_oauth"] = {**c, "retry_after": now + 1800}
        return c.get("data")
    req = urllib.request.Request(
        os.environ.get("AIUSAGE_OAUTH_URL") or "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "claude-cli/2.1.0 (external, cli)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:
        cache["claude_oauth"] = {**c, "retry_after": now + 600}
        return c.get("data")

    def win(key):
        w = body.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
            return None
        return {"used": max(0.0, min(100.0, float(w["utilization"]))), "resets": parse_iso(w.get("resets_at"))}

    data = {"five": win("five_hour"), "week": win("seven_day")}
    cache["claude_oauth"] = {"at": now, "data": data}
    return data


def collect_claude(now, aos, cache, cfg=None):
    week_path = os.path.join(HOME, ".config", "aos", "claude-week.json")
    limit_path = os.path.join(HOME, ".config", "aos", "claude-limit.json")
    hist_path = os.path.join(HOME, ".config", "aos", "claude-week-history.jsonl")

    week_used = None
    week_resets = None
    updated_at = None
    if os.path.isfile(week_path):
        try:
            with open(week_path, encoding="utf-8") as f:
                w = json.load(f)
            u = w.get("used")
            if isinstance(u, (int, float)):
                week_used = round(max(0.0, min(1.0, float(u))) * 100)
            week_resets = parse_iso(w.get("resetsAt"))
            updated_at = parse_iso(w.get("at"))
        except Exception:
            pass

    # 5時間枠の公式記録があれば拾う
    five_used = None
    five_resets = None
    oauth = None
    if week_used is None or not updated_at or now - updated_at > 6 * 3600:
        oauth = collect_claude_oauth(now, cache, cfg or {})
    if oauth and oauth.get("week"):
        week_used = round(oauth["week"]["used"])
        week_resets = oauth["week"]["resets"]
        updated_at = now
    for hl in reversed(read_tail_lines(hist_path, max_bytes=32_000)):
        if "fiveLast" not in hl:
            continue
        try:
            hd = json.loads(hl)
            fl = hd.get("fiveLast")
            if isinstance(fl, (int, float)):
                fr = parse_iso(hd.get("fiveLastResetsAt"))
                if not fr or fr > now:
                    five_used = round(max(0.0, min(1.0, float(fl))) * 100)
                    five_resets = fr
                break
        except Exception:
            pass

    if oauth and oauth.get("five"):
        five_used = round(oauth["five"]["used"])
        five_resets = oauth["five"]["resets"]

    limit_active = None
    if os.path.isfile(limit_path):
        try:
            with open(limit_path, encoding="utf-8") as f:
                lim = json.load(f)
            until = parse_iso(lim.get("until"))
            at = parse_iso(lim.get("at"))
            if lim.get("kind") == "signed_out":
                limit_active = T("サインアウト中", "Signed out")
            elif until and until > now:
                limit_active = T(f"上限中（{fmt_jst(until)} まで）", f"Limit reached (until {fmt_jst(until)})")
            elif at and (now - at) < 6 * 3600 and not until:
                limit_active = T("上限中", "Limit reached")
        except Exception:
            pass

    chat_5h, chat_24h = collect_claude_projects(now, cache)
    tok_5h = max(aos["claude_5h_tok"], chat_5h)
    tok_24h = max(aos["claude_24h_tok"], chat_24h)
    rec_str = T(f"記録 {fmt_jst(updated_at)}", f"recorded {fmt_jst(updated_at)}") if updated_at else ""

    views = []

    # 1) 公式の5時間枠が取れていれば先頭に置く（一番小さい制限時間軸）
    if five_used is not None:
        rem5 = max(0, 100 - five_used)
        rst5 = T(f"（{fmt_jst(five_resets)} に戻る）", f" (resets {fmt_jst(five_resets)})") if five_resets else ""
        views.append({
            "key": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": rem5,
            "center": f"{rem5}%",
            "status": rem_status(rem5),
            "tip1": T(f"Claude  5時間 残り {rem5}%{rst5}", f"Claude  5h: {rem5}% left{rst5}"),
            "tip2": T(f"5時間消費 {fmt_tokens(tok_5h)}", f"5h used {fmt_tokens(tok_5h)} tokens") + usd_note(aos['claude_5h_usd']) + CLICK,
        })

    # 2) 週間枠（公式）
    if limit_active:
        views.append({
            "key": "week",
            "sub": T("週間", "Week"),
            "mode": "percent",
            "percent": 0,
            "center": "0%",
            "status": "danger",
            "tip1": f"Claude  {limit_active}",
            "tip2": T(f"24時間 {fmt_tokens(tok_24h)}", f"24h {fmt_tokens(tok_24h)} tokens") + usd_note(aos['claude_24h_usd']) + CLICK,
        })
    elif week_used is not None:
        rem_w = max(0, 100 - week_used)
        rst_w = T(f"（{fmt_jst(week_resets)} に戻る）", f" (resets {fmt_jst(week_resets)})") if week_resets else ""
        parts = [T(f"24時間 {fmt_tokens(tok_24h)}", f"24h {fmt_tokens(tok_24h)} tokens")]
        if aos["claude_24h_usd"] > 0:
            parts.append(T(f"目安 ${aos['claude_24h_usd']:.1f}", f"~${aos['claude_24h_usd']:.1f}"))
        if rec_str:
            parts.append(rec_str)
        views.append({
            "key": "week",
            "sub": T("週間", "Week"),
            "mode": "percent",
            "percent": rem_w,
            "center": f"{rem_w}%",
            "status": rem_status(rem_w),
            "tip1": T(f"Claude  週間 残り {rem_w}%{rst_w}", f"Claude  Week: {rem_w}% left{rst_w}"),
            "tip2": SEP.join(parts) + CLICK,
        })

    # 3) 公式5時間%が無い場合は、5時間消費量を切替ビューに置く
    if five_used is None:
        views.append({
            "key": "5h_tok",
            "sub": T("5h消費", "5h used"),
            "mode": "tokens",
            "percent": None,
            "center": fmt_tokens(tok_5h),
            "status": "info" if tok_5h > 0 else "none",
            "tip1": T(f"Claude  直近5時間の消費 {fmt_tokens(tok_5h)} トークン", f"Claude  last 5h: {fmt_tokens(tok_5h)} tokens"),
            "tip2": T("公式の5時間%は未取得", "official 5h % not available") + usd_note(aos['claude_5h_usd']) + CLICK,
        })

    # 4) 24時間消費量
    views.append({
        "key": "24h_tok",
        "sub": T("24h消費", "24h used"),
        "mode": "tokens",
        "percent": None,
        "center": fmt_tokens(tok_24h),
        "status": "info" if tok_24h > 0 else "none",
        "tip1": T(f"Claude  24時間の消費 {fmt_tokens(tok_24h)} トークン", f"Claude  last 24h: {fmt_tokens(tok_24h)} tokens"),
        "tip2": T("24時間の消費量", "24h usage") + usd_note(aos['claude_24h_usd']) + CLICK,
    })

    return {"id": "claude", "label": "Claude", "views": views}


def parse_codex_tail(fp, tail_bytes=96_000):
    lines = read_tail_lines(fp, max_bytes=tail_bytes)
    last_rl = None
    last_ts = None
    last_tok = 0
    for line in reversed(lines):
        if "token_count" not in line and "rate_limits" not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        p = d.get("payload")
        if not isinstance(p, dict):
            continue
        if last_rl is None:
            rl = p.get("rate_limits")
            if isinstance(rl, dict) and rl.get("primary"):
                last_rl = rl
                last_ts = parse_iso(d.get("timestamp"))
        if last_tok == 0:
            info = p.get("info")
            if isinstance(info, dict):
                tu = info.get("total_token_usage")
                if isinstance(tu, dict):
                    tt = int(tu.get("total_tokens") or 0)
                    if tt > 0:
                        last_tok = tt
        if last_rl is not None and last_tok > 0:
            break
    return last_rl, last_ts, last_tok


def collect_codex(now, cache):
    dt_now = datetime.fromtimestamp(now, tz=JST)
    files = []
    for d_off in (0, 1):
        d = dt_now - timedelta(days=d_off)
        pat = os.path.join(HOME, ".codex", "sessions", d.strftime("%Y"), d.strftime("%m"), d.strftime("%d"), "*.jsonl")
        files.extend(glob.glob(pat))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    codex_cache = cache.setdefault("codex", {})
    latest_rl = None
    latest_ts = None
    cutoff_24h = now - 86400
    cutoff_5h = now - 5 * 3600
    tok_5h = 0
    tok_24h = 0
    active_keys = set()

    for fp in files:
        try:
            st = os.stat(fp)
        except Exception:
            continue
        if st.st_mtime < cutoff_24h and latest_rl is not None:
            break
        active_keys.add(fp)
        sig = f"{int(st.st_mtime)}:{st.st_size}"
        ent = codex_cache.get(fp)
        if isinstance(ent, dict) and ent.get("sig") == sig:
            rl, ts, tok = ent.get("rl"), ent.get("ts"), int(ent.get("tok") or 0)
        else:
            rl, ts, tok = parse_codex_tail(fp, 96_000)
            if rl is None and tok == 0 and st.st_size > 96_000:
                rl, ts, tok = parse_codex_tail(fp, 512_000)
            codex_cache[fp] = {"sig": sig, "rl": rl, "ts": ts, "tok": tok}

        if latest_rl is None and rl is not None:
            latest_rl = rl
            latest_ts = ts
        if st.st_mtime >= cutoff_24h:
            tok_24h += tok
        if st.st_mtime >= cutoff_5h:
            tok_5h += tok

    for k in list(codex_cache.keys()):
        if k not in active_keys:
            del codex_cache[k]

    views = []
    credits_bal = None
    has_balance = False

    if latest_rl:
        credits = latest_rl.get("credits") or {}
        if credits.get("has_credits") and credits.get("balance") is not None:
            try:
                credits_bal = float(credits["balance"])
                has_balance = credits_bal > 0
            except Exception:
                pass

        # primary / secondary を時間軸の小さい順に並べる
        windows = []
        for wobj in (latest_rl.get("primary"), latest_rl.get("secondary")):
            if isinstance(wobj, dict) and isinstance(wobj.get("used_percent"), (int, float)):
                wmin = int(wobj.get("window_minutes") or 10080)
                windows.append((wmin, wobj))
        windows.sort(key=lambda x: x[0])

        bal_note = T(f"残クレジット {credits_bal:,.0f}", f"credits left {credits_bal:,.0f}") if has_balance else ""
        rec_note = T(f"記録 {fmt_jst(latest_ts)}", f"recorded {fmt_jst(latest_ts)}") if latest_ts else ""

        for wmin, wobj in windows:
            used_pct = round(max(0.0, min(100.0, float(wobj["used_percent"]))))
            rem_pct = max(0, 100 - used_pct)
            rst = wobj.get("resets_at")
            rst_str = T(f"（{fmt_jst(rst)} に戻る）", f" (resets {fmt_jst(rst)})") if rst else ""
            if wmin >= 1440:
                sub, title = T("週間", "Week"), T("週間", "Week")
            elif wmin >= 60:
                h = int(wmin // 60)
                sub, title = T(f"{h}時間", f"{h}h"), T(f"{h}時間", f"{h}h")
            else:
                sub, title = T("枠", "Limit"), T("枠", "Limit")

            st = rem_status(rem_pct)
            if rem_pct == 0 and has_balance:
                st = "warn"
            parts = []
            if bal_note:
                parts.append(bal_note)
            parts.append(T(f"24時間 {fmt_tokens(tok_24h)}", f"24h {fmt_tokens(tok_24h)} tokens"))
            if rec_note:
                parts.append(rec_note)
            views.append({
                "key": f"win_{wmin}",
                "sub": sub,
                "mode": "percent",
                "percent": rem_pct,
                "center": f"{rem_pct}%",
                "status": st,
                "tip1": T(f"Codex  {title} 残り {rem_pct}%{rst_str}", f"Codex  {title}: {rem_pct}% left{rst_str}"),
                "tip2": SEP.join(parts) + CLICK,
            })

    # 直近5時間の消費トークン
    views.append({
        "key": "5h_tok",
        "sub": T("5h消費", "5h used"),
        "mode": "tokens",
        "percent": None,
        "center": fmt_tokens(tok_5h),
        "status": "info" if tok_5h > 0 else "none",
        "tip1": T(f"Codex  直近5時間の消費 {fmt_tokens(tok_5h)} トークン", f"Codex  last 5h: {fmt_tokens(tok_5h)} tokens"),
        "tip2": T(f"24時間 {fmt_tokens(tok_24h)}（このプランは週間枠のみ）", f"24h {fmt_tokens(tok_24h)} tokens (weekly limit only on this plan)") + CLICK,
    })

    # 追加クレジット残高があるときは切替ビューに足す
    if has_balance and credits_bal is not None:
        views.append({
            "key": "credits",
            "sub": T("残クレ", "Credits"),
            "mode": "tokens",
            "percent": None,
            "center": fmt_short_num(credits_bal),
            "status": "ok",
            "tip1": T(f"Codex  追加クレジット 残り {credits_bal:,.0f}", f"Codex  extra credits left {credits_bal:,.0f}"),
            "tip2": T("週間枠を使い切ってもこの残高から稼働", "used after the weekly limit runs out") + CLICK,
        })

    return {"id": "codex", "label": "Codex", "views": views}


def gemini_model_rates(model):
    """Antigravity / Gemini モデルの100万トークンあたり目安単価（入力, キャッシュ読込, 出力 USD）"""
    m = str(model or "").lower()
    if "opus" in m:
        return (5.0, 0.50, 25.0)
    if "sonnet" in m:
        return (3.0, 0.30, 15.0)
    if "flash" in m:
        return (0.30, 0.03, 2.50)
    if "hydrogen" in m or "argon" in m:
        return (1.25, 0.125, 10.0)
    # neon / pro 系の既定（Gemini 4 Neon 公式単価: 入力$3・キャッシュ$0.15・出力$15）
    return (3.0, 0.15, 15.0)


def _gemini_transcript_paths():
    """Antigravity（CLI・T3 Code 経由の両方）の会話ログ transcript.jsonl を重複なしで返す。"""
    pats = [
        os.path.join(HOME, ".gemini", "antigravity*", "brain", "*", ".system_generated", "logs", "transcript.jsonl"),
        os.path.join(HOME, ".t3", "userdata", "providers", "antigravity", "*", "antigravity-acp", "brain", "*", ".system_generated", "logs", "transcript.jsonl"),
    ]
    seen = set()
    out = []
    for pat in pats:
        for fp in glob.glob(pat):
            rp = os.path.realpath(fp)
            if rp not in seen:
                seen.add(rp)
                out.append(fp)
    return out


def _gemini_log_paths():
    """Antigravity（CLI・T3 Code 経由の両方）の実行ログを重複なしで返す。"""
    pats = [
        os.path.join(HOME, ".gemini", "antigravity*", "log", "*.log"),
        os.path.join(HOME, ".t3", "userdata", "providers", "antigravity", "*", "antigravity-acp", "log", "*.log"),
    ]
    seen = set()
    out = []
    for pat in pats:
        for fp in glob.glob(pat):
            rp = os.path.realpath(fp)
            if rp not in seen:
                seen.add(rp)
                out.append(fp)
    return out


_GEMINI_429_RE = re.compile(
    r"^E(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+.*Individual quota reached.*Resets in ((?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?)"
)


def _parse_duration_sec(s):
    """'2h24m35s' のような残り時間表記を秒数に変換する。"""
    if not s:
        return None
    total = 0
    for val, unit in re.findall(r"(\d+)([dhms])", s):
        n = int(val)
        if unit == "d":
            total += n * 86400
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        elif unit == "s":
            total += n
    return total if total > 0 else None


def _collect_gemini_quota_resets(now, cache):
    """Antigravity の実行ログから 429 枠切れのリセット確定時刻を拾う。
    戻り値: (active_reset_ts or None, sorted known_reset_ts list)"""
    cutoff = now - 36 * 3600
    log_cache = cache.setdefault("gemini_429_v1", {})
    active_keys = set()
    pairs = []

    for fp in _gemini_log_paths():
        try:
            st = os.stat(fp)
        except Exception:
            continue
        if st.st_mtime < cutoff:
            continue
        active_keys.add(fp)
        sig = f"{int(st.st_mtime)}:{st.st_size}"
        ent = log_cache.get(fp)
        if isinstance(ent, dict) and ent.get("sig") == sig:
            rows = ent.get("rows") or []
        else:
            rows = []
            year = datetime.fromtimestamp(st.st_mtime).year
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "Individual quota reached" not in line or "Resets in" not in line:
                            continue
                        m = _GEMINI_429_RE.match(line)
                        if not m:
                            continue
                        mo, dy, hh, mm, ss = (int(m.group(i)) for i in range(1, 6))
                        dur = _parse_duration_sec(m.group(6))
                        if not dur:
                            continue
                        try:
                            dt = datetime(year, mo, dy, hh, mm, ss)
                            if dt.timestamp() > now + 86400:
                                dt = datetime(year - 1, mo, dy, hh, mm, ss)
                            log_ts = dt.timestamp()
                            rows.append([log_ts, log_ts + dur])
                        except Exception:
                            continue
            except Exception:
                pass
            log_cache[fp] = {"sig": sig, "rows": rows}
        for log_ts, reset_ts in rows:
            pairs.append((float(log_ts), float(reset_ts)))

    for k in list(log_cache.keys()):
        if k not in active_keys:
            del log_cache[k]

    if not pairs:
        return None, []

    pairs.sort(key=lambda x: x[0])
    active_reset = None
    for _, reset_ts in pairs:
        if reset_ts > now:
            active_reset = reset_ts

    # 同じ枠の複数429（数秒の揺れ）を1つの確定リセット時刻にまとめる
    raw_resets = sorted(r for _, r in pairs)
    known = []
    for r in raw_resets:
        if known and abs(r - known[-1]) <= 600:
            known[-1] = r
        else:
            known.append(r)
    return active_reset, known


def _compute_gemini_5h_window(events, now, known_resets):
    """Gemini の5時間枠を固定5時間ブロック（使い始めた瞬間から5時間後にリセット）で集計する。
    429ログから得た確定リセット時刻があれば終了時刻をそこに合わせる。"""
    five_h = 5 * 3600
    events_sorted = sorted(events, key=lambda x: x[0])
    win_start = None
    win_end = None
    u5 = 0.0
    t5 = 0

    for ts, usd, tok in events_sorted:
        if win_start is None or ts >= win_end:
            win_start = ts
            win_end = ts + five_h
            u5 = 0.0
            t5 = 0
            for r in known_resets:
                if win_start < r <= win_end + 600:
                    win_end = r
                    break
        u5 += usd
        t5 += tok

    if win_start is None or now >= win_end:
        return {"active": False, "win_start": None, "win_end": None, "usd": 0.0, "tok": 0}
    return {"active": True, "win_start": win_start, "win_end": win_end, "usd": u5, "tok": t5}


def collect_gemini(now, cfg, cache=None):
    if cache is None:
        cache = {}
    cap_5h = float(cfg.get("gemini_5h_usd") or 25.0)
    cap_w = float(cfg.get("gemini_week_usd") or 65.0)
    cap_m = float(cfg.get("gemini_month_usd") or 200.0)

    cutoff_7d = now - 7 * 86400
    cutoff_30d = now - 30 * 86400
    t7 = t30 = 0
    u7 = u30 = 0.0
    events = []

    # 1. Antigravity の会話ログ（CLI・T3 Code 経由の定額枠消費分）
    tx_cache = cache.setdefault("gemini_tx_v1", {})
    active_keys = set()
    for fp in _gemini_transcript_paths():
        try:
            st = os.stat(fp)
        except Exception:
            continue
        if st.st_mtime < cutoff_30d:
            continue
        active_keys.add(fp)
        sig = f"{int(st.st_mtime)}:{st.st_size}"
        ent = tx_cache.get(fp)
        if isinstance(ent, dict) and ent.get("sig") == sig:
            rows = ent.get("rows") or []
        else:
            rows = []
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if "_tokens" not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        it = int(d.get("input_tokens") or 0)
                        ct = int(d.get("cache_read_tokens") or 0)
                        ot = int(d.get("output_tokens") or 0)
                        tok = it + ct + ot
                        if tok <= 0:
                            continue
                        ts = parse_iso(d.get("created_at"))
                        if ts is None or ts < cutoff_30d:
                            continue
                        ri, rc, ro = gemini_model_rates(d.get("model"))
                        usd = (it * ri + ct * rc + ot * ro) / 1_000_000.0
                        rows.append([ts, usd, tok])
            except Exception:
                pass
            tx_cache[fp] = {"sig": sig, "rows": rows}
        for ts, usd, tok in rows:
            if ts < cutoff_30d:
                continue
            t30 += tok
            u30 += usd
            if ts >= cutoff_7d:
                t7 += tok
                u7 += usd
            events.append((ts, usd, tok))

    for k in list(tx_cache.keys()):
        if k not in active_keys:
            del tx_cache[k]

    active_reset, known_resets = _collect_gemini_quota_resets(now, cache)
    win = _compute_gemini_5h_window(events, now, known_resets)
    u5 = win["usd"]
    t5 = win["tok"]
    win_start = win["win_start"]
    win_end = win["win_end"]

    # 2. AOS の Gemini API 記録（トークン表示に加算）
    gp = os.path.join(HOME, ".config", "aos", "gemini-usage.jsonl")
    for line in read_tail_lines(gp, max_bytes=2_000_000):
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = parse_iso(r.get("at"))
        if ts is None or ts < cutoff_30d:
            continue
        t = r.get("tokens")
        if isinstance(t, dict):
            it = int(t.get("input") or 0)
            ct = int(t.get("cache_read") or 0)
            ot = int(t.get("output") or 0)
            tok = it + ct + ot
            t30 += tok
            if ts >= cutoff_7d:
                t7 += tok
            if win_start is not None and ts >= win_start:
                t5 += tok

    # 5時間ビュー: 429 枠切れ中なら公式どおり 0%＋戻る時刻、通常時は固定5時間枠で計算
    if active_reset is not None:
        rst_s = T(f"（{fmt_jst(active_reset)} に戻る）", f" (resets {fmt_jst(active_reset)})")
        view_5h = {
            "key": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": 0,
            "center": "0%",
            "status": "danger",
            "tip1": T(f"Gemini  5時間 上限中{rst_s}", f"Gemini  5h: Limit reached{rst_s}"),
            "tip2": T(f"消費 {fmt_tokens(t5)} トークン（目安 ${u5:.2f}）", f"{fmt_tokens(t5)} tokens used (est. ${u5:.2f})") + CLICK,
        }
    elif not win["active"]:
        view_5h = {
            "key": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": 100,
            "center": "100%",
            "status": "ok",
            "tip1": T(f"Gemini  5時間 残り 100%（リセット済み・目安 $0.00 / ${cap_5h:.0f}）", f"Gemini  5h: 100% left (reset · est. $0.00 / ${cap_5h:.0f})"),
            "tip2": T("消費 0 トークン（config.json で目安を補正可）", "0 tokens used (tune caps in config.json)") + CLICK,
        }
    else:
        used_pct = min(100.0, (u5 / cap_5h) * 100.0) if cap_5h > 0 else 0.0
        rem_5h = max(0, round(100.0 - used_pct))
        rst_s = T(f"{fmt_jst(win_end)} に戻る・", f"resets {fmt_jst(win_end)} · ") if win_end else ""
        view_5h = {
            "key": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": rem_5h,
            "center": f"{rem_5h}%",
            "status": rem_status(rem_5h),
            "tip1": T(f"Gemini  5時間 残り 約{rem_5h}%（{rst_s}目安 ${u5:.2f} / ${cap_5h:.0f}）", f"Gemini  5h: ~{rem_5h}% left ({rst_s}est. ${u5:.2f} / ${cap_5h:.0f})"),
            "tip2": T(f"消費 {fmt_tokens(t5)} トークン（config.json で目安を補正可）", f"{fmt_tokens(t5)} tokens used (tune caps in config.json)") + CLICK,
        }

    def make_view(key, sub, title, usd, cap, tok):
        used_pct = min(100.0, (usd / cap) * 100.0) if cap > 0 else 0.0
        rem = max(0, round(100.0 - used_pct))
        return {
            "key": key,
            "sub": sub,
            "mode": "percent",
            "percent": rem,
            "center": f"{rem}%",
            "status": rem_status(rem),
            "tip1": T(f"Gemini  {title} 残り 約{rem}%（目安 ${usd:.2f} / ${cap:.0f}）", f"Gemini  {title}: ~{rem}% left (est. ${usd:.2f} / ${cap:.0f})"),
            "tip2": T(f"消費 {fmt_tokens(tok)} トークン（config.json で目安を補正可）", f"{fmt_tokens(tok)} tokens used (tune caps in config.json)") + CLICK,
        }

    # 一番小さい時間軸（5時間 → 週間 → 月間）の順に並べる
    views = [
        view_5h,
        make_view("week", T("週間", "Week"), T("週間", "Week"), u7, cap_w, t7),
        make_view("month", T("月間", "Month"), T("月間", "Month"), u30, cap_m, t30),
    ]
    return {"id": "gemini", "label": "Gemini", "views": views}

def collect_opencode_db(now):
    dbp = os.path.join(HOME, ".local", "share", "opencode", "opencode.db")
    if not os.path.isfile(dbp):
        return 0.0, 0.0, 0.0
    try:
        c5_ms = int((now - 5 * 3600) * 1000)
        c7_ms = int((now - 7 * 86400) * 1000)
        c30_ms = int((now - 30 * 86400) * 1000)
        con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        cur = con.cursor()
        rows = cur.execute(
            "SELECT time_created, data FROM message WHERE time_created >= ?",
            (c30_ms,),
        ).fetchall()
        con.close()
        u5 = u7 = u30 = 0.0
        for tc, raw in rows:
            if not raw:
                continue
            try:
                d = json.loads(raw)
                c = d.get("cost")
                if isinstance(c, (int, float)) and c > 0:
                    val = float(c)
                    u30 += val
                    if tc >= c7_ms:
                        u7 += val
                    if tc >= c5_ms:
                        u5 += val
            except Exception:
                pass
        return u5, u7, u30
    except Exception:
        return 0.0, 0.0, 0.0


def collect_opencode(now, aos, cfg):
    cap_5h = float(cfg.get("opencode_5h_usd") or 7.0)
    cap_w = float(cfg.get("opencode_week_usd") or 17.5)
    cap_m = float(cfg.get("opencode_month_usd") or 35.0)

    state_path = os.path.join(HOME, ".config", "aos", "council-state.json")
    down_until = None
    down_limit = None
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                st = json.load(f)
            u = st.get("goDownUntil")
            if isinstance(u, (int, float)) and (u / 1000.0) > now:
                down_until = u / 1000.0
                down_limit = str(st.get("goLimit") or "")
        except Exception:
            pass

    db5, db7, db30 = collect_opencode_db(now)
    usd_5h = aos["go_5h"] + db5
    usd_w = aos["go_7d"] + db7
    usd_m = aos["go_30d"] + db30

    def make_view(key, sub, title, usd, cap, hit_down):
        if hit_down and down_until:
            rem = 0
            st = "danger"
            tip1 = T(f"OpenCode Go  {title} 残り 0%（{fmt_jst(down_until)} まで停止）", f"OpenCode Go  {title}: 0% left (paused until {fmt_jst(down_until)})")
        else:
            used_pct = min(100.0, (usd / cap) * 100) if cap > 0 else 0.0
            rem = max(0, round(100.0 - used_pct))
            st = rem_status(rem)
            tip1 = T(f"OpenCode Go  {title} 残り {rem}%（消費 ${usd:.2f} / ${cap:.1f}）", f"OpenCode Go  {title}: {rem}% left (${usd:.2f} / ${cap:.1f} used)")
        return {
            "key": key,
            "sub": sub,
            "mode": "percent",
            "percent": rem,
            "center": f"{rem}%",
            "status": st,
            "tip1": tip1,
            "tip2": T(f"5h ${usd_5h:.2f}・週 ${usd_w:.2f}・月 ${usd_m:.2f}", f"5h ${usd_5h:.2f} · week ${usd_w:.2f} · month ${usd_m:.2f}") + CLICK,
        }

    # 一番小さい時間軸（5時間 → 週間 → 月間）の順に並べる
    views = [
        make_view("5h", T("5時間", "5h"), T("5時間", "5h"), usd_5h, cap_5h, down_until is not None),
        make_view("week", T("週間", "Week"), T("週間", "Week"), usd_w, cap_w, down_until is not None and "week" in (down_limit or "").lower()),
        make_view("month", T("月間", "Month"), T("月間", "Month"), usd_m, cap_m, down_until is not None and "month" in (down_limit or "").lower()),
    ]
    return {"id": "opencode", "label": "OpenCode", "views": views}


def collect_custom(cfg):
    """config.json の "custom" に足した自分の項目。コマンドが出す数字（残り%）か JSON を丸にする。
    JSON の形: {"percent": 42, "center": "42%", "sub": "週", "tip1": "...", "tip2": "..."} か、その一覧（押すと切り替わる）。"""
    out = []
    seen = set()
    for i, c in enumerate(cfg.get("custom") or []):
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or f"Custom {i + 1}")[:14]
        cid = "custom:" + label
        if cid in seen:
            cid = f"{cid}:{i}"
        seen.add(cid)
        cmd = c.get("command")
        views = []
        if isinstance(cmd, str) and cmd.strip():
            try:
                r = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True, text=True,
                                   timeout=float(c.get("timeout") or 8))
                txt = (r.stdout or "").strip()
                raw = None
                try:
                    raw = json.loads(txt)
                except Exception:
                    raw = None
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    raw = {"percent": raw}
                elif raw is None and txt:
                    try:
                        raw = {"percent": float(txt.split()[0].rstrip("%"))}
                    except Exception:
                        raw = {"center": txt[:5], "tip2": txt[:120]}
                for j, v in enumerate(raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []):
                    if not isinstance(v, dict):
                        continue
                    pct = v.get("percent")
                    pct = max(0, min(100, round(float(pct)))) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None
                    views.append({
                        "key": str(v.get("key") or f"v{j}"),
                        "sub": str(v.get("sub") or "")[:8],
                        "mode": "percent" if pct is not None else "tokens",
                        "percent": pct,
                        "center": str(v.get("center") or (f"{pct}%" if pct is not None else "--"))[:5],
                        "status": v.get("status") or (rem_status(pct) if pct is not None else "info"),
                        "tip1": str(v.get("tip1") or (f"{label}  " + (T(f"残り {pct}%", f"{pct}% left") if pct is not None else ""))),
                        "tip2": str(v.get("tip2") or T("自分で足した項目", "Your own item")) + (CLICK if isinstance(raw, list) and len(raw) > 1 else ""),
                    })
                if r.returncode != 0 and not views:
                    views.append({"key": "err", "sub": "", "mode": "percent", "percent": None, "center": "!",
                                  "status": "danger", "tip1": f"{label}  " + T("コマンドが失敗しました", "Command failed"),
                                  "tip2": (r.stderr or "").strip()[:120]})
            except subprocess.TimeoutExpired:
                views.append({"key": "err", "sub": "", "mode": "percent", "percent": None, "center": "…",
                              "status": "warn", "tip1": f"{label}  " + T("時間切れ", "Timed out"), "tip2": cmd[:120]})
            except Exception as e:
                views.append({"key": "err", "sub": "", "mode": "percent", "percent": None, "center": "!",
                              "status": "danger", "tip1": f"{label}  " + T("エラー", "Error"), "tip2": str(e)[:120]})
        if not views:
            views.append({"key": "none", "sub": "", "mode": "percent", "percent": None, "center": "--", "status": "none",
                          "tip1": label, "tip2": T("コマンドが何も出しませんでした", "The command printed nothing")})
        out.append({"id": cid, "label": label, "views": views, "custom": True})
    return out


def main():
    now = time.time()
    cfg = load_config()
    cache = load_cache()
    aos = collect_aos_usage(now)
    items = [
        collect_claude(now, aos, cache, cfg),
        collect_codex(now, cache),
        collect_gemini(now, cfg, cache),
        collect_opencode(now, aos, cfg),
    ] + collect_custom(cfg)
    save_cache(cache)
    present = {
        "claude": os.path.isdir(os.path.join(HOME, ".claude")),
        "codex": os.path.isdir(os.path.join(HOME, ".codex")),
        "gemini": bool(glob.glob(os.path.join(HOME, ".gemini", "antigravity*", "brain")))
        or bool(glob.glob(os.path.join(HOME, ".t3", "userdata", "providers", "antigravity", "*", "antigravity-acp", "brain")))
        or os.path.isfile(os.path.join(HOME, ".config", "aos", "gemini-usage.jsonl")),
        "opencode": os.path.isfile(os.path.join(HOME, ".local", "share", "opencode", "opencode.db"))
        or aos.get("go_30d", 0) > 0,
    }
    hide = set(cfg.get("hide") or [])
    for it in items:
        it["present"] = bool(present.get(it["id"], True)) and it["id"] not in hide and it.get("label") not in hide
    out = {
        "updated_at": int(now),
        "updated_str": fmt_jst(now),
        "items": items,
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
