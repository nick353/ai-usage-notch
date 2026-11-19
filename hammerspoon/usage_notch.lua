-- AI Usage Notch（AI 残り枠・トークン消費サイドノッチ）
-- 画面の端に黒い切り欠き（サイドノッチ）を貼り、マウスを寄せると各 AI サブスクの残り枠がパッと出る。
-- 見た目・つまんで運ぶ手ごたえは Work Recorder（作業録画のサイドノッチ）と完全に揃えている。
--   ふだん: 端に細い黒い線だけ。マウスを寄せるとノッチが出てくる。
--   4つの輪: Claude / Codex / Gemini / OpenCode の「残り%」（一番小さい時間軸を初期表示）。
--   クリック: 時間軸を切り替え（5時間 → 週間 → 月間 など）。option+クリックで今すぐ最新値に更新。
--   カーソルを乗せる: 内側に黒い吹き出しで、戻る日時・残クレジット・消費量などの詳細。
--   長押し（0.25秒）か押したまま動かす: つまんで上下・左右の端へ運べる。離すと近い方の端へ吸い寄せられ位置を覚える。
--   ctrl+option+cmd+U でも5秒だけパッと表示。外から: open -g "hammerspoon://ai-usage-refresh"

local M = {}
local HOME = os.getenv("HOME")
local BASE = debug.getinfo(1, "S").source:sub(2):match("^(.*)/hammerspoon/[^/]+$") or (HOME .. "/Projects/ai-usage-notch")
local COLLECT = BASE .. "/scripts/collect.py"

local function loadConfig()
  local f = io.open(BASE .. "/config.json", "r")
  if not f then return {} end
  local body = f:read("*a"); f:close()
  local ok, c = pcall(hs.json.decode, body)
  return (ok and type(c) == "table") and c or {}
end
local CONFIG = loadConfig()
local REFRESH_SEC = tonumber(CONFIG.refresh_sec) or 120
-- 言語: config.json の "lang"（"ja" / "en"）があればそれ、無ければ Mac の言語（日本語なら日本語、ほかは英語）
local LANG = CONFIG.lang or (((hs.host.locale.preferredLanguages() or {})[1] or "en"):sub(1, 2) == "ja" and "ja" or "en")
LANG = tostring(LANG):sub(1, 2) == "ja" and "ja" or "en"
local function L(ja, en) return LANG == "ja" and ja or en end

local function clamp(v, a, b) return math.max(a, math.min(b, v)) end

---------------------------------------------------------------- 見た目の定数（work-recorder と完全統一）
local K = 0.5523
local STEP = 62
local COUNT = 4
local GEAR_H = 20  -- 一番下の「•••」（表示するものを選ぶ）の段
local function calcH(n) return 28 + STEP * (n - 1) + 42 + GEAR_H end
local H = calcH(COUNT)
local OPEN_W, REST_W = 54, 6
local FILLET = 10
local BLACK = { red = 0.04, green = 0.04, blue = 0.05, alpha = 1 }
local GRAY = { white = 0.62 }
local NAME_COLOR = { white = 0.78 }
local SUB_DIM = { white = 0.48 }
local SUBTEXT = { white = 0.72 }
local WHITE = { white = 0.94 }
local RED = { red = 0.96, green = 0.23, blue = 0.21 }
local AMBER = { red = 0.96, green = 0.68, blue = 0.20 }
local GREEN = { red = 0.36, green = 0.80, blue = 0.45 }
local BLUE = { red = 0.38, green = 0.68, blue = 0.98 }
local DIM = { white = 0.28 }
local PAD = 16

local function defaultPosition()
  local recSide = hs.settings.get("workrec.side") or "right"
  local recY = hs.settings.get("workrec.yfrac") or 0.42
  local y = recY < 0.55 and clamp(recY + 0.32, 0.66, 0.80) or clamp(recY - 0.32, 0.20, 0.34)
  return recSide, y
end
local defSide, defY = defaultPosition()
local side = hs.settings.get("aiusage.side") or defSide
local yfrac = hs.settings.get("aiusage.yfrac") or defY

local function placeholder(id, label)
  return {
    id = id,
    label = label,
    views = { { key = "init", sub = L("読込中", "loading"), mode = "percent", percent = nil, center = "--", status = "none", tip1 = label .. L("  読み込み中...", "  loading..."), tip2 = L("しばらくお待ちください", "one moment") } },
  }
end

