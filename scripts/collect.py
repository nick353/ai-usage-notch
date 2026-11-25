#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""各AIサブスク（Claude / Codex / Gemini / OpenCode）の残り%と消費量を時間軸ごとに高速集計して JSON で返す。
ふだんはこの Mac の中の記録だけを読む。Claude の週・5時間の枠だけは、ほかに記録が無いとき、
Claude Code のログイン情報で Anthropic の使用量の窓口に聞く（config.json の claude_oauth: false で止められる）。
ログイン情報は Anthropic にだけ渡し、保存も表示もしない。"""

from concurrent.futures import ThreadPoolExecutor
import glob
import json
import os
import re
import select
import shutil
import signal
import sqlite3
import subprocess
try:
    import pty
except Exception:
    pty = None
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


CLICK = T("｜押すと全AI一斉切替", " | click to switch all")
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


def collect_aos_usage(now, cache=None):
    """~/.config/aos/claude-usage.jsonl から Claude 5h/24h/7d/30d と OpenCode Go 5h/7d/30d を増分キャッシュで高速集計する。"""
    p = os.path.join(HOME, ".config", "aos", "claude-usage.jsonl")
    empty = {
        "claude_5h_tok": 0, "claude_5h_usd": 0.0,
        "claude_24h_tok": 0, "claude_24h_usd": 0.0,
        "claude_7d_tok": 0, "claude_7d_usd": 0.0,
        "claude_30d_tok": 0, "claude_30d_usd": 0.0,
        "claude_hours": {},
        "go_5h": 0.0, "go_7d": 0.0, "go_30d": 0.0, "go_unknown_7d": 0,
    }
    if not os.path.isfile(p):
        return empty
    try:
        st = os.stat(p)
    except Exception:
        return empty

    cutoff_30d = now - 30 * 86400
    cutoff_7d = now - 7 * 86400
    cutoff_24h = now - 86400
    cutoff_5h = now - 5 * 3600
    cutoff_hour_30d = int(cutoff_30d // 3600) * 3600

    c = cache.get("aos_usage_v2") if isinstance(cache, dict) else None
    if isinstance(c, dict) and c.get("ino") == st.st_ino and isinstance(c.get("offset"), int) and 0 <= c["offset"] <= st.st_size:
        offset = c["offset"]
        go_rows = [r for r in (c.get("go_rows") or []) if r[0] >= cutoff_30d]
        go_unk_rows = [t for t in (c.get("go_unk_rows") or []) if t >= cutoff_7d]
        cl_recent = [r for r in (c.get("cl_recent") or []) if r[0] >= cutoff_24h]
        cl_hours = {k: v for k, v in (c.get("cl_hours") or {}).items() if int(k) >= cutoff_hour_30d}
        seen = set(c.get("seen_ids") or [])
    else:
        offset = 0
        go_rows = []
        go_unk_rows = []
        cl_recent = []
        cl_hours = {}
        seen = set()

    go_keywords = ("go_allowance_usd", "grok-", "glm-", "kimi-", "qwen", "minimax", "deepseek")

    new_offset = offset
    if st.st_size > offset:
        try:
            with open(p, "rb") as f:
                f.seek(offset)
                raw = f.read()
            last_nl = raw.rfind(b"\n")
            if last_nl >= 0:
                chunk = raw[:last_nl + 1]
                new_offset = offset + len(chunk)
                for line in chunk.decode("utf-8", errors="ignore").splitlines():
                    if not line:
                        continue
                    has_go = any(k in line for k in go_keywords)
                    has_cl = ("claude" in line.lower()) or ("estimated_usd" in line)
                    if not has_go and not has_cl:
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
                                go_rows.append([int(at), val])
                        except Exception:
                            pass
                    elif has_go and at >= cutoff_7d:
                        model = str(r.get("model") or "")
                        if any(k in model for k in ("grok-", "glm-", "kimi-", "qwen", "minimax", "deepseek")):
                            if r.get("ok") or r.get("tokens"):
                                go_unk_rows.append(int(at))

                    if has_cl:
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
                            if tok > 0 or usd_val > 0:
                                hk = str(int(at // 3600) * 3600)
                                cur = cl_hours.get(hk)
                                if cur is None:
                                    cl_hours[hk] = [tok, round(usd_val, 6)]
                                else:
                                    cur[0] += tok
                                    cur[1] = round(cur[1] + usd_val, 6)
                                if at >= cutoff_24h:
                                    cl_recent.append([int(at), tok, usd_val])
        except Exception:
            pass

    if isinstance(cache, dict):
        seen_list = list(seen)[-4000:]
        cache["aos_usage_v2"] = {
            "ino": st.st_ino,
            "offset": new_offset,
            "go_rows": go_rows,
            "go_unk_rows": go_unk_rows,
            "cl_recent": cl_recent,
            "cl_hours": cl_hours,
            "seen_ids": seen_list,
        }
        cache.pop("aos_usage_v1", None)

    claude_5h_tok = 0
    claude_5h_usd = 0.0
    claude_24h_tok = 0
    claude_24h_usd = 0.0
    for ts, tok, usd_val in cl_recent:
        if ts >= cutoff_24h:
            claude_24h_tok += tok
            claude_24h_usd += usd_val
            if ts >= cutoff_5h:
                claude_5h_tok += tok
                claude_5h_usd += usd_val

    claude_7d_tok = 0
    claude_7d_usd = 0.0
    claude_30d_tok = 0
    claude_30d_usd = 0.0
    for hk, (tok, usd_val) in cl_hours.items():
        hts = int(hk)
        if hts >= cutoff_hour_30d:
            claude_30d_tok += tok
            claude_30d_usd += usd_val
            if hts >= cutoff_7d - 3600:
                claude_7d_tok += tok
                claude_7d_usd += usd_val

    go_5h = go_7d = go_30d = 0.0
    for ts, val in go_rows:
        if ts >= cutoff_30d:
            go_30d += val
            if ts >= cutoff_7d:
                go_7d += val
            if ts >= cutoff_5h:
                go_5h += val
    go_unknown_7d = sum(1 for ts in go_unk_rows if ts >= cutoff_7d)

    return {
        "claude_5h_tok": claude_5h_tok,
        "claude_5h_usd": claude_5h_usd,
        "claude_24h_tok": claude_24h_tok,
        "claude_24h_usd": claude_24h_usd,
        "claude_7d_tok": claude_7d_tok,
        "claude_7d_usd": claude_7d_usd,
        "claude_30d_tok": claude_30d_tok,
        "claude_30d_usd": claude_30d_usd,
        "claude_hours": cl_hours,
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


def _render_vt100(raw_text, rows=45, cols=120):
    """PTY 出力のカーソル制御を簡易エミュレートし、最終画面のテキスト行リストを返す。"""
    grid = [[" "] * cols for _ in range(rows)]
    r_cur = 0
    c_cur = 0
    i = 0
    n = len(raw_text)
    while i < n:
        ch = raw_text[i]
        if ch == "\x1b" and i + 1 < n and raw_text[i + 1] == "[":
            j = i + 2
            while j < n and raw_text[j] in "0123456789;?":
                j += 1
            cmd = raw_text[j] if j < n else ""
            params = raw_text[i + 2:j].lstrip("?")
            nums = [int(x) for x in params.split(";") if x.isdigit()]
            if cmd in ("H", "f"):
                r_cur = max(0, min(rows - 1, (nums[0] if len(nums) >= 1 else 1) - 1))
                c_cur = max(0, min(cols - 1, (nums[1] if len(nums) >= 2 else 1) - 1))
            elif cmd == "A":
                r_cur = max(0, r_cur - (nums[0] if nums else 1))
            elif cmd == "B":
                r_cur = min(rows - 1, r_cur + (nums[0] if nums else 1))
            elif cmd == "C":
                c_cur = min(cols - 1, c_cur + (nums[0] if nums else 1))
            elif cmd == "D":
                c_cur = max(0, c_cur - (nums[0] if nums else 1))
            elif cmd == "G":
                c_cur = max(0, min(cols - 1, (nums[0] if nums else 1) - 1))
            elif cmd == "J":
                mode = nums[0] if nums else 0
                if mode == 2:
                    grid = [[" "] * cols for _ in range(rows)]
                elif mode == 0:
                    for cc in range(c_cur, cols):
                        grid[r_cur][cc] = " "
                    for rr in range(r_cur + 1, rows):
                        grid[rr] = [" "] * cols
            elif cmd == "K":
                mode = nums[0] if nums else 0
                if mode == 0:
                    for cc in range(c_cur, cols):
                        grid[r_cur][cc] = " "
                elif mode == 2:
                    grid[r_cur] = [" "] * cols
            i = j + 1
        elif ch == "\x1b" and i + 1 < n and raw_text[i + 1] == "]":
            bel = raw_text.find("\x07", i + 2)
            i = bel + 1 if bel >= 0 else i + 2
        elif ch == "\x1b":
            i += 2
        elif ch == "\r":
            c_cur = 0
            i += 1
        elif ch == "\n":
            r_cur = min(rows - 1, r_cur + 1)
            i += 1
        elif ord(ch) >= 32:
            if 0 <= r_cur < rows and 0 <= c_cur < cols:
                grid[r_cur][c_cur] = ch
                c_cur += 1
            i += 1
        else:
            i += 1
    return ["".join(row).rstrip() for row in grid]


def _find_claude_bin():
    for cand in (
        os.environ.get("AOS_CLAUDE_BIN"),
        os.path.join(HOME, ".local", "bin", "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        shutil.which("claude"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _find_claude_trusted_cwd():
    """Claude Code が既に信頼済みのフォルダを1つ選ぶ（macOS の TCC 対象フォルダは避ける）。"""
    tcc_avoid = tuple(os.path.join(HOME, d) for d in ("Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music"))
    cj = os.path.join(HOME, ".claude.json")
    candidates = []
    try:
        with open(cj, encoding="utf-8") as f:
            d = json.load(f)
        projs = d.get("projects")
        if isinstance(projs, dict):
            for p, info in projs.items():
                if not isinstance(info, dict) or not info.get("hasTrustDialogAccepted") or not os.path.isdir(p):
                    continue
                if any(p == t or p.startswith(t + os.sep) for t in tcc_avoid):
                    continue
                candidates.append(p)
    except Exception:
        pass
    if BASE in candidates:
        return BASE
    for p in candidates:
        if p.startswith(os.path.join(HOME, "Projects") + os.sep):
            return p
    if candidates:
        return candidates[0]
    return BASE


_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_claude_reset_str(s, now):
    """'Resets Nov 29 at 10:59pm (Asia/Tokyo)' や 'Resets 7pm' / 'Resets in 2h 15m' をタイムスタンプに変換する。"""
    if not s:
        return None
    m_in = re.search(r"Resets\s+in\s+([0-9dhms ]+)", s, re.I)
    if m_in:
        dur = _parse_duration_sec(m_in.group(1).strip())
        if dur:
            return now + dur
    now_dt = datetime.fromtimestamp(now)
    # 例: Resets Nov 29 at 10:59pm / Resets Nov 29, 11pm
    m_dt = re.search(
        r"Resets\s+([A-Za-z]{3})\s+(\d{1,2})(?:\s+at|\s*,)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)",
        s,
        re.I,
    )
    if m_dt:
        mon = _MONTH_MAP.get(m_dt.group(1).lower())
        if mon:
            day = int(m_dt.group(2))
            hh = int(m_dt.group(3)) % 12
            if m_dt.group(5).lower() == "pm":
                hh += 12
            mm = int(m_dt.group(4) or 0)
            for yr in (now_dt.year, now_dt.year + 1):
                try:
                    cand = datetime(yr, mon, day, hh, mm).timestamp()
                    if cand >= now - 3600:
                        return cand
                except Exception:
                    continue
    # 例: Resets 7:30pm / Resets 11pm
    m_tm = re.search(r"Resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", s, re.I)
    if m_tm:
        hh = int(m_tm.group(1)) % 12
        if m_tm.group(3).lower() == "pm":
            hh += 12
        mm = int(m_tm.group(2) or 0)
        cand = datetime(now_dt.year, now_dt.month, now_dt.day, hh, mm).timestamp()
        if cand < now - 60:
            cand += 86400
        return cand
    return None


def _parse_claude_usage_output(raw_text, now):
    """Claude Code CLI (`claude`) の `/usage` 画面から公式の使用率とリセット時刻を抜く。"""
    if not raw_text:
        return None
    lines = [l.strip() for l in _render_vt100(raw_text, rows=45, cols=120) if l.strip()]
    plan_name = None
    for l in lines[:8]:
        m_pl = re.search(r"·\s*(Claude\s+[A-Za-z0-9 +_-]+)", l)
        if m_pl:
            plan_name = m_pl.group(1).strip()
            break

    five_used = None
    five_resets = None
    week_used = None
    week_resets = None
    extra_weeks = []

    i = 0
    while i < len(lines):
        l = lines[i]
        ll = l.lower()
        if ll.startswith("current session") or ll.startswith("current week"):
            # 直後数行から % used と Resets を拾う
            pct = None
            rst = None
            for j in range(i, min(len(lines), i + 4)):
                if pct is None:
                    mp = re.search(r"(\d+(?:\.\d+)?)\s*%\s*used", lines[j], re.I)
                    if mp:
                        pct = max(0.0, min(100.0, float(mp.group(1))))
                if rst is None and "resets" in lines[j].lower():
                    rst = _parse_claude_reset_str(lines[j], now)
            if pct is not None:
                if ll.startswith("current session"):
                    five_used = pct
                    five_resets = rst
                elif "all models" in ll or l.strip().lower() == "current week":
                    week_used = pct
                    week_resets = rst
                else:
                    m_lbl = re.search(r"\(([^()]+)\)", l)
                    if m_lbl:
                        extra_weeks.append({
                            "name": m_lbl.group(1).strip(),
                            "used": pct,
                            "resets": rst,
                        })
        i += 1

    if five_used is None and week_used is None:
        return None
    return {
        "at": now,
        "plan_name": plan_name,
        "five_used": five_used,
        "five_resets": five_resets,
        "week_used": week_used,
        "week_resets": week_resets,
        "extra_weeks": extra_weeks,
    }


def _probe_claude_usage(now, cache, cfg, force=False):
    """公式 CLI (`claude`) の `/usage` 画面を PTY で読み、公式の使用率を取得する（既定5分キャッシュ）。"""
    if cfg.get("claude_cli_usage") is False or pty is None:
        return None

    c = cache.get("claude_official_usage_v1") or {}
    cached_data = c.get("data") if isinstance(c.get("data"), dict) else None
    interval = max(60, int(cfg.get("claude_usage_interval_sec") or 300))

    if not force and cached_data and c.get("at"):
        rst_passed = False
        for rk in ("five_resets", "week_resets"):
            rv = cached_data.get(rk)
            if isinstance(rv, (int, float)) and rv > 0 and now >= rv:
                rst_passed = True
                break
        if not rst_passed and (now - float(c["at"])) < interval:
            return cached_data

    if not force and c.get("retry_after") and now < float(c["retry_after"]):
        if cached_data and c.get("at") and (now - float(c["at"])) < 3600:
            return cached_data
        return None

    cl = _find_claude_bin()
    if not cl:
        cache["claude_official_usage_v1"] = {**c, "retry_after": now + 600}
        return cached_data if (cached_data and c.get("at") and (now - float(c["at"])) < 3600) else None

    master = None
    proc = None
    buf = b""
    try:
        master, slave = pty.openpty()
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        user_name = env.get("USER") or os.path.basename(HOME)
        env["USER"] = user_name
        env.setdefault("LOGNAME", user_name)
        extra_path = os.path.join(HOME, ".local", "bin") + ":/opt/homebrew/bin:/usr/local/bin"
        env["PATH"] = extra_path + ":" + env.get("PATH", "/usr/bin:/bin")
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = "120"
        env["LINES"] = "45"
        proc = subprocess.Popen(
            [cl],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=_find_claude_trusted_cwd(),
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)

        t0 = time.time()
        deadline = t0 + 14.0
        sent = 0
        t_sent = 0.0
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    buf += chunk
                except OSError:
                    break
            txt = _ANSI_ESCAPE_RE.sub(" ", buf.decode("utf-8", errors="ignore"))
            if "Do you trust the files" in txt or "Do you trust the contents" in txt:
                break
            if "API Usage Billing" in txt and (time.time() - t0) > 1.5:
                break
            if sent == 0:
                if "Make auto mode" in txt:
                    os.write(master, b"\x1b")
                    time.sleep(0.25)
                    os.write(master, b"/usage\r")
                    sent = 1
                    t_sent = time.time()
                elif ("Try " in txt or "shortcuts" in txt or "bypass permissions" in txt) and (time.time() - t0) > 1.8:
                    os.write(master, b"/usage\r")
                    sent = 1
                    t_sent = time.time()
            else:
                if ("Current week" in txt or "Current session" in txt) and "% used" in txt and (
                    "Usage credits" in txt or "Extra usage" in txt or (time.time() - t_sent) > 2.5
                ):
                    end2 = time.time() + 0.45
                    while time.time() < end2:
                        r2, _, _ = select.select([master], [], [], 0.05)
                        if r2:
                            try:
                                c2 = os.read(master, 65536)
                                if not c2:
                                    break
                                buf += c2
                            except OSError:
                                break
                    break
                if sent == 1 and (time.time() - t_sent) > 2.0 and "Usage" not in txt:
                    os.write(master, b"\r")
                    sent = 2
    except Exception:
        pass
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                proc.wait(timeout=0.8)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if master is not None:
            try:
                os.close(master)
            except Exception:
                pass

    parsed = _parse_claude_usage_output(buf.decode("utf-8", errors="ignore"), now)
    if parsed:
        cache["claude_official_usage_v1"] = {"at": now, "data": parsed}
        return parsed

    cache["claude_official_usage_v1"] = {**c, "retry_after": now + 300}
    if cached_data and c.get("at") and (now - float(c["at"])) < 3600:
        return cached_data
    return None


_UNSET = object()


def collect_claude(now, aos, cache, cfg=None, force_probe=False, official_override=_UNSET):
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
    extra_weeks = []
    plan_name = None
    is_cli_official = False

    # claude-week.json が無い・5分以上古い・強制更新時は claude CLI の公式 /usage から最新実測を取る
    need_cli = force_probe or week_used is None or not updated_at or (now - updated_at) > 300
    cli_official = None if official_override is _UNSET else official_override
    if official_override is _UNSET and need_cli:
        cli_official = _probe_claude_usage(now, cache, cfg or {}, force=force_probe)

    if isinstance(cli_official, dict):
        cli_at = cli_official.get("at") or now
        if updated_at is None or cli_at >= updated_at or force_probe:
            if cli_official.get("week_used") is not None:
                week_used = round(float(cli_official["week_used"]))
                if cli_official.get("week_resets"):
                    week_resets = cli_official["week_resets"]
                updated_at = cli_at
                is_cli_official = True
            if cli_official.get("five_used") is not None:
                five_used = round(float(cli_official["five_used"]))
                five_resets = cli_official.get("five_resets")
                is_cli_official = True
            extra_weeks = cli_official.get("extra_weeks") or []
            plan_name = cli_official.get("plan_name")

    oauth = None
    if not is_cli_official and (week_used is None or not updated_at or now - updated_at > 6 * 3600):
        oauth = collect_claude_oauth(now, cache, cfg or {})
    if oauth and oauth.get("week"):
        week_used = round(oauth["week"]["used"])
        week_resets = oauth["week"]["resets"]
        updated_at = now
    if five_used is None:
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
    plan_tag = f" ({plan_name}・公式)" if (plan_name and is_cli_official and LANG == "ja") else (
        f" ({plan_name} · official)" if (plan_name and is_cli_official) else ""
    )

    # 実測ペースから週間上限USDを自動校正（5時間目安・月間目安の%算出に使う）
    cl_hours = aos.get("claude_hours") or {}
    default_week_cap = 2000.0 if (plan_name and "ultra" in plan_name.lower()) else (800.0 if (plan_name and "max" in plan_name.lower()) else 1500.0)
    week_cap_usd = default_week_cap
    if week_used is not None and week_used >= 5 and week_resets:
        win_start_ts = week_resets - 7 * 86400
        win_usd = sum(u for hk, (_, u) in cl_hours.items() if int(hk) >= win_start_ts - 3600)
        if win_usd > 10.0:
            week_cap_usd = max(100.0, win_usd / (week_used / 100.0))
    cap_5h_usd = max(10.0, week_cap_usd / 14.0)
    cap_m_usd = max(400.0, week_cap_usd * (30.0 / 7.0))

    # 全AI共通の4スロット順: 1:5時間 -> 2:週間 -> 3:月間 -> 4:その他（別枠/24h）
    views = []

    # 1) 5時間スロット
    if five_used is not None:
        rem5 = max(0, 100 - five_used)
        rst5 = T(f"（{fmt_jst(five_resets)} に戻る）", f" (resets {fmt_jst(five_resets)})") if five_resets else ""
        views.append({
            "key": "5h",
            "slot": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": rem5,
            "center": f"{rem5}%",
            "status": rem_status(rem5),
            "tip1": T(f"Claude{plan_tag}  5時間 残り {rem5}%{rst5}", f"Claude{plan_tag}  5h: {rem5}% left{rst5}"),
            "tip2": T(f"5時間消費 {fmt_tokens(tok_5h)}", f"5h used {fmt_tokens(tok_5h)} tokens") + usd_note(aos['claude_5h_usd']) + (SEP + rec_str if rec_str else "") + CLICK,
        })
    else:
        used_5h_pct = min(100.0, (aos["claude_5h_usd"] / cap_5h_usd) * 100.0) if cap_5h_usd > 0 else 0.0
        rem5 = 0 if limit_active else max(0, round(100.0 - used_5h_pct))
        note5 = T("週間枠のみプラン・5時間ペース目安", "weekly-only plan · 5h pace est.") if (is_cli_official and week_used is not None) else T("5時間ペース目安", "5h pace est.")
        views.append({
            "key": "5h",
            "slot": "5h",
            "sub": T("5h目安", "5h est"),
            "mode": "percent",
            "percent": rem5,
            "center": f"{rem5}%",
            "status": "danger" if limit_active else rem_status(rem5),
            "tip1": T(f"Claude  5時間目安 残り 約{rem5}%（消費 ${aos['claude_5h_usd']:.1f} / ${cap_5h_usd:.0f}）", f"Claude  5h est.: ~{rem5}% left (${aos['claude_5h_usd']:.1f} / ${cap_5h_usd:.0f})"),
            "tip2": T(f"直近5時間 {fmt_tokens(tok_5h)}・{note5}", f"last 5h {fmt_tokens(tok_5h)} tokens · {note5}") + CLICK,
        })

    # 2) 週間スロット（公式）
    if limit_active:
        views.append({
            "key": "week",
            "slot": "week",
            "sub": T("週間", "Week"),
            "mode": "percent",
            "percent": 0,
            "center": "0%",
            "status": "danger",
            "tip1": f"Claude{plan_tag}  {limit_active}",
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
            "slot": "week",
            "sub": T("週間", "Week"),
            "mode": "percent",
            "percent": rem_w,
            "center": f"{rem_w}%",
            "status": rem_status(rem_w),
            "tip1": T(f"Claude{plan_tag}  週間 残り {rem_w}%{rst_w}", f"Claude{plan_tag}  Week: {rem_w}% left{rst_w}"),
            "tip2": SEP.join(parts) + CLICK,
        })
    else:
        u7 = aos.get("claude_7d_usd") or 0.0
        t7 = aos.get("claude_7d_tok") or tok_24h
        rem_w = max(0, round(100.0 - min(100.0, (u7 / week_cap_usd) * 100.0)))
        views.append({
            "key": "week",
            "slot": "week",
            "sub": T("週目安", "Wk est"),
            "mode": "percent",
            "percent": rem_w,
            "center": f"{rem_w}%",
            "status": rem_status(rem_w),
            "tip1": T(f"Claude  週間目安 残り 約{rem_w}%（消費 ${u7:.0f} / ${week_cap_usd:.0f}）", f"Claude  Week est.: ~{rem_w}% left (${u7:.0f} / ${week_cap_usd:.0f})"),
            "tip2": T(f"7日 {fmt_tokens(t7)} トークン", f"7d {fmt_tokens(t7)} tokens") + CLICK,
        })

    # 3) 月間スロット（30日ペース目安%）
    u30 = aos.get("claude_30d_usd") or 0.0
    t30 = aos.get("claude_30d_tok") or tok_24h
    rem_m = max(0, round(100.0 - min(100.0, (u30 / cap_m_usd) * 100.0))) if cap_m_usd > 0 else 100
    views.append({
        "key": "month",
        "slot": "month",
        "sub": T("月目安", "Mo est"),
        "mode": "percent",
        "percent": rem_m,
        "center": f"{rem_m}%",
        "status": rem_status(rem_m),
        "tip1": T(f"Claude  月間目安 残り 約{rem_m}%（30日 ${u30:.0f} / ${cap_m_usd:.0f}）", f"Claude  Month est.: ~{rem_m}% left (30d ${u30:.0f} / ${cap_m_usd:.0f})"),
        "tip2": T(f"30日消費 {fmt_tokens(t30)} トークン（週間ペース換算）", f"30d {fmt_tokens(t30)} tokens (from weekly pace)") + CLICK,
    })

    # 4) その他スロット（モデル別週間枠があれば優先、無ければ24時間目安%）
    if extra_weeks:
        ew = extra_weeks[0]
        ename = str(ew.get("name") or "Extra").strip()
        ew_rem = max(0, 100 - round(float(ew.get("used") or 0)))
        ew_rst = ew.get("resets")
        ew_rst_s = T(f"（{fmt_jst(ew_rst)} に戻る）", f" (resets {fmt_jst(ew_rst)})") if ew_rst else ""
        views.append({
            "key": "extra",
            "slot": "extra",
            "sub": ename[:6],
            "mode": "percent",
            "percent": ew_rem,
            "center": f"{ew_rem}%",
            "status": rem_status(ew_rem),
            "tip1": T(f"Claude ({ename})  別枠 残り {ew_rem}%{ew_rst_s}", f"Claude ({ename})  Pool: {ew_rem}% left{ew_rst_s}"),
            "tip2": T(f"24時間 {fmt_tokens(tok_24h)}", f"24h {fmt_tokens(tok_24h)} tokens") + (SEP + rec_str if rec_str else "") + CLICK,
        })
    else:
        cap_24h = max(30.0, week_cap_usd / 5.0)
        rem_24 = max(0, round(100.0 - min(100.0, (aos["claude_24h_usd"] / cap_24h) * 100.0)))
        views.append({
            "key": "extra",
            "slot": "extra",
            "sub": T("24h目安", "24h est"),
            "mode": "percent",
            "percent": rem_24,
            "center": f"{rem_24}%",
            "status": rem_status(rem_24),
            "tip1": T(f"Claude  24時間目安 残り 約{rem_24}%（消費 ${aos['claude_24h_usd']:.1f} / ${cap_24h:.0f}）", f"Claude  24h est.: ~{rem_24}% left (${aos['claude_24h_usd']:.1f} / ${cap_24h:.0f})"),
            "tip2": T(f"24時間消費 {fmt_tokens(tok_24h)} トークン", f"24h {fmt_tokens(tok_24h)} tokens") + CLICK,
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
    for d_off in range(0, 31):
        d = dt_now - timedelta(days=d_off)
        pat = os.path.join(HOME, ".codex", "sessions", d.strftime("%Y"), d.strftime("%m"), d.strftime("%d"), "*.jsonl")
        files.extend(glob.glob(pat))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    codex_cache = cache.setdefault("codex_v2", {})
    cache.pop("codex", None)
    latest_rl = None
    latest_ts = None
    cutoff_30d = now - 30 * 86400
    cutoff_7d = now - 7 * 86400
    cutoff_24h = now - 86400
    cutoff_5h = now - 5 * 3600
    tok_5h = 0
    tok_24h = 0
    tok_7d = 0
    tok_30d = 0
    file_toks = []
    peak_bal = float(cache.get("codex_credits_peak") or 0.0)
    active_keys = set()

    for fp in files:
        try:
            st = os.stat(fp)
        except Exception:
            continue
        if st.st_mtime < cutoff_30d and latest_rl is not None:
            continue
        active_keys.add(fp)
        sig = f"{int(st.st_mtime)}:{st.st_size}"
        ent = codex_cache.get(fp)
        if isinstance(ent, dict) and ent.get("sig") == sig:
            rl = ent.get("rl")
            ts = ent.get("ts")
            tok = int(ent.get("tok") or 0)
            bal = ent.get("bal")
        else:
            rl, ts, tok = parse_codex_tail(fp, 96_000)
            if rl is None and tok == 0 and st.st_size > 96_000 and st.st_mtime >= cutoff_24h:
                rl, ts, tok = parse_codex_tail(fp, 512_000)
            bal = None
            if isinstance(rl, dict):
                cr = rl.get("credits") or {}
                if cr.get("balance") is not None:
                    try:
                        bal = float(cr["balance"])
                    except Exception:
                        bal = None
            # 24時間より古いファイルは巨大な rate_limits 辞書はキャッシュせず軽量保持
            store_rl = rl if st.st_mtime >= cutoff_24h else None
            codex_cache[fp] = {"sig": sig, "rl": store_rl, "ts": ts, "tok": tok, "bal": bal}

        if latest_rl is None and rl is not None:
            latest_rl = rl
            latest_ts = ts
        if isinstance(bal, (int, float)) and bal > peak_bal:
            peak_bal = float(bal)
        if tok > 0 and st.st_mtime >= cutoff_30d:
            tok_30d += tok
            file_toks.append((st.st_mtime, tok))
            if st.st_mtime >= cutoff_7d:
                tok_7d += tok
            if st.st_mtime >= cutoff_24h:
                tok_24h += tok
            if st.st_mtime >= cutoff_5h:
                tok_5h += tok

    for k in list(codex_cache.keys()):
        if k not in active_keys:
            del codex_cache[k]
    if peak_bal > 0:
        cache["codex_credits_peak"] = peak_bal

    credits_bal = None
    has_balance = False
    win_5h = None
    win_week = None

    if latest_rl:
        credits = latest_rl.get("credits") or {}
        if credits.get("has_credits") and credits.get("balance") is not None:
            try:
                credits_bal = float(credits["balance"])
                has_balance = credits_bal > 0
                if credits_bal > peak_bal:
                    peak_bal = credits_bal
                    cache["codex_credits_peak"] = peak_bal
            except Exception:
                pass

        for wobj in (latest_rl.get("primary"), latest_rl.get("secondary")):
            if isinstance(wobj, dict) and isinstance(wobj.get("used_percent"), (int, float)):
                wmin = int(wobj.get("window_minutes") or 10080)
                if wmin >= 1440:
                    win_week = (wmin, wobj)
                else:
                    win_5h = (wmin, wobj)

    bal_note = T(f"残クレジット {credits_bal:,.0f}", f"credits left {credits_bal:,.0f}") if has_balance else ""
    rec_note = T(f"記録 {fmt_jst(latest_ts)}", f"recorded {fmt_jst(latest_ts)}") if latest_ts else ""

    # 実測ペースから週間トークン上限を自動校正
    week_cap_tok = max(tok_7d * 2.0, 15_000_000_000.0)
    if win_week:
        _, wobj = win_week
        u_w = float(wobj.get("used_percent") or 0.0)
        rst_w = wobj.get("resets_at")
        if u_w >= 3.0 and rst_w:
            w_start = float(rst_w) - 7 * 86400
            tok_win_w = sum(tk for mt, tk in file_toks if mt >= w_start)
            if tok_win_w > 50_000_000:
                week_cap_tok = max(500_000_000.0, tok_win_w / (u_w / 100.0))
    cap_5h_tok = max(100_000_000.0, week_cap_tok / 14.0)
    cap_m_tok = max(2_000_000_000.0, week_cap_tok * (30.0 / 7.0))

    views = []

    # 1) 5時間スロット
    if win_5h:
        wmin, wobj = win_5h
        used_pct = round(max(0.0, min(100.0, float(wobj["used_percent"]))))
        rem_pct = max(0, 100 - used_pct)
        rst = wobj.get("resets_at")
        rst_str = T(f"（{fmt_jst(rst)} に戻る）", f" (resets {fmt_jst(rst)})") if rst else ""
        h = max(1, int(wmin // 60))
        st = "warn" if (rem_pct == 0 and has_balance) else rem_status(rem_pct)
        parts = [T(f"5時間 {fmt_tokens(tok_5h)}", f"5h {fmt_tokens(tok_5h)} tokens")]
        if rec_note:
            parts.append(rec_note)
        views.append({
            "key": "5h",
            "slot": "5h",
            "sub": T(f"{h}時間", f"{h}h"),
            "mode": "percent",
            "percent": rem_pct,
            "center": f"{rem_pct}%",
            "status": st,
            "tip1": T(f"Codex  {h}時間 残り {rem_pct}%{rst_str}", f"Codex  {h}h: {rem_pct}% left{rst_str}"),
            "tip2": SEP.join(parts) + CLICK,
        })
    else:
        rem_5h = max(0, round(100.0 - min(100.0, (tok_5h / cap_5h_tok) * 100.0))) if cap_5h_tok > 0 else 100
        views.append({
            "key": "5h",
            "slot": "5h",
            "sub": T("5h目安", "5h est"),
            "mode": "percent",
            "percent": rem_5h,
            "center": f"{rem_5h}%",
            "status": rem_status(rem_5h),
            "tip1": T(f"Codex  5時間目安 残り 約{rem_5h}%（消費 {fmt_tokens(tok_5h)} / {fmt_tokens(cap_5h_tok)}）", f"Codex  5h est.: ~{rem_5h}% left ({fmt_tokens(tok_5h)} / {fmt_tokens(cap_5h_tok)})"),
            "tip2": T(f"24時間 {fmt_tokens(tok_24h)}（週間枠のみプラン・5時間ペース目安）", f"24h {fmt_tokens(tok_24h)} (weekly-only plan · 5h pace est.)") + CLICK,
        })

    # 2) 週間スロット（公式）
    if win_week:
        _, wobj = win_week
        used_pct = round(max(0.0, min(100.0, float(wobj["used_percent"]))))
        rem_pct = max(0, 100 - used_pct)
        rst = wobj.get("resets_at")
        rst_str = T(f"（{fmt_jst(rst)} に戻る）", f" (resets {fmt_jst(rst)})") if rst else ""
        st = "warn" if (rem_pct == 0 and has_balance) else rem_status(rem_pct)
        parts = []
        if bal_note:
            parts.append(bal_note)
        parts.append(T(f"24時間 {fmt_tokens(tok_24h)}", f"24h {fmt_tokens(tok_24h)} tokens"))
        if rec_note:
            parts.append(rec_note)
        views.append({
            "key": "week",
            "slot": "week",
            "sub": T("週間", "Week"),
            "mode": "percent",
            "percent": rem_pct,
            "center": f"{rem_pct}%",
            "status": st,
            "tip1": T(f"Codex  週間 残り {rem_pct}%{rst_str}", f"Codex  Week: {rem_pct}% left{rst_str}"),
            "tip2": SEP.join(parts) + CLICK,
        })
    else:
        rem_w = max(0, round(100.0 - min(100.0, (tok_7d / week_cap_tok) * 100.0)))
        views.append({
            "key": "week",
            "slot": "week",
            "sub": T("週目安", "Wk est"),
            "mode": "percent",
            "percent": rem_w,
            "center": f"{rem_w}%",
            "status": rem_status(rem_w),
            "tip1": T(f"Codex  週間目安 残り 約{rem_w}%（7日 {fmt_tokens(tok_7d)}）", f"Codex  Week est.: ~{rem_w}% left (7d {fmt_tokens(tok_7d)})"),
            "tip2": T(f"24時間 {fmt_tokens(tok_24h)} トークン", f"24h {fmt_tokens(tok_24h)} tokens") + CLICK,
        })

    # 3) 月間スロット（30日ペース目安%）
    rem_m = max(0, round(100.0 - min(100.0, (tok_30d / cap_m_tok) * 100.0))) if cap_m_tok > 0 else 100
    views.append({
        "key": "month",
        "slot": "month",
        "sub": T("月目安", "Mo est"),
        "mode": "percent",
        "percent": rem_m,
        "center": f"{rem_m}%",
        "status": rem_status(rem_m),
        "tip1": T(f"Codex  月間目安 残り 約{rem_m}%（30日 {fmt_tokens(tok_30d)} / {fmt_tokens(cap_m_tok)}）", f"Codex  Month est.: ~{rem_m}% left (30d {fmt_tokens(tok_30d)} / {fmt_tokens(cap_m_tok)})"),
        "tip2": T(f"7日 {fmt_tokens(tok_7d)}・30日 {fmt_tokens(tok_30d)} トークン（週間ペース換算）", f"7d {fmt_tokens(tok_7d)} · 30d {fmt_tokens(tok_30d)} tokens") + CLICK,
    })

    # 4) その他スロット（追加クレジット残高を最高残高比の残り%リングで表示）
    if has_balance and credits_bal is not None:
        base_cr = max(peak_bal, credits_bal, 1.0)
        rem_cr = max(0, min(100, round((credits_bal / base_cr) * 100.0)))
        views.append({
            "key": "extra",
            "slot": "extra",
            "sub": T("残クレ", "Credits"),
            "mode": "percent",
            "percent": rem_cr,
            "center": f"{rem_cr}%",
            "status": rem_status(rem_cr),
            "tip1": T(f"Codex  追加クレジット 残り {rem_cr}%（{credits_bal:,.0f} / {base_cr:,.0f}）", f"Codex  extra credits: {rem_cr}% left ({credits_bal:,.0f} / {base_cr:,.0f})"),
            "tip2": T("週間枠を使い切ってもこの残高から稼働（過去最大残高比）", "used after weekly limit (vs peak balance)") + CLICK,
        })
    else:
        cap_24h = max(200_000_000.0, week_cap_tok / 5.0)
        rem_24 = max(0, round(100.0 - min(100.0, (tok_24h / cap_24h) * 100.0)))
        views.append({
            "key": "extra",
            "slot": "extra",
            "sub": T("24h目安", "24h est"),
            "mode": "percent",
            "percent": rem_24,
            "center": f"{rem_24}%",
            "status": rem_status(rem_24),
            "tip1": T(f"Codex  24時間目安 残り 約{rem_24}%（消費 {fmt_tokens(tok_24h)}）", f"Codex  24h est.: ~{rem_24}% left ({fmt_tokens(tok_24h)})"),
            "tip2": T("追加クレジットなし", "No extra credits") + CLICK,
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
    if "carbon" in m:
        return (4.0, 0.20, 20.0)
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
        return None, None, []

    pairs.sort(key=lambda x: x[0])
    active_reset = None
    active_429_at = None
    for log_ts, reset_ts in pairs:
        if reset_ts > now:
            active_reset = reset_ts
            active_429_at = log_ts

    # 同じ枠の複数429（数秒の揺れ）を1つの確定リセット時刻にまとめる
    raw_resets = sorted(r for _, r in pairs)
    known = []
    for r in raw_resets:
        if known and abs(r - known[-1]) <= 600:
            known[-1] = r
        else:
            known.append(r)
    return active_reset, active_429_at, known


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


# Gemini / Google AI プラン別の目安上限（USD 換算。Pro は実測の429到達額 $24.6 基準、Ultra は公式倍率 5x / 20x）
GEMINI_PLAN_ORDER = ("free", "plus", "pro", "ultra_5x", "ultra_20x")
GEMINI_PLANS = {
    "free":      {"label": "Free",      "5h": 6.0,   "week": 15.0,   "month": 50.0},
    "plus":      {"label": "Plus",      "5h": 12.0,  "week": 32.0,   "month": 100.0},
    "pro":       {"label": "Pro",       "5h": 25.0,  "week": 65.0,   "month": 200.0},
    "ultra_5x":  {"label": "Ultra 5x",  "5h": 125.0, "week": 325.0,  "month": 1000.0},
    "ultra_20x": {"label": "Ultra 20x", "5h": 500.0, "week": 1300.0, "month": 4000.0},
}
GEMINI_PLAN_ALIASES = {
    "free": "free",
    "plus": "plus",
    "pro": "pro",
    "20": "pro",
    "ultra": "ultra_5x",
    "ultra5": "ultra_5x",
    "ultra5x": "ultra_5x",
    "ultra_5x": "ultra_5x",
    "5x": "ultra_5x",
    "100": "ultra_5x",
    "ultra20": "ultra_20x",
    "ultra20x": "ultra_20x",
    "ultra_20x": "ultra_20x",
    "20x": "ultra_20x",
    "200": "ultra_20x",
}


def _find_agy_bin():
    for cand in (
        os.environ.get("AOS_AGY_BIN"),
        os.path.join(HOME, ".local", "bin", "agy"),
        shutil.which("agy"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\x1b[=>]"
)


def _parse_agy_quota_block(block, now):
    """Models & Quota の1グループ分から Weekly / Five Hour の残り%とリセット時刻を抜く。"""
    def _extract(sub):
        if not sub:
            return None, None
        m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", sub)
        rem = max(0.0, min(100.0, float(m_pct.group(1)))) if m_pct else None
        m_rst = re.search(r"Refreshes in\s+([0-9dhms ]+)", sub, re.I)
        resets_at = None
        if m_rst:
            dur = _parse_duration_sec(m_rst.group(1).strip())
            if dur:
                resets_at = now + dur
        return rem, resets_at

    idx_w = block.find("Weekly Limit Remaining")
    idx_5 = block.find("Five Hour Limit Remaining")
    w_sub = ""
    h5_sub = ""
    if idx_w >= 0:
        end_w = idx_5 if idx_5 > idx_w else len(block)
        w_sub = block[idx_w:end_w]
    if idx_5 >= 0:
        end_5 = idx_w if idx_w > idx_5 else len(block)
        h5_sub = block[idx_5:end_5]
    w_rem, w_rst = _extract(w_sub)
    h5_rem, h5_rst = _extract(h5_sub)
    return w_rem, w_rst, h5_rem, h5_rst


def _parse_agy_usage_output(raw_text, now):
    """Antigravity CLI (`agy`) の `/usage` 画面出力から公式の残り%・リセット時刻・プラン名を抜く。"""
    if not raw_text:
        return None
    clean = _ANSI_ESCAPE_RE.sub(" ", raw_text)
    clean = re.sub(r"[ \t]+", " ", clean)

    plan_name = None
    m_plan = re.search(r"[^\s@]+@[^\s@]+\s+\(([^()\r\n]{2,40})\)", clean)
    if m_plan:
        plan_name = m_plan.group(1).strip()

    idx_gem = clean.find("GEMINI MODELS")
    if idx_gem < 0:
        return None
    idx_cg = clean.find("CLAUDE AND GPT MODELS", idx_gem)
    idx_tail = clean.find("Within each group", idx_gem)

    gem_end = len(clean)
    for cand in (idx_cg, idx_tail):
        if cand > idx_gem and cand < gem_end:
            gem_end = cand
    gem_block = clean[idx_gem:gem_end]
    g_w_rem, g_w_rst, g_5_rem, g_5_rst = _parse_agy_quota_block(gem_block, now)

    cg_w_rem = cg_w_rst = cg_5_rem = cg_5_rst = None
    if idx_cg >= 0:
        cg_end = idx_tail if idx_tail > idx_cg else len(clean)
        cg_block = clean[idx_cg:cg_end]
        cg_w_rem, cg_w_rst, cg_5_rem, cg_5_rst = _parse_agy_quota_block(cg_block, now)

    if g_w_rem is None and g_5_rem is None:
        return None

    return {
        "at": now,
        "plan_name": plan_name,
        "gemini_5h_rem": g_5_rem,
        "gemini_5h_resets_at": g_5_rst,
        "gemini_week_rem": g_w_rem,
        "gemini_week_resets_at": g_w_rst,
        "cg_5h_rem": cg_5_rem,
        "cg_5h_resets_at": cg_5_rst,
        "cg_week_rem": cg_w_rem,
        "cg_week_resets_at": cg_w_rst,
    }


def _find_agy_trusted_cwd():
    """agy が追加の信頼確認を出さずに開ける、既存の信頼済みフォルダを1つ選ぶ。"""
    settings_path = os.path.join(HOME, ".gemini", "antigravity-cli", "settings.json")
    try:
        with open(settings_path, encoding="utf-8") as f:
            st = json.load(f)
        for p in st.get("trustedWorkspaces") or []:
            if isinstance(p, str) and os.path.isdir(p):
                return p
    except Exception:
        pass
    last_conv = os.path.join(HOME, ".gemini", "antigravity-cli", "cache", "last_conversations.json")
    try:
        with open(last_conv, encoding="utf-8") as f:
            lc = json.load(f)
        if isinstance(lc, dict):
            for p in lc.keys():
                if isinstance(p, str) and os.path.isdir(p) and "/tmp" not in p and "antigravity-tmp" not in p:
                    return p
    except Exception:
        pass
    return BASE


def _probe_gemini_usage(now, cache, cfg, force=False):
    """公式 CLI (`agy`) の `/usage` 画面を PTY で読み、公式の 5時間/週間 残り% を取得する（既定5分キャッシュ）。"""
    if cfg.get("gemini_usage_official") is False or pty is None:
        return None

    c = cache.get("gemini_official_usage_v1") or {}
    cached_data = c.get("data") if isinstance(c.get("data"), dict) else None
    interval = max(60, int(cfg.get("gemini_usage_interval_sec") or 300))

    if not force and cached_data and c.get("at"):
        rst_passed = False
        for rk in ("gemini_5h_resets_at", "gemini_week_resets_at"):
            rv = cached_data.get(rk)
            if isinstance(rv, (int, float)) and rv > 0 and now >= rv:
                rst_passed = True
                break
        if not rst_passed and (now - float(c["at"])) < interval:
            return cached_data

    if not force and c.get("retry_after") and now < float(c["retry_after"]):
        if cached_data and c.get("at") and (now - float(c["at"])) < 3600:
            return cached_data
        return None

    agy = _find_agy_bin()
    if not agy:
        cache["gemini_official_usage_v1"] = {**c, "retry_after": now + 600}
        return cached_data if (cached_data and c.get("at") and (now - float(c["at"])) < 3600) else None

    master = None
    proc = None
    buf = b""
    try:
        master, slave = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = "120"
        env["LINES"] = "40"
        env["AGY_BARE"] = "1"
        proc = subprocess.Popen(
            [agy],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=_find_agy_trusted_cwd(),
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)

        t0 = time.time()
        deadline = t0 + 12.0
        sent = 0
        t_sent = 0.0
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(master, 65536)
                    if not chunk:
                        break
                    buf += chunk
                except OSError:
                    break
            txt = _ANSI_ESCAPE_RE.sub(" ", buf.decode("utf-8", errors="ignore"))
            if "Do you trust the contents" in txt:
                break
            if sent == 0:
                if "? for shortcuts" in txt or "esc to dismiss" in txt:
                    time.sleep(0.25)
                    os.write(master, b"/usage")
                    time.sleep(0.2)
                    os.write(master, b"\r")
                    sent = 1
                    t_sent = time.time()
                elif "not signed in" in txt.lower() and "signing in" not in txt.lower() and (time.time() - t0) > 3.5:
                    break
            else:
                if "GEMINI MODELS" in txt and "Five Hour Limit Remaining" in txt and (
                    "Within each group" in txt or "Quota available" in txt or txt.count("%") >= 2
                ):
                    end2 = time.time() + 0.35
                    while time.time() < end2:
                        r2, _, _ = select.select([master], [], [], 0.05)
                        if r2:
                            try:
                                c2 = os.read(master, 65536)
                                if not c2:
                                    break
                                buf += c2
                            except OSError:
                                break
                    break
                if sent == 1 and (time.time() - t_sent) > 1.5 and "GEMINI MODELS" not in txt:
                    os.write(master, b"\r")
                    sent = 2
    except Exception:
        pass
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                proc.wait(timeout=0.8)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        if master is not None:
            try:
                os.close(master)
            except Exception:
                pass

    parsed = _parse_agy_usage_output(buf.decode("utf-8", errors="ignore"), now)
    if parsed:
        cache["gemini_official_usage_v1"] = {"at": now, "data": parsed}
        return parsed

    cache["gemini_official_usage_v1"] = {**c, "retry_after": now + 300}
    if cached_data and c.get("at") and (now - float(c["at"])) < 3600:
        return cached_data
    return None


def _probe_gemini_plan(now, cache, force=False):
    """公式 CLI の `agy models` から現在の Google AI プランを推定する（30分キャッシュ）。"""
    c = cache.get("gemini_plan_probe_v1") or {}
    if not force and c.get("plan") in GEMINI_PLANS:
        ttl = 300 if c.get("fallback") else 1800
        if c.get("at") and (now - float(c["at"])) < ttl:
            return c["plan"]

    agy = _find_agy_bin()
    if not agy:
        plan = c.get("plan") if c.get("plan") in GEMINI_PLANS else "pro"
        cache["gemini_plan_probe_v1"] = {"at": now, "plan": plan, "fallback": True}
        return plan

    try:
        r = subprocess.run([agy, "models"], capture_output=True, text=True, timeout=12)
        txt = ((r.stdout or "") + "\n" + (r.stderr or "")).lower()
        detected = None
        if r.returncode == 0 and txt.strip():
            if "gemini-4-carbon" in txt or "g1-ultra" in txt or "orchestrator-ultra" in txt:
                detected = "ultra_5x"
            elif "gemini-4-neon" in txt or "g1-pro" in txt:
                detected = "pro"
            elif "gemini-4-hydrogen" in txt:
                detected = "plus"
            elif "gemini-" in txt:
                detected = "free"
        if detected:
            cache["gemini_plan_probe_v1"] = {"at": now, "plan": detected, "fallback": False}
            return detected
    except Exception:
        pass

    plan = c.get("plan") if c.get("plan") in GEMINI_PLANS else "pro"
    cache["gemini_plan_probe_v1"] = {"at": now, "plan": plan, "fallback": True}
    return plan


def _resolve_gemini_plan(now, cfg, cache, u5, active_reset, force_plan=False):
    raw = str(cfg.get("gemini_plan") or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    alias = GEMINI_PLAN_ALIASES.get(raw)
    if alias in GEMINI_PLANS:
        plan_key = alias
        plan_mode = "manual"
    else:
        plan_key = _probe_gemini_plan(now, cache, force=force_plan)
        plan_mode = "auto"
        # 429 枠切れしていないのに現プランの5時間目安を大きく超えて動いている場合は上位プランへ自動補正
        if active_reset is None and u5 > GEMINI_PLANS[plan_key]["5h"] * 1.25:
            idx = GEMINI_PLAN_ORDER.index(plan_key)
            for cand in GEMINI_PLAN_ORDER[idx + 1:]:
                plan_key = cand
                if u5 <= GEMINI_PLANS[cand]["5h"] * 1.25:
                    break

    spec = GEMINI_PLANS[plan_key]
    def _cap(cfg_key, default_val):
        v = cfg.get(cfg_key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        return float(default_val)

    cap_5h = _cap("gemini_5h_usd", spec["5h"])
    cap_w = _cap("gemini_week_usd", spec["week"])
    cap_m = _cap("gemini_month_usd", spec["month"])
    return plan_key, plan_mode, spec["label"], cap_5h, cap_w, cap_m


def collect_gemini(now, cfg, cache=None, force_plan=False, official_override=_UNSET):
    if cache is None:
        cache = {}

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

    active_reset, active_429_at, known_resets = _collect_gemini_quota_resets(now, cache)
    latest_ev_ts = max((ts for ts, _, _ in events), default=0.0)
    if active_reset is not None and active_429_at is not None and latest_ev_ts > active_429_at + 10:
        # 429 枠切れのあとに正常な応答が返っている（プラン変更等で解除済み）
        active_reset = None
    win = _compute_gemini_5h_window(events, now, known_resets)
    u5 = win["usd"]
    t5 = win["tok"]
    win_start = win["win_start"]
    win_end = win["win_end"]

    official = _probe_gemini_usage(now, cache, cfg, force=force_plan) if official_override is _UNSET else official_override
    if official and official.get("gemini_5h_resets_at"):
        # 公式の5時間リセット時刻が分かっているときは、その5時間前から現在までの実トークンを集計する
        off_5h_start = float(official["gemini_5h_resets_at"]) - 5 * 3600
        win_start = off_5h_start
        t5 = sum(tok for ts, _, tok in events if ts >= off_5h_start)
        u5 = sum(usd for ts, usd, _ in events if ts >= off_5h_start)
    if official and official.get("plan_name"):
        pname = official["plan_name"]
        pl = pname.lower()
        if "ultra" in pl:
            plan_key = "ultra_5x"
        elif "pro" in pl:
            plan_key = "pro"
        elif "plus" in pl:
            plan_key = "plus"
        elif "free" in pl:
            plan_key = "free"
        else:
            plan_key = "pro"
        plan_mode = "official"
        plan_label = pname
        spec = GEMINI_PLANS[plan_key]
        cap_5h = float(cfg.get("gemini_5h_usd") or spec["5h"])
        cap_w = float(cfg.get("gemini_week_usd") or spec["week"])
        cap_m = float(cfg.get("gemini_month_usd") or spec["month"])
    else:
        plan_key, plan_mode, plan_label, cap_5h, cap_w, cap_m = _resolve_gemini_plan(
            now, cfg, cache, u5, active_reset, force_plan=force_plan
        )

    if official:
        plan_mode = "official"
    plan_tag = f" ({plan_label}" + (
        T("・公式)", " · official)") if official else (T("・自動)", " · auto)") if plan_mode == "auto" else ")")
    )
    est_tag = f" ({plan_label}" + (T("・目安)", " · est.)") if official else (T("・自動)", " · auto)") if plan_mode == "auto" else ")"))
    plan_hint = T("••• からプラン変更可", "change plan in ••• menu")

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

    rec_note = T(f"記録 {fmt_jst(official['at'])}", f"recorded {fmt_jst(official['at'])}") if (official and official.get("at")) else ""

    # 5時間ビュー: 公式 `/usage` が取れたら実測値を最優先、無いときだけ429ログ・固定5時間枠の推計
    if official and official.get("gemini_5h_rem") is not None:
        rem_f = max(0.0, min(100.0, float(official["gemini_5h_rem"])))
        used_f = max(0.0, 100.0 - rem_f)
        rem_5h = round(rem_f)
        rst = official.get("gemini_5h_resets_at")
        rst_s = T(f"（{fmt_jst(rst)} に戻る）", f" (resets {fmt_jst(rst)})") if rst else ""
        parts = [T(f"消費 {used_f:.1f}%", f"{used_f:.1f}% used"), T(f"5時間 {fmt_tokens(t5)}", f"5h {fmt_tokens(t5)} tokens")]
        if rec_note:
            parts.append(rec_note)
        view_5h = {
            "key": "5h",
            "slot": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": rem_5h,
            "center": f"{rem_5h}%",
            "status": rem_status(rem_5h),
            "tip1": T(f"Gemini{plan_tag}  5時間 残り {rem_f:.1f}%{rst_s}", f"Gemini{plan_tag}  5h: {rem_f:.1f}% left{rst_s}"),
            "tip2": SEP.join(parts) + CLICK,
        }
    elif active_reset is not None:
        rst_s = T(f"（{fmt_jst(active_reset)} に戻る）", f" (resets {fmt_jst(active_reset)})")
        view_5h = {
            "key": "5h",
            "slot": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": 0,
            "center": "0%",
            "status": "danger",
            "tip1": T(f"Gemini{plan_tag}  5時間 上限中{rst_s}", f"Gemini{plan_tag}  5h: Limit reached{rst_s}"),
            "tip2": T(f"消費 {fmt_tokens(t5)} トークン（目安 ${u5:.2f}・{plan_hint}）", f"{fmt_tokens(t5)} tokens used (est. ${u5:.2f} · {plan_hint})") + CLICK,
        }
    elif not win["active"]:
        view_5h = {
            "key": "5h",
            "slot": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": 100,
            "center": "100%",
            "status": "ok",
            "tip1": T(f"Gemini{plan_tag}  5時間 残り 100%（リセット済み・目安 $0.00 / ${cap_5h:.0f}）", f"Gemini{plan_tag}  5h: 100% left (reset · est. $0.00 / ${cap_5h:.0f})"),
            "tip2": T(f"消費 0 トークン（{plan_hint}）", f"0 tokens used ({plan_hint})") + CLICK,
        }
    else:
        used_pct = min(100.0, (u5 / cap_5h) * 100.0) if cap_5h > 0 else 0.0
        rem_5h = max(0, round(100.0 - used_pct))
        rst_s = T(f"{fmt_jst(win_end)} に戻る・", f"resets {fmt_jst(win_end)} · ") if win_end else ""
        view_5h = {
            "key": "5h",
            "slot": "5h",
            "sub": T("5時間", "5h"),
            "mode": "percent",
            "percent": rem_5h,
            "center": f"{rem_5h}%",
            "status": rem_status(rem_5h),
            "tip1": T(f"Gemini{plan_tag}  5時間 残り 約{rem_5h}%（{rst_s}目安 ${u5:.2f} / ${cap_5h:.0f}）", f"Gemini{plan_tag}  5h: ~{rem_5h}% left ({rst_s}est. ${u5:.2f} / ${cap_5h:.0f})"),
            "tip2": T(f"消費 {fmt_tokens(t5)} トークン（{plan_hint}）", f"{fmt_tokens(t5)} tokens used ({plan_hint})") + CLICK,
        }

    def make_est_view(key, slot, sub, title, usd, cap, tok):
        used_pct = min(100.0, (usd / cap) * 100.0) if cap > 0 else 0.0
        rem = max(0, round(100.0 - used_pct))
        return {
            "key": key,
            "slot": slot,
            "sub": sub,
            "mode": "percent",
            "percent": rem,
            "center": f"{rem}%",
            "status": rem_status(rem),
            "tip1": T(f"Gemini{est_tag}  {title} 残り 約{rem}%（目安 ${usd:.2f} / ${cap:.0f}）", f"Gemini{est_tag}  {title}: ~{rem}% left (est. ${usd:.2f} / ${cap:.0f})"),
            "tip2": T(f"消費 {fmt_tokens(tok)} トークン（{plan_hint}）", f"{fmt_tokens(tok)} tokens used ({plan_hint})") + CLICK,
        }

    # 週間ビュー: 公式 `/usage` が取れたら実測値を最優先
    eff_cap_m = cap_m
    if official and official.get("gemini_week_rem") is not None:
        rem_f = max(0.0, min(100.0, float(official["gemini_week_rem"])))
        used_f = max(0.0, 100.0 - rem_f)
        rem_w = round(rem_f)
        rst = official.get("gemini_week_resets_at")
        rst_s = T(f"（{fmt_jst(rst)} に戻る）", f" (resets {fmt_jst(rst)})") if rst else ""
        if used_f >= 5.0 and rst:
            w_start = float(rst) - 7 * 86400
            u_win_w = sum(usd for ts, usd, _ in events if ts >= w_start)
            if u_win_w > 5.0:
                eff_cap_w = max(cap_w, u_win_w / (used_f / 100.0))
                eff_cap_m = max(cap_m, eff_cap_w * (30.0 / 7.0))
        parts = [T(f"消費 {used_f:.1f}%", f"{used_f:.1f}% used"), T(f"7日 {fmt_tokens(t7)}", f"7d {fmt_tokens(t7)} tokens")]
        if rec_note:
            parts.append(rec_note)
        view_week = {
            "key": "week",
            "slot": "week",
            "sub": T("週間", "Week"),
            "mode": "percent",
            "percent": rem_w,
            "center": f"{rem_w}%",
            "status": rem_status(rem_w),
            "tip1": T(f"Gemini{plan_tag}  週間 残り {rem_f:.1f}%{rst_s}", f"Gemini{plan_tag}  Week: {rem_f:.1f}% left{rst_s}"),
            "tip2": SEP.join(parts) + CLICK,
        }
    else:
        view_week = make_est_view("week", "week", T("週間", "Week"), T("週間", "Week"), u7, cap_w, t7)

    # 月間ビュー（実測週間ペースから校正した月間残り%目安）
    view_month = make_est_view("month", "month", T("月目安", "Mo est"), T("月間目安", "Month est."), u30, eff_cap_m, t30)

    # 全AI共通の4スロット順: 1:5時間 -> 2:週間 -> 3:月間 -> 4:その他（契約内Claude/GPT別枠）
    views = [view_5h, view_week, view_month]
    if official and official.get("cg_week_rem") is not None:
        cg_w_f = max(0.0, min(100.0, float(official["cg_week_rem"])))
        cg_w_rem = round(cg_w_f)
        cg_rst = official.get("cg_week_resets_at")
        cg_rst_s = T(f"（{fmt_jst(cg_rst)} に戻る）", f" (resets {fmt_jst(cg_rst)})") if cg_rst else ""
        cg_parts = []
        if official.get("cg_5h_rem") is not None:
            cg_parts.append(T(f"5時間 残り {float(official['cg_5h_rem']):.1f}%", f"5h {float(official['cg_5h_rem']):.1f}% left"))
        cg_parts.append(T("契約内の Claude・GPT 別枠", "Claude/GPT pool in same plan"))
        if rec_note:
            cg_parts.append(rec_note)
        views.append({
            "key": "extra",
            "slot": "extra",
            "sub": T("別枠週", "Other Wk"),
            "mode": "percent",
            "percent": cg_w_rem,
            "center": f"{cg_w_rem}%",
            "status": rem_status(cg_w_rem),
            "tip1": T(f"Gemini契約内 Claude・GPT  週間 残り {cg_w_f:.1f}%{cg_rst_s}", f"Gemini plan Claude/GPT  Week: {cg_w_f:.1f}% left{cg_rst_s}"),
            "tip2": SEP.join(cg_parts) + CLICK,
        })
    else:
        views.append({
            "key": "extra",
            "slot": "extra",
            "sub": T("別枠なし", "No extra"),
            "mode": "percent",
            "percent": None,
            "center": "—",
            "status": "none",
            "tip1": T(f"Gemini ({plan_label})  別枠データなし", f"Gemini ({plan_label})  No extra pool data"),
            "tip2": T("公式 /usage が取れたときに Claude・GPT 別枠を表示", "Shows Claude/GPT pool when official /usage is available") + CLICK,
        })

    return {
        "id": "gemini",
        "label": "Gemini",
        "plan": plan_key,
        "plan_mode": plan_mode,
        "plan_label": plan_label,
        "views": views,
    }

def collect_opencode_db(now, cache=None):
    dbp = os.path.join(HOME, ".local", "share", "opencode", "opencode.db")
    if not os.path.isfile(dbp):
        return 0.0, 0.0, 0.0
    try:
        st = os.stat(dbp)
        wal = dbp + "-wal"
        wal_sig = ""
        if os.path.isfile(wal):
            wst = os.stat(wal)
            wal_sig = f":{int(wst.st_mtime)}:{wst.st_size}"
        sig = f"{int(st.st_mtime)}:{st.st_size}{wal_sig}"
        c5_ms = int((now - 5 * 3600) * 1000)
        c7_ms = int((now - 7 * 86400) * 1000)
        c30_ms = int((now - 30 * 86400) * 1000)

        ent = cache.get("opencode_db_v1") if isinstance(cache, dict) else None
        if isinstance(ent, dict) and ent.get("sig") == sig:
            rows_parsed = ent.get("rows") or []
        else:
            con = None
            try:
                con = sqlite3.connect(dbp, timeout=2.0)
                con.execute("PRAGMA query_only = ON")
                rows = con.execute(
                    "SELECT time_created, data FROM message WHERE time_created >= ?",
                    (c30_ms,),
                ).fetchall()
            except Exception:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
                con = sqlite3.connect(f"file:{dbp}?mode=ro&immutable=1", uri=True)
                rows = con.execute(
                    "SELECT time_created, data FROM message WHERE time_created >= ?",
                    (c30_ms,),
                ).fetchall()
            finally:
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
            rows_parsed = []
            for tc, raw in rows:
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                    c = d.get("cost")
                    if isinstance(c, (int, float)) and c > 0:
                        rows_parsed.append([int(tc), float(c)])
                except Exception:
                    pass
            if isinstance(cache, dict):
                cache["opencode_db_v1"] = {"sig": sig, "rows": rows_parsed}

        u5 = u7 = u30 = 0.0
        for tc, val in rows_parsed:
            if tc >= c30_ms:
                u30 += val
                if tc >= c7_ms:
                    u7 += val
                if tc >= c5_ms:
                    u5 += val
        return u5, u7, u30
    except Exception:
        return 0.0, 0.0, 0.0


def collect_opencode(now, aos, cfg, cache=None):
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

    db5, db7, db30 = collect_opencode_db(now, cache)
    usd_5h = aos["go_5h"] + db5
    usd_w = aos["go_7d"] + db7
    usd_m = aos["go_30d"] + db30

    def make_view(key, slot, sub, title, usd, cap, hit_down):
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
            "slot": slot,
            "sub": sub,
            "mode": "percent",
            "percent": rem,
            "center": f"{rem}%",
            "status": st,
            "tip1": tip1,
            "tip2": T(f"5h ${usd_5h:.2f}・週 ${usd_w:.2f}・月 ${usd_m:.2f}", f"5h ${usd_5h:.2f} · week ${usd_w:.2f} · month ${usd_m:.2f}") + CLICK,
        }

    # 全AI共通の4スロット順: 1:5時間 -> 2:週間 -> 3:月間 -> 4:その他（別枠なし）
    views = [
        make_view("5h", "5h", T("5時間", "5h"), T("5時間", "5h"), usd_5h, cap_5h, down_until is not None),
        make_view("week", "week", T("週間", "Week"), T("週間", "Week"), usd_w, cap_w, down_until is not None and "week" in (down_limit or "").lower()),
        make_view("month", "month", T("月間", "Month"), T("月間", "Month"), usd_m, cap_m, down_until is not None and "month" in (down_limit or "").lower()),
        {
            "key": "extra",
            "slot": "extra",
            "sub": T("別枠なし", "No extra"),
            "mode": "percent",
            "percent": None,
            "center": "—",
            "status": "none",
            "tip1": T("OpenCode Go  別枠・追加クレジットなし", "OpenCode Go  No extra pool or credits"),
            "tip2": T(f"5h ${usd_5h:.2f}・週 ${usd_w:.2f}・月 ${usd_m:.2f}", f"5h ${usd_5h:.2f} · week ${usd_w:.2f} · month ${usd_m:.2f}") + CLICK,
        },
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
    force_refresh = "--refresh-plan" in sys.argv
    aos = collect_aos_usage(now, cache)

    # Claude / Gemini の公式 /usage 取得はキャッシュ切れ時に数秒かかるため並列実行する
    week_path = os.path.join(HOME, ".config", "aos", "claude-week.json")
    claude_week_fresh = False
    if not force_refresh and os.path.isfile(week_path):
        try:
            with open(week_path, encoding="utf-8") as f:
                w = json.load(f)
            uat = parse_iso(w.get("at"))
            if uat and (now - uat) <= 300:
                claude_week_fresh = True
        except Exception:
            pass

    claude_cache_sub = {"claude_official_usage_v1": cache.get("claude_official_usage_v1")}
    gemini_cache_sub = {"gemini_official_usage_v1": cache.get("gemini_official_usage_v1")}
    cached_cl_ent = cache.get("claude_official_usage_v1") or {}
    claude_off = cached_cl_ent.get("data") if isinstance(cached_cl_ent.get("data"), dict) else None
    gemini_off = None
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_c = None if claude_week_fresh else ex.submit(_probe_claude_usage, now, claude_cache_sub, cfg, force_refresh)
        fut_g = ex.submit(_probe_gemini_usage, now, gemini_cache_sub, cfg, force_refresh)
        if fut_c is not None:
            try:
                claude_off = fut_c.result()
            except Exception:
                claude_off = None
        try:
            gemini_off = fut_g.result()
        except Exception:
            gemini_off = None
    if claude_cache_sub.get("claude_official_usage_v1") is not None:
        cache["claude_official_usage_v1"] = claude_cache_sub["claude_official_usage_v1"]
    if gemini_cache_sub.get("gemini_official_usage_v1") is not None:
        cache["gemini_official_usage_v1"] = gemini_cache_sub["gemini_official_usage_v1"]

    items = [
        collect_claude(now, aos, cache, cfg, force_probe=force_refresh, official_override=claude_off),
        collect_codex(now, cache),
        collect_gemini(now, cfg, cache, force_plan=force_refresh, official_override=gemini_off),
        collect_opencode(now, aos, cfg, cache),
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