local rawItems = nil   -- 集計が返した全部（表示の選択をかける前）
local applyPrefs       -- 下で定義
local items = {
  placeholder("claude", "Claude"),
  placeholder("codex", "Codex"),
  placeholder("gemini", "Gemini"),
  placeholder("opencode", "OpenCode"),
}
-- 各AIで今選んでいるビューの番号（1 = 一番時間軸が小さい初期表示）
local viewIdx = { claude = 1, codex = 1, gemini = 1, opencode = 1 }

local notch = hs.canvas.new({ x = 0, y = 0, w = REST_W, h = H + 20 })
local tip = hs.canvas.new({ x = 0, y = 0, w = 10, h = 10 })
local width = REST_W
local target = REST_W
local anim = nil
local hovering = false
local pinnedUntil = 0
local loading = false
local lastFetch = 0
local pulse = 0
local fillet = FILLET
local mode = "docked"
local float = { x = 0, y = 0, sx = 1, sy = 1, vx = 0, vy = 0 }
local drawnW = nil
local activeHoverId = nil

local function screenFrame() return hs.screen.primaryScreen():fullFrame() end

local function centerY(f, T)
  return clamp(f.y + f.h * yfrac, f.y + 30 + T / 2, f.y + f.h - 8 - T / 2)
end

local function statusColor(st)
  if st == "danger" then return RED
  elseif st == "warn" then return AMBER
  elseif st == "ok" then return GREEN
  elseif st == "info" then return BLUE
  else return GRAY end
end

local function activeView(it)
  local vs = it.views
  if type(vs) ~= "table" or #vs == 0 then
    return { sub = "", mode = "percent", percent = nil, center = "--", status = "none", tip1 = it.label, tip2 = "" }
  end
  local idx = viewIdx[it.id] or 1
  if idx < 1 or idx > #vs then idx = 1; viewIdx[it.id] = 1 end
  return vs[idx]
end

local function shape(w, rf)
  local r = math.min(rf, w / 2)
  local R = math.min(15, w - r)
  local T = H + 2 * FILLET
  local top = FILLET - r
  local pts = {
    { x = w, y = top },
    { x = w - r, y = top + r, c1x = w, c1y = top + K * r, c2x = w - r + K * r, c2y = top + r },
    { x = R, y = FILLET },
    { x = 0, y = FILLET + R, c1x = R - K * R, c1y = FILLET, c2x = 0, c2y = FILLET + R - K * R },
    { x = 0, y = FILLET + H - R },
    { x = R, y = FILLET + H, c1x = 0, c1y = FILLET + H - R + K * R, c2x = R - K * R, c2y = FILLET + H },
    { x = w - r, y = FILLET + H },
    { x = w, y = FILLET + H + r, c1x = w - r + K * r, c1y = FILLET + H, c2x = w, c2y = FILLET + H + r - K * r },
  }
  if side == "left" then
    for _, p in ipairs(pts) do
      p.x = w - p.x
      if p.c1x then p.c1x = w - p.c1x; p.c2x = w - p.c2x end
    end
  end
  return pts, T
end

local function blob(cw, ch, sx, sy)
  local w, h = OPEN_W * sx, (H + 8) * sy
  local x, y = (cw - w) / 2, (ch - h) / 2
  return { type = "rectangle", frame = { x = x, y = y, w = w, h = h },
    roundedRectRadii = { xRadius = math.min(24, w / 2), yRadius = math.min(24, w / 2) },
    action = "fill", fillColor = BLACK, trackMouseDown = true, id = "body" }
end

local function ring(id, cx, cy, color, frac)
  local out = {
    -- 当たり判定はラベル付近まで少し広めにして押しやすくする
    { id = id, type = "rectangle", frame = { x = cx - 24, y = cy - 16, w = 48, h = 52 }, action = "fill",
      fillColor = { white = 1, alpha = 0.001 }, trackMouseEnterExit = true, trackMouseDown = true },
    { type = "circle", center = { x = cx, y = cy }, radius = 12.5, action = "stroke",
      strokeColor = DIM, strokeWidth = 2.5 },
  }
  if frac and frac > 0 then
    table.insert(out, { type = "arc", center = { x = cx, y = cy }, radius = 12.5, startAngle = 0,
      endAngle = 360 * math.min(frac, 1), action = "stroke", strokeColor = color, strokeWidth = 2.5,
      arcRadii = false })
  end
  return out
end

local function nameLabel(text, cx, y)
  return { type = "text", text = hs.styledtext.new(text, { font = { size = 8.5 },
    color = NAME_COLOR, paragraphStyle = { alignment = "center" } }),
    frame = { x = cx - 27, y = y, w = 54, h = 11 } }
end

local function subLabel(text, cx, y, color)
  return { type = "text", text = hs.styledtext.new(text or "", { font = { size = 7.5 },
    color = color or SUB_DIM, paragraphStyle = { alignment = "center" } }),
    frame = { x = cx - 27, y = y, w = 54, h = 10 } }
end

local function centerText(text, cx, cy, color)
  local s = tostring(text or "--")
  local len = #s
  local sz = len <= 3 and 9.5 or (len == 4 and 7.8 or 7.0)
  local dy = len <= 3 and 6.2 or (len == 4 and 5.0 or 4.5)
  return { type = "text", text = hs.styledtext.new(s, { font = { name = ".AppleSystemUIFontBold", size = sz },
    color = color or WHITE, paragraphStyle = { alignment = "center" } }),
    frame = { x = cx - 13, y = cy - dy, w = 26, h = 15 } }
end

local function buttons(els, cx, top)
  for i, it in ipairs(items) do
    local v = activeView(it)
    local cy = top + 28 + (i - 1) * STEP
    local col = statusColor(v.status)
    local frac = 0
    if loading then
      frac = ((pulse + (i - 1) * 0.7) % (2 * math.pi)) / (2 * math.pi)
      col = GRAY
    elseif v.mode == "percent" and type(v.percent) == "number" then
      frac = clamp(v.percent / 100, 0, 1)
    elseif v.status == "info" or v.status == "ok" then
      frac = 1
    end
    for _, e in ipairs(ring(it.id, cx, cy, col, frac)) do table.insert(els, e) end
    table.insert(els, centerText(v.center, cx, cy, WHITE))
    table.insert(els, nameLabel(it.label, cx, cy + 16))
    local subCol = v.mode == "percent" and SUB_DIM or { red = 0.52, green = 0.72, blue = 0.92 }
    table.insert(els, subLabel(v.sub, cx, cy + 27, subCol))
  end
  local gy = top + H - 14
  table.insert(els, { id = "gear", type = "rectangle", frame = { x = cx - 22, y = gy - 10, w = 44, h = 20 }, action = "fill",
    fillColor = { white = 1, alpha = 0.001 }, trackMouseEnterExit = true, trackMouseDown = true })
  table.insert(els, { type = "text", text = hs.styledtext.new("•••", { font = { size = 9 }, color = SUB_DIM,
    paragraphStyle = { alignment = "center" } }), frame = { x = cx - 22, y = gy - 7, w = 44, h = 14 } })
end

local function itemCy(id)
  for i, it in ipairs(items) do
    if it.id == id then return FILLET + 28 + (i - 1) * STEP, it end
  end
  return nil, nil
end

local function draw()
  local f = screenFrame()
  if mode == "floating" then
    local cw, ch = OPEN_W + 2 * PAD, H + 8 + 2 * PAD
    notch:frame({ x = math.floor(float.x - cw / 2), y = math.floor(float.y - ch / 2), w = cw, h = ch })
    local els = { blob(cw, ch, float.sx, float.sy) }
    buttons(els, cw / 2, (ch - (H + 8) * float.sy) / 2 + 4 * float.sy)
    notch:replaceElements(els)
    drawnW = nil
    return
  end
  local pts, T = shape(width, fillet)
  local cy = centerY(f, T)
  local x = side == "left" and f.x or (f.x + f.w - width)
  local newFrame = { x = x, y = math.floor(cy - T / 2), w = width, h = T }
  local growing = drawnW == nil or width >= drawnW
  if not growing then notch:frame(newFrame) end
  local els = {
    { id = "body", type = "segments", closed = true, coordinates = pts, action = "fill", fillColor = BLACK,
      trackMouseDown = true },
  }
  if width >= OPEN_W * 0.8 then
    buttons(els, side == "left" and (width / 2 - 1) or (width / 2 + 1), FILLET)
  end
  notch:replaceElements(els)
  if growing then notch:frame(newFrame) end
  drawnW = width
end

local function animateTo(w)
  target = w
  if anim then return end
  anim = hs.timer.doEvery(1 / 60, function()
    if mode ~= "docked" then return end
    local d = target - width
    if math.abs(d) <= 1 then
      width = target
      anim:stop(); anim = nil
    else
      local step = d * 0.3
      if step > 0 then step = math.max(1, math.floor(step)) else step = math.min(-1, math.ceil(step)) end
      width = width + step
    end
    draw()
  end)
end

-- 2行の黒い吹き出し（ボタンの内側の向きに出す）
local function showTip(line1, line2, cy)
  if mode ~= "docked" then return end
  local nf = notch:frame()
  local sf = screenFrame()
  local st1 = hs.styledtext.new(line1 or "", { font = { name = ".AppleSystemUIFontBold", size = 12 }, color = { white = 0.96 } })
  local st2 = hs.styledtext.new(line2 or "", { font = { size = 10.5 }, color = SUBTEXT })
  local sz1 = hs.drawing.getTextDrawingSize(st1)
  local sz2 = hs.drawing.getTextDrawingSize(st2)
  local tw = math.max(sz1.w, sz2.w)
  local w = math.ceil(tw) + 24
  local h = (line2 and #line2 > 0) and 46 or 30
  local onLeft = side == "left"
  local ty = clamp(nf.y + cy - h / 2, sf.y + 28, sf.y + sf.h - h - 8)
  local arrowY = clamp(nf.y + cy - ty, 12, h - 12)
  tip:frame({ x = onLeft and (nf.x + nf.w + 4) or (nf.x - w - 10), y = ty, w = w + 6, h = h })
  local bx = onLeft and 6 or 0
  local tri = onLeft
    and { { x = 7, y = arrowY - 6 }, { x = 0, y = arrowY }, { x = 7, y = arrowY + 6 } }
    or { { x = w - 1, y = arrowY - 6 }, { x = w + 6, y = arrowY }, { x = w - 1, y = arrowY + 6 } }
  local els = {
    { type = "rectangle", frame = { x = bx, y = 0, w = w, h = h }, roundedRectRadii = { xRadius = 9, yRadius = 9 },
      action = "fill", fillColor = BLACK },
    { type = "segments", closed = true, action = "fill", fillColor = BLACK, coordinates = tri },
  }
  if line2 and #line2 > 0 then
    table.insert(els, { type = "text", text = st1, frame = { x = bx + 12, y = 7, w = sz1.w + 6, h = sz1.h + 2 } })
    table.insert(els, { type = "text", text = st2, frame = { x = bx + 12, y = 25, w = sz2.w + 6, h = sz2.h + 2 } })
  else
    table.insert(els, { type = "text", text = st1, frame = { x = bx + 12, y = (h - sz1.h) / 2, w = sz1.w + 6, h = sz1.h + 2 } })
  end
  tip:replaceElements(els)
  tip:show()
end

local function showTipForId(id)
  if id == "gear" then
    showTip(L("表示するものを選ぶ", "Choose what to show"), L("足す・消す・並べ替え（右クリックでも開く）", "Add, remove, reorder (or right-click)"), FILLET + H - 14)
    return
  end
  local cy, it = itemCy(id)
  if not it then return end
  local v = activeView(it)
  showTip(v.tip1, v.tip2, cy)
end

local closeTimer = nil
local function wantOpen() return hovering or hs.timer.secondsSinceEpoch() < pinnedUntil end
local function settle()
  if closeTimer then closeTimer:stop(); closeTimer = nil end
  if mode ~= "docked" then return end
  if wantOpen() then
    animateTo(OPEN_W)
  else
    closeTimer = hs.timer.doAfter(1.0, function()
      closeTimer = nil
      if not wantOpen() and mode == "docked" then tip:hide(); activeHoverId = nil; animateTo(REST_W) end
    end)
  end
end

---------------------------------------------------------------- 非同期データ更新
local fetchTask = nil
local function refresh(force)
  if fetchTask then return end
  local now = hs.timer.secondsSinceEpoch()
  if not force and (now - lastFetch) < 15 then return end
  loading = true
  if mode == "docked" and width > REST_W + 1 then draw() end
  fetchTask = hs.task.new("/usr/bin/python3", function(code, out)
    fetchTask = nil
    loading = false
    lastFetch = hs.timer.secondsSinceEpoch()
    if code == 0 and out and #out > 0 then
      local ok, data = pcall(hs.json.decode, out)
      if ok and type(data) == "table" and type(data.items) == "table" and #data.items > 0 then
        rawItems = data.items
        applyPrefs()
      end
    end
    draw()
    if activeHoverId and mode == "docked" and width >= OPEN_W * 0.8 then
      showTipForId(activeHoverId)
    end
  end, { COLLECT, "--lang", LANG })
  if not fetchTask:start() then
    fetchTask = nil
    loading = false
  end
end

---------------------------------------------------------------- つまむ・運ぶ・吸い寄せる
local press = nil
local spring = nil
local moveFloat

local function dockedCenter(f, s)
  local T = H + 2 * FILLET
  local cx = s == "left" and (f.x + OPEN_W / 2) or (f.x + f.w - OPEN_W / 2)
  return cx, centerY(f, T)
end

local function snap()
  local f = screenFrame()
  side = (float.x < f.x + f.w / 2) and "left" or "right"
  yfrac = clamp((float.y - f.y) / f.h, 0.08, 0.92)
  hs.settings.set("aiusage.side", side)
  hs.settings.set("aiusage.yfrac", yfrac)
  local tx, ty = dockedCenter(f, side)
  if spring then spring:stop() end
  spring = hs.timer.doEvery(1 / 60, function()
    float.vx = (float.vx + (tx - float.x) * 0.11) * 0.74
    float.vy = (float.vy + (ty - float.y) * 0.11) * 0.74
    float.x = float.x + float.vx
    float.y = float.y + float.vy
    local sp = math.sqrt(float.vx ^ 2 + float.vy ^ 2)
    local along = sp > 0.01 and math.abs(float.vx) / sp or 0
    local s = clamp(sp / 45, 0, 0.3)
    float.sx = 1 + s * along - s * 0.5 * (1 - along)
    float.sy = 1 + s * (1 - along) - s * 0.5 * along
    if math.abs(tx - float.x) < 0.8 and math.abs(ty - float.y) < 0.8 and sp < 0.8 then
      spring:stop(); spring = nil
      mode = "docked"; width = OPEN_W; drawnW = nil; fillet = 0
      local grow
      grow = hs.timer.doEvery(1 / 60, function()
        fillet = math.min(FILLET, fillet + 1.2)
        draw()
        if fillet >= FILLET then grow:stop() end
      end)
      draw()
      hovering = false
      settle()
      return
    end
    moveFloat()
  end)
end

local function beginDrag()
  press.dragging = true
  if closeTimer then closeTimer:stop(); closeTimer = nil end
  if anim then anim:stop(); anim = nil end
  tip:hide(); activeHoverId = nil
  mode = "floating"
  float.x, float.y = press.cx, press.cy
  float.vx, float.vy = 0, 0
  float.sx, float.sy = 1.08, 1.08
  press.offX, press.offY = press.x0 - press.cx, press.y0 - press.cy
  draw()
end

moveFloat = function()
  local cw, ch = OPEN_W + 2 * PAD, H + 8 + 2 * PAD
  notch:topLeft({ x = math.floor(float.x - cw / 2), y = math.floor(float.y - ch / 2) })
  local w, h = OPEN_W * float.sx, (H + 8) * float.sy
  notch[1].frame = { x = (cw - w) / 2, y = (ch - h) / 2, w = w, h = h }
end


---------------------------------------------------------------- 表示するものを選ぶ（出す・出さない・時間の幅・並べ替え・自分の項目）
-- 選んだ内容はこの Mac の Hammerspoon の設定（aiusage.prefs）に残る。自分の項目だけは config.json の "custom" に書く
local PREF_KEY = "aiusage.prefs"
local CONFIG_PATH = BASE .. "/config.json"
local function blankPrefs() return { hidden = {}, shown = {}, hiddenViews = {}, order = {} } end
local prefs = hs.settings.get(PREF_KEY)
if type(prefs) ~= "table" then prefs = blankPrefs() end
for k, v in pairs(blankPrefs()) do if type(prefs[k]) ~= "table" then prefs[k] = v end end
local function savePrefs() hs.settings.set(PREF_KEY, prefs) end

local function orderIndex(id)
  for i, v in ipairs(prefs.order) do if v == id then return i end end
  return nil
end
local function isVisible(it)
  return (it.present ~= false or prefs.shown[it.id] == true) and prefs.hidden[it.id] ~= true
end
local function sortedAll()
  local all = {}
  for i, it in ipairs(rawItems or {}) do all[#all + 1] = { it = it, pos = i } end
  table.sort(all, function(a, b)
    return (orderIndex(a.it.id) or (1000 + a.pos)) < (orderIndex(b.it.id) or (1000 + b.pos))
  end)
  return all
end

applyPrefs = function()
  if not rawItems then return end
  local list = {}
  for _, e in ipairs(sortedAll()) do
    local it = e.it
    if isVisible(it) then
      local hv = prefs.hiddenViews[it.id] or {}
      local vs = {}
      for _, v in ipairs(it.views or {}) do
        if hv[v.key] ~= true then table.insert(vs, v) end
      end
      if #vs == 0 and it.views and it.views[1] then vs = { it.views[1] } end
      local copy = {}
      for k, v in pairs(it) do copy[k] = v end
      copy.views = vs
      table.insert(list, copy)
    end
  end
  if #list == 0 then
    list = { { id = "none", label = L("なし", "None"), views = { { key = "none", sub = "", mode = "percent", center = "--",
      status = "none", tip1 = L("表示するものがありません", "Nothing to show"), tip2 = L("下の ••• から選んでください", "Pick items from ••• below") } } } }
  end
  items = list
  COUNT = #items
  H = calcH(COUNT)
end

local function readConfigFile()
  local f = io.open(CONFIG_PATH, "r") or io.open(BASE .. "/config.example.json", "r")
  if not f then return {} end
  local body = f:read("*a"); f:close()
  local ok, c = pcall(hs.json.decode, body)
  return (ok and type(c) == "table") and c or {}
end
local function writeConfigFile(c)
  if type(c.hide) == "table" and next(c.hide) == nil then c.hide = nil end
  hs.json.write(c, CONFIG_PATH, true, true)
end

local function addCustom()
  hs.focus()
  local next1 = L("次へ", "Next")
  local b1, name = hs.dialog.textPrompt(L("自分の項目を足す（1/2）", "Add your own item (1/2)"),
    L("丸の下に出す名前（短く。例: Cursor）", "Name shown under the ring (short, e.g. Cursor)"), "", next1, L("やめる", "Cancel"))
  if b1 ~= next1 or not name or name == "" then return end
  local add = L("足す", "Add")
  local b2, cmd = hs.dialog.textPrompt(L("自分の項目を足す（2/2）", "Add your own item (2/2)"),
    L("数字を出すコマンド。残り%の数字か、JSON を出してください（くわしくは説明書）。例: echo 42",
      "A shell command that prints the remaining % as a number, or JSON (see README). Example: echo 42"),
    "echo 42", add, L("やめる", "Cancel"))
  if b2 ~= add or not cmd or cmd == "" then return end
  local c = readConfigFile()
  if type(c.custom) ~= "table" then c.custom = {} end
  table.insert(c.custom, { label = name, command = cmd })
  writeConfigFile(c)
  refresh(true)
end

local function removeCustom(it)
  local c = readConfigFile()
  local keep = {}
  for _, x in ipairs(type(c.custom) == "table" and c.custom or {}) do
    if x.label ~= it.label then table.insert(keep, x) end
  end
  c.custom = keep
  writeConfigFile(c)
  prefs.hidden[it.id] = nil; prefs.hiddenViews[it.id] = nil
  savePrefs()
  refresh(true)
end

local menuBar = nil
local function openMenu()
  if not rawItems then refresh(true); return end
  local all = sortedAll()
  local ids = {}
  for _, e in ipairs(all) do ids[#ids + 1] = e.it.id end
  local function changed() savePrefs(); applyPrefs(); draw() end
  local function move(id, d)
    local i = nil
    for k, v in ipairs(ids) do if v == id then i = k end end
    if not i or not ids[i + d] then return end
    ids[i], ids[i + d] = ids[i + d], ids[i]
    prefs.order = ids
    changed()
  end
  local m = { { title = L("表示するもの", "Show"), disabled = true } }
  for idx, e in ipairs(all) do
    local it = e.it
    local visible = isVisible(it)
    local sub = {
      { title = L("表示する", "Show"), checked = visible, fn = function()
        if visible then
          prefs.hidden[it.id] = true; prefs.shown[it.id] = nil
        else
          prefs.hidden[it.id] = nil
          if it.present == false then prefs.shown[it.id] = true end
        end
        changed()
      end },
    }
    if type(it.views) == "table" and #it.views > 1 then
      table.insert(sub, { title = "-" })
      table.insert(sub, { title = L("出す時間の幅（押すと切り替わる順）", "Views (cycled by clicking)"), disabled = true })
      local hv = prefs.hiddenViews[it.id] or {}
      for _, v in ipairs(it.views) do
        local on = hv[v.key] ~= true
        table.insert(sub, { title = "   " .. ((v.sub and v.sub ~= "") and v.sub or v.key), checked = on, fn = function()
          prefs.hiddenViews[it.id] = prefs.hiddenViews[it.id] or {}
          prefs.hiddenViews[it.id][v.key] = on and true or nil
          viewIdx[it.id] = 1
          changed()
        end })
      end
    end
    table.insert(sub, { title = "-" })
    table.insert(sub, { title = L("上へ", "Move up"), disabled = idx == 1, fn = function() move(it.id, -1) end })
    table.insert(sub, { title = L("下へ", "Move down"), disabled = idx == #all, fn = function() move(it.id, 1) end })
    if it.custom then
      table.insert(sub, { title = "-" })
      table.insert(sub, { title = L("この項目を消す", "Remove this item"), fn = function() removeCustom(it) end })
    end
    local name = it.label .. ((it.present == false) and L("（この Mac に無い）", " (not found on this Mac)") or "")
    table.insert(m, { title = name, checked = visible, menu = sub })
  end
  table.insert(m, { title = "-" })
  table.insert(m, { title = L("自分の項目を足す…", "Add your own item…"), fn = function() hs.timer.doAfter(0.1, addCustom) end })
  table.insert(m, { title = L("設定ファイルを開く", "Open config.json"), fn = function()
    if not io.open(CONFIG_PATH, "r") then writeConfigFile(readConfigFile()) end
    hs.task.new("/usr/bin/open", nil, { "-t", CONFIG_PATH }):start()
  end })
  table.insert(m, { title = L("表示を最初に戻す", "Reset what is shown"), fn = function()
    prefs = blankPrefs()
    for k in pairs(viewIdx) do viewIdx[k] = 1 end
    changed()
  end })
  table.insert(m, { title = L("今すぐ新しくする", "Refresh now"), fn = function() refresh(true) end })
  menuBar = menuBar or hs.menubar.new(false)
  menuBar:setMenu(m)
  local nf = notch:frame()
  -- 右端なら出っぱりの左に、左端なら右に開く（メニューの幅はおよそ 240 点）
  local pt = side == "left" and { x = nf.x + nf.w + 6, y = nf.y + 24 } or { x = nf.x - 246, y = nf.y + 24 }
  tip:hide()
  menuBar:popupMenu(pt)
end

-- クリック: そのAIの時間軸を次へ切替。option+クリックで今すぐ最新値に更新。「•••」か ctrl+クリックで表示するものを選ぶ
local function click(id)
  if not id or id == "body" then return end
  if id == "gear" or hs.eventtap.checkKeyboardModifiers().ctrl then
    hs.timer.doAfter(0, openMenu)
    return
  end
  if hs.eventtap.checkKeyboardModifiers().alt then
    refresh(true)
    return
  end
  local _, it = itemCy(id)
  if not it or type(it.views) ~= "table" or #it.views <= 1 then
    refresh(true)
    return
  end
  local cur = viewIdx[id] or 1
  viewIdx[id] = (cur % #it.views) + 1
  draw()
  if mode == "docked" and width >= OPEN_W * 0.8 then
    activeHoverId = id
    showTipForId(id)
  end
end

local function onPress(id)
  if press and press.timer then press.timer:stop() end
  local m = hs.mouse.absolutePosition()
  local nf = notch:frame()
  press = { id = id, t0 = hs.timer.secondsSinceEpoch(), x0 = m.x, y0 = m.y, dragging = false }
  if mode == "docked" then
    press.cx = side == "left" and (nf.x + OPEN_W / 2) or (nf.x + nf.w - OPEN_W / 2)
    press.cy = nf.y + nf.h / 2
  else
    press.cx, press.cy = float.x, float.y
  end
  press.timer = hs.timer.doEvery(1 / 60, function()
    local p = press
    if not p then return end
    local held = hs.eventtap.checkMouseButtons().left
    local m2 = hs.mouse.absolutePosition()
    local moved = math.abs(m2.x - p.x0) + math.abs(m2.y - p.y0)
    if not held then
      p.timer:stop(); press = nil
      if p.dragging then snap() elseif hs.timer.secondsSinceEpoch() - p.t0 < 0.5 then click(p.id) end
      return
    end
    if not p.dragging and (moved >= 5 or hs.timer.secondsSinceEpoch() - p.t0 >= 0.25) then beginDrag() end
    if p.dragging then
      local nx, ny = m2.x - p.offX, m2.y - p.offY
      float.vx = float.vx * 0.5 + (nx - float.x) * 0.5
      float.vy = float.vy * 0.5 + (ny - float.y) * 0.5
      float.x, float.y = nx, ny
      local sp = math.sqrt(float.vx ^ 2 + float.vy ^ 2)
      local along = sp > 0.01 and math.abs(float.vx) / sp or 0
      local s = clamp(sp / 40, 0, 0.3)
      float.sx = 1.04 + s * along - s * 0.5 * (1 - along)
      float.sy = 1.04 + s * (1 - along) - s * 0.5 * along
      moveFloat()
    end
  end)
end

notch:level(hs.canvas.windowLevels.overlay)
notch:behavior({ "canJoinAllSpaces", "stationary", "fullScreenAuxiliary" })
notch:clickActivating(false)
notch:canvasMouseEvents(false, false, true, false)
tip:level(hs.canvas.windowLevels.overlay)
tip:behavior({ "canJoinAllSpaces", "stationary", "fullScreenAuxiliary" })
tip:clickActivating(false)

notch:mouseCallback(function(_, msg, id)
  if id == "_canvas_" then
    if mode ~= "docked" then return end
    if msg == "mouseEnter" then
      hovering = true
      refresh(false)
      settle()
    elseif msg == "mouseExit" then
      -- ここでは閉じない。閉じるかは端の見張りが広いゆとりで決める
      activeHoverId = nil
      tip:hide()
    end
    return
  end
  if msg == "mouseDown" then
    onPress(id)
  elseif msg == "mouseEnter" and not press then
    activeHoverId = id
    showTipForId(id)
  elseif msg == "mouseExit" then
    if activeHoverId == id then activeHoverId = nil end
    tip:hide()
  end
end)

local motion = hs.timer.doEvery(1 / 12, function()
  if loading then
    pulse = pulse + 0.45
    if mode == "docked" and width > REST_W + 1 then draw() end
  end
end)

local edgeWatch = hs.timer.doEvery(1 / 20, function()
  if mode ~= "docked" or press then return end
  local f = screenFrame()
  local m = hs.mouse.absolutePosition()
  if hovering then
    -- 外かどうかは「開ききった形」＋内側に広いゆとりで見る。開く途中の細い形で見ると、
    -- ボタンへ指を動かす途中で外と見て閉じていた（2026-11-19 オーナー「押そうとしたら引っ込むのが早すぎる」）
    local nf = notch:frame()
    local outX
    if side == "left" then
      outX = m.x < f.x - 4 or m.x > f.x + OPEN_W + 36
    else
      outX = m.x < f.x + f.w - OPEN_W - 36 or m.x > f.x + f.w + 4
    end
    if outX or m.y < nf.y - 24 or m.y > nf.y + nf.h + 24 then
      hovering = false
      activeHoverId = nil
      tip:hide()
      settle()
    end
    return
  end
  local T = H + 2 * FILLET
  local cy = centerY(f, T)
  if math.abs(m.y - cy) > T / 2 then return end
  local near = side == "left" and (m.x >= f.x and m.x <= f.x + 6) or (m.x <= f.x + f.w and m.x >= f.x + f.w - 6)
  if near then
    hovering = true
    refresh(false)
    settle()
  end
end)

local screenWatch = hs.screen.watcher.new(function() if mode == "docked" then draw() end end):start()
local autoRefresh = hs.timer.doEvery(REFRESH_SEC, function() refresh(true) end)

local pinTimer = nil
local function peek(sec)
  local now = hs.timer.secondsSinceEpoch()
  if pinnedUntil > now then
    pinnedUntil = 0
    if pinTimer then pinTimer:stop(); pinTimer = nil end
    settle()
    return
  end
  pinnedUntil = now + (sec or 5)
  refresh(false)
  settle()
  if pinTimer then pinTimer:stop() end
  pinTimer = hs.timer.doAfter(sec or 5, function()
    pinTimer = nil
    settle()
  end)
end

hs.hotkey.bind({ "ctrl", "alt", "cmd" }, "U", function() peek(5) end)
local rightTap = hs.eventtap.new({ hs.eventtap.event.types.rightMouseDown }, function(e)
  if mode ~= "docked" or width < OPEN_W * 0.8 then return false end
  local p = e:location()
  local nf = notch:frame()
  if p.x >= nf.x and p.x <= nf.x + nf.w and p.y >= nf.y and p.y <= nf.y + nf.h then
    hs.timer.doAfter(0, openMenu)
    return true
  end
  return false
end):start()
hs.urlevent.bind("ai-usage-refresh", function() refresh(true) end)
hs.urlevent.bind("ai-usage-peek", function(_, params) peek(tonumber(params and params.sec) or 5) end)

draw()
notch:show()
refresh(true)

M.refresh = function() refresh(true) end
M.peek = peek
M.cycle = click
M.items = function() return items end
M.frame = function() return notch:frame() end
M.debugShowTip = showTipForId
M.openMenu = openMenu
M._keep = { notch, tip, motion, edgeWatch, screenWatch, autoRefresh, rightTap }
return M
