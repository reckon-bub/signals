#!/usr/bin/env python3
"""
Сканер сетапов -> Telegram. Только сигналы, ордера — руками.

Два режима:
  H1  — контекст 4H, вход по закрытию 1H, риск 1R (0,5R для новых монет)
  M15 — контекст 1H (+4H не против), вход по закрытию 15m, риск 0,5R

Запуск каждые 15 минут (cron-job.org -> GitHub Actions). Стандартная библиотека Python.
"""
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ===== НАСТРОЙКИ =====
COINS = ["BTC", "ETH", "SOL", "LINK", "BNB", "XRP",
         "ADA", "AVAX", "DOT", "LTC", "BCH", "NEAR", "SUI", "ATOM"]
# Новые монеты — риск 0,5R до 26.10.2026. Потом убрать отсюда.
HALF_RISK = {"ADA", "AVAX", "DOT", "LTC", "BCH", "NEAR", "SUI", "ATOM"}

MODES = {
    # ctx — таймфрейм контекста, entry — таймфрейм входа
    "H1":  {"ctx": "c4", "entry": "c1",  "risk": 1.0, "label": "1H",  "cancel": "2 ч",
            "stop_bars": 24},
    "M15": {"ctx": "c1", "entry": "c15", "risk": 0.5, "label": "15m", "cancel": "1 ч",
            "stop_bars": 32},
}

RS_SHORT_BARS = 42        # сила vs BTC: 7 дней (42 свечи 4H)
RS_LONG_BARS = 180        # и 30 дней
ZONE_PCT = 0.004          # зона вокруг уровня: ±0,4%
ATR_MAX = 5.0             # ATR(14) 4H, % — выше монета «слишком нервная»
NEWS_BEFORE_H = 2         # Tier 1: не входим за 2 часа до выхода
NEWS_AFTER_MIN = 30       # и 30 минут после
DEFAULT_RISK = 4.68
DEFAULT_DEPOSIT = 468.0
MMR = 0.005               # поддерживающая маржа (оценка ликвидации)
STOP_BUFFER = 0.001       # +0,1% за свинг
MIN_RR = 2.0
COOLDOWN_H = 3            # после карточки — 3 часа тишины по входам
MAX_CARDS_DAY = 3         # не больше 3 карточек в день
STATUS_HOUR_VN = 7

AUTO_PIVOT = 3            # свинг = экстремум среди 3 свечей слева и 3 справа
AUTO_CLUSTER = 0.006      # свинги ближе 0,6% = один уровень
AUTO_MAX_DIST = 0.12      # уровни не дальше 12% от цены
AUTO_PER_SIDE = 2
AUTO_LOOKBACK = {"c4": 180, "c1": 168}   # 4H ~30 дней, 1H ~7 дней

VN = timezone(timedelta(hours=7))
API = "https://data-api.binance.vision/api/v3/klines"
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
LEVELS_CSV_URL = os.environ.get("LEVELS_CSV_URL", "")
NEWS_CSV_URL = os.environ.get("NEWS_CSV_URL", "")
MANUAL = os.environ.get("MANUAL", "false").lower() == "true"

# Шаг цены и количества на Bybit (USDT Perpetual). Если ордер не принимается — сверь в карточке контракта.
PRICE_DEC = {"BTC": 1, "ETH": 2, "SOL": 2, "LINK": 3, "BNB": 2, "XRP": 4,
             "ADA": 4, "AVAX": 3, "DOT": 3, "LTC": 2, "BCH": 2, "NEAR": 3, "SUI": 4, "ATOM": 3}
QTY_STEP = {"BTC": 0.001, "ETH": 0.01, "SOL": 0.1, "LINK": 0.1, "BNB": 0.01, "XRP": 1,
            "ADA": 1, "AVAX": 0.1, "DOT": 0.1, "LTC": 0.1, "BCH": 0.01, "NEAR": 0.1, "SUI": 10, "ATOM": 0.1}


# ===== СЕТЬ =====
def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signals-bot"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url[:80]}... не удался: {last}")


def _tg_post(text, html):
    params = {"chat_id": TG_CHAT_ID, "text": text[:4000], "disable_web_page_preview": "true"}
    if html:
        params["parse_mode"] = "HTML"
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                 data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def send_tg(text):
    """Отправка в Telegram. Если HTML не прошёл — тот же текст без разметки. Никогда не роняет бота."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("---- TG (нет токена) ----\n" + text + "\n")
        return
    try:
        _tg_post(text, True)
        return
    except urllib.error.HTTPError as e:
        print("TG HTML error:", e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:  # noqa
        print("TG error:", e)
    plain = re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    try:
        _tg_post(plain, False)
    except Exception as e:  # noqa
        print("TG plain error:", e)


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def klines(symbol, interval, limit):
    """Только ЗАКРЫТЫЕ свечи."""
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    raw = json.loads(http_get(f"{API}?{q}"))
    now_ms = int(time.time() * 1000)
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])}
            for k in raw if int(k[6]) < now_ms]


# ===== ТАБЛИЦА =====
def parse_num(s):
    s = str(s).strip().replace(" ", "").replace(" ", "").replace(" ", "").lstrip("'")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif s.count(",") == 1:
        s = s.replace(",", ".")
    elif s.count(",") > 1:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def load_csv(url):
    if not url:
        return []
    rows = []
    for r in csv.DictReader(io.StringIO(http_get(url))):
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items()})
    return rows


def load_levels():
    levels = {c: [] for c in COINS}
    settings = {"risk_usd": DEFAULT_RISK, "deposit": DEFAULT_DEPOSIT, "position_open": 0}
    for r in load_csv(LEVELS_CSV_URL):
        typ = r.get("type", "").lower()
        price = parse_num(r.get("price", ""))
        if price is None or not typ:
            continue
        if typ in settings:
            settings[typ] = price
            continue
        coin = r.get("coin", "").upper().replace("USDT", "").replace(".P", "")
        if coin in levels:
            levels[coin].append({"price": price, "type": typ})
    return levels, settings


def parse_dt(s):
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S",
              "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), f).replace(tzinfo=VN)
        except ValueError:
            pass
    return None


def load_news():
    news = []
    for r in load_csv(NEWS_CSV_URL):
        dt = parse_dt(r.get("datetime", ""))
        if dt:
            news.append({"dt": dt, "name": r.get("name", "новость")})
    return sorted(news, key=lambda x: x["dt"])


# ===== ИНДИКАТОРЫ =====
def ema(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out = [None] * (n - 1) + [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr_pct(c, n=14):
    if len(c) < n + 1:
        return None
    trs = [max(c[i]["h"] - c[i]["l"], abs(c[i]["h"] - c[i - 1]["c"]), abs(c[i]["l"] - c[i - 1]["c"]))
           for i in range(1, len(c))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a / c[-1]["c"] * 100


def trend(c):
    """long / short / flat по EMA 50/200; цена дальше 1 ATR за EMA 50 -> flat."""
    closes = [x["c"] for x in c]
    e50, e200 = ema(closes, 50)[-1], ema(closes, 200)[-1]
    if e50 is None or e200 is None:
        return "flat", None
    p = closes[-1]
    atr_abs = (atr_pct(c) or 0) * p / 100
    if p > e200 and e50 > e200 and p >= e50 - atr_abs:
        return "long", e50
    if p < e200 and e50 < e200 and p <= e50 + atr_abs:
        return "short", e50
    return "flat", e50


# ===== ПАТТЕРНЫ =====
def touched(c, L):
    return c["l"] <= L * (1 + ZONE_PCT) and c["h"] >= L * (1 - ZONE_PCT)


def bull_pattern(p, c):
    rng, body = c["h"] - c["l"], abs(c["c"] - c["o"])
    if p["c"] < p["o"] and c["c"] > c["o"] and c["c"] >= p["o"] and c["o"] <= p["c"] * 1.0005:
        return "бычье поглощение"
    if rng > 0:
        lower = min(c["o"], c["c"]) - c["l"]
        if lower >= 2 * body and lower >= 0.55 * rng and c["c"] >= c["l"] + 0.5 * rng:
            return "пин-бар"
    if p["c"] < p["o"] and c["c"] > p["h"]:
        return "разворот вверх"
    return None


def bear_pattern(p, c):
    rng, body = c["h"] - c["l"], abs(c["c"] - c["o"])
    if p["c"] > p["o"] and c["c"] < c["o"] and c["c"] <= p["o"] and c["o"] >= p["c"] * 0.9995:
        return "медвежье поглощение"
    if rng > 0:
        upper = c["h"] - max(c["o"], c["c"])
        if upper >= 2 * body and upper >= 0.55 * rng and c["c"] <= c["h"] - 0.5 * rng:
            return "пин-бар"
    if p["c"] > p["o"] and c["c"] < p["l"]:
        return "разворот вниз"
    return None


def breakout_state(c, L, up):
    """first / confirmed / failed на последней закрытой свече; held — пробой подтверждён и держится."""
    cl = [x["c"] for x in c]
    above = (lambda v: v > L) if up else (lambda v: v < L)
    ev = None
    if len(cl) >= 3:
        if above(cl[-1]) and not above(cl[-2]):
            ev = "first"
        elif above(cl[-1]) and above(cl[-2]) and not above(cl[-3]):
            ev = "confirmed"
        elif not above(cl[-1]) and above(cl[-2]) and not above(cl[-3]):
            ev = "failed"
    held = False
    for i in range(max(1, len(cl) - 30), len(cl) - 1):
        if not above(cl[i - 1]) and above(cl[i]) and above(cl[i + 1]):
            held = all(above(v) for v in cl[i:])
    return ev, held


# ===== АВТОУРОВНИ =====
def auto_levels(c, lookback):
    """Свинги -> кластеры (2+ касания) -> ближайшие поддержки/сопротивления. Возвращает (уровни, последний свинг-лоу)."""
    data = c[-lookback:]
    if len(data) < 2 * AUTO_PIVOT + 2:
        return [], None
    k, price = AUTO_PIVOT, data[-1]["c"]
    pivots = []
    for i in range(k, len(data) - k):
        win = data[i - k:i + k + 1]
        if data[i]["h"] == max(x["h"] for x in win):
            pivots.append((data[i]["h"], i, "H"))
        if data[i]["l"] == min(x["l"] for x in win):
            pivots.append((data[i]["l"], i, "L"))
    clusters = []
    for pr, i, kind in sorted(pivots):
        if clusters and abs(pr - clusters[-1]["mean"]) <= clusters[-1]["mean"] * AUTO_CLUSTER:
            cl = clusters[-1]
            cl["pts"].append(pr)
            cl["mean"] = sum(cl["pts"]) / len(cl["pts"])
        else:
            clusters.append({"pts": [pr], "mean": pr})
    lows = [(i, pr) for pr, i, kind in pivots if kind == "L" and pr < price]
    last_low = max(lows)[1] if lows else None
    out = []
    for side in ("support", "resistance"):
        if side == "support":
            cand = sorted([x for x in clusters if price * (1 - AUTO_MAX_DIST) < x["mean"] < price * 0.999],
                          key=lambda x: -x["mean"])
        else:
            cand = sorted([x for x in clusters if price * 1.001 < x["mean"] < price * (1 + AUTO_MAX_DIST)],
                          key=lambda x: x["mean"])
        strong = [x for x in cand if len(x["pts"]) >= 2][:AUTO_PER_SIDE]
        for x in strong:
            out.append({"price": round(x["mean"], 8), "type": side, "auto": True})
        if strong:
            out.append({"price": round(strong[0]["mean"], 8),
                        "type": "break_up" if side == "resistance" else "break_down", "auto": True})
    return out, last_low


def merge_levels(manual, auto):
    """Ручные уровни главнее: автоуровень ближе ±0,8% к уже добавленному того же типа — отбрасывается."""
    res = list(manual)
    for a in auto:
        if any(abs(a["price"] - m["price"]) <= m["price"] * ZONE_PCT * 2 and a["type"] == m["type"] for m in res):
            continue
        res.append(a)
    return res


# ===== ФОРМАТ =====
def fmt(x):
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.0f}".replace(",", " ")
    if a >= 100:
        return f"{x:.2f}"
    if a >= 10:
        return f"{x:.3f}"
    return f"{x:.4f}"


def px(coin, x):
    return f"{x:.{PRICE_DEC.get(coin, 4)}f}"


def chart(coin):
    return f'<a href="https://www.tradingview.com/chart/?symbol=BYBIT:{coin}USDT.P">график</a>'


# ===== РАСЧЁТ ОРДЕРА =====
def plan(coin, side, ce, cc, levels, L, ctx_atr_pct, settings, risk_mult, stop_bars):
    """Возвращает (ордер, None) или (None, причина отказа)."""
    price_now = ce[-1]["c"]
    gap_max = price_now * ctx_atr_pct / 100 * 0.5
    entry = price_now if abs(price_now - L) <= gap_max else L
    recent = ce[-stop_bars:]
    if side == "long":
        lows = [x["l"] for x in recent if x["l"] <= L * (1 + ZONE_PCT)] + [x["l"] for x in ce[-6:]]
        stop = min(lows) * (1 - STOP_BUFFER)
    else:
        highs = [x["h"] for x in recent if x["h"] >= L * (1 - ZONE_PCT)] + [x["h"] for x in ce[-6:]]
        stop = max(highs) * (1 + STOP_BUFFER)
    min_d = entry * ctx_atr_pct / 100 * 0.5
    if side == "long":
        stop = min(stop, entry - min_d)
        swing = max(x["h"] for x in cc[-48:])          # последний хай контекста — первое препятствие
        targets = sorted([x["price"] for x in levels] + [swing])
        targets = [t for t in targets if t > entry * 1.003]
    else:
        stop = max(stop, entry + min_d)
        swing = min(x["l"] for x in cc[-48:])
        targets = sorted([x["price"] for x in levels] + [swing], reverse=True)
        targets = [t for t in targets if t < entry * 0.997]
    if not targets:
        return None, "нет цели"
    target = targets[0]
    dist = abs(entry - stop)
    rr = (abs(target - entry) - 0.1 * dist) / dist   # с комиссией ~0,1R
    if rr < MIN_RR:
        return None, f"R:R {rr:.1f}"
    risk = settings["risk_usd"] * risk_mult
    step = QTY_STEP.get(coin, 0.001)
    qty = int(risk / dist / step) * step
    if qty <= 0:
        return None, "объём меньше лота"
    notional = qty * entry
    lev = 3
    if notional / lev > settings["deposit"] * 0.9:
        lev = 5
        if notional / lev > settings["deposit"] * 0.9:
            return None, "маржа не влезает"
    liq = entry * (1 - 1 / lev + MMR) if side == "long" else entry * (1 + 1 / lev - MMR)
    if (side == "long" and liq >= stop - dist) or (side == "short" and liq <= stop + dist):
        return None, "ликвидация близко"
    dec = 0 if step >= 1 else len(f"{step:.10f}".rstrip("0").split(".")[1])
    return {"entry": entry, "stop": stop, "target": target, "qty": f"{qty:.{dec}f}", "lev": lev,
            "risk": qty * dist, "rr": rr, "price_now": price_now, "chased": entry != price_now}, None


def order_msg(coin, side, mode, o, patt, rs_tag, risk_mult):
    m = MODES[mode]
    side_txt = "ЛОНГ 🟩" if side == "long" else "ШОРТ 🟥"
    why = "Откат к поддержке" if side == "long" else "Откат к сопротивлению"
    strength = {"strong": ", сильнее BTC", "weak": ", слабее BTC"}.get(rs_tag, "")
    risk_txt = f"${o['risk']:.2f}" + (" (0,5R)" if risk_mult < 1 else "")
    wait = f"\nЦена сейчас {px(coin, o['price_now'])} — ждём отката к лимитке." if o["chased"] else ""
    return (f"🟢 <b>{coin} · {side_txt} · {m['label']}</b>\n"
            f"Лимитка <code>{px(coin, o['entry'])}</code> · стоп <code>{px(coin, o['stop'])}</code> · "
            f"тейк <code>{px(coin, o['target'])}</code>\n"
            f"<code>{o['qty']}</code> {coin} · {o['lev']}x · риск {risk_txt} · R:R {o['rr']:.1f}\n"
            f"{why}, {patt}{strength}.{wait}\n"
            f"Не исполнилась за {m['cancel']} — отмени. Стоп сверь с хвостами на Bybit. {chart(coin)}")


# ===== СОСТОЯНИЕ =====
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


# ===== ОСНОВНОЙ ЦИКЛ =====
def main():
    now = datetime.now(VN)
    st = load_state()
    seen = st.setdefault("seen", {})
    st.setdefault("news_warned", [])
    for old in ("last_1h", "last_4h"):
        st.pop(old, None)

    manual_levels, settings = load_levels()
    news = load_news()

    blackout = next((n for n in news if n["dt"] - timedelta(hours=NEWS_BEFORE_H) <= now
                     <= n["dt"] + timedelta(minutes=NEWS_AFTER_MIN)), None)

    # напоминание о новости за 2–3 часа
    for n in news:
        key = n["dt"].strftime("%Y-%m-%d %H:%M") + n["name"]
        if n["dt"] - timedelta(hours=3) <= now < n["dt"] - timedelta(hours=NEWS_BEFORE_H) and key not in st["news_warned"]:
            send_tg(f"⏰ <b>{esc(n['name'])}</b> в {n['dt']:%H:%M}. "
                    f"С {(n['dt'] - timedelta(hours=NEWS_BEFORE_H)):%H:%M} не входим. "
                    f"Открытая позиция — стоп в безубыток или закрыть.")
            st["news_warned"].append(key)
    st["news_warned"] = st["news_warned"][-30:]

    # данные
    data, failed = {}, []
    for coin in COINS:
        sym = coin + "USDT"
        try:
            data[coin] = {"c4": klines(sym, "4h", 1000), "c1": klines(sym, "1h", 400),
                          "c15": klines(sym, "15m", 150)}
        except Exception as e:  # noqa
            print("fetch fail", coin, e)
            failed.append(coin)
    if "BTC" not in data:
        raise RuntimeError("нет данных по BTC")

    # BTC-фильтр
    btc_manual = [x for x in manual_levels["BTC"] if x["type"] == "btc_filter"]
    btc_c4 = data["BTC"]["c4"]
    btc_L = btc_manual[0]["price"] if btc_manual else auto_levels(btc_c4, AUTO_LOOKBACK["c4"])[1]
    btc_ok = (btc_c4[-1]["c"] > btc_L) if btc_L else None

    def rs(coin):
        c4 = data[coin]["c4"]
        if coin == "BTC" or len(c4) <= RS_LONG_BARS or len(btc_c4) <= RS_LONG_BARS:
            return "base"
        r = [((c4[-1]["c"] / c4[-1 - n]["c"]) / (btc_c4[-1]["c"] / btc_c4[-1 - n]["c"]) - 1) * 100
             for n in (RS_SHORT_BARS, RS_LONG_BARS)]
        return "strong" if r[0] > 0 and r[1] > 0 else "weak" if r[0] < 0 and r[1] < 0 else "mixed"

    summary = {"strong": [], "weak": []}
    candidates = []
    for coin, d in data.items():
        if len(d["c4"]) < 210 or len(d["c1"]) < 210 or len(d["c15"]) < 40:
            continue
        rs_tag = rs(coin)
        if rs_tag in summary:
            summary[rs_tag].append(coin)
        atr4 = atr_pct(d["c4"])
        tr4, _ = trend(d["c4"])

        # уровни: H1 — ручные + авто 4H; M15 — ещё и авто 1H
        lv_h1 = merge_levels(manual_levels[coin], auto_levels(d["c4"], AUTO_LOOKBACK["c4"])[0])
        lv_m15 = merge_levels(lv_h1, auto_levels(d["c1"], AUTO_LOOKBACK["c1"])[0])

        # пробои 4H: сообщаем только подтверждённые и только по тренду
        k4 = f"4H:{coin}"
        new_4h = d["c4"][-1]["t"] > seen.get(k4, 0)
        retests = []
        for x in lv_h1:
            if x["type"] not in ("break_up", "break_down"):
                continue
            up = x["type"] == "break_up"
            ev, held = breakout_state(d["c4"], x["price"], up)
            if held:
                retests.append({"price": x["price"], "type": "support" if up else "resistance"})
            if new_4h and ev == "confirmed" and ((up and tr4 == "long") or (not up and tr4 == "short")):
                send_tg(f"✅ <b>{coin}</b>: пробой {fmt(x['price'])} {'вверх' if up else 'вниз'} подтверждён. "
                        f"Ждём ретест — вход пришлю отдельно. {chart(coin)}")
        seen[k4] = d["c4"][-1]["t"]

        for mode, m in MODES.items():
            ce, cc = d[m["entry"]], d[m["ctx"]]
            key = f"{mode}:{coin}"
            if ce[-1]["t"] <= seen.get(key, 0):
                continue
            seen[key] = ce[-1]["t"]

            tr_ctx, e50_ctx = trend(cc)
            if tr_ctx not in ("long", "short"):
                continue
            if mode == "M15" and tr4 in ("long", "short") and tr4 != tr_ctx:
                continue          # 15m не торгуем против 4H
            side = tr_ctx
            if blackout or (atr4 or 99) > ATR_MAX:
                continue
            if side == "long" and (rs_tag == "weak" or (coin != "BTC" and btc_ok is False)):
                continue
            if side == "short" and rs_tag == "strong":
                continue
            if mode == "M15" and not ((side == "long" and rs_tag == "strong") or
                                      (side == "short" and rs_tag == "weak")):
                continue          # 15m — только самые сильные (лонг) / слабые (шорт)

            levels = lv_h1 if mode == "H1" else lv_m15
            zone_type = "support" if side == "long" else "resistance"
            zones = [x["price"] for x in levels + retests if x["type"] == zone_type]
            if e50_ctx:
                zones.append(e50_ctx)
            p, c = ce[-2], ce[-1]
            risk_mult = m["risk"] * (0.5 if coin in HALF_RISK and m["risk"] >= 1 else 1)
            for L in sorted(set(zones), key=lambda z: abs(z - c["c"])):
                if not (touched(c, L) or touched(p, L)):
                    continue
                if side == "long":
                    patt = bull_pattern(p, c) if c["c"] >= L * (1 - ZONE_PCT) else None
                else:
                    patt = bear_pattern(p, c) if c["c"] <= L * (1 + ZONE_PCT) else None
                if not patt:
                    continue
                o, why = plan(coin, side, ce, cc, levels, L, atr_pct(cc), settings, risk_mult, m["stop_bars"])
                if o:
                    score = (100 if mode == "H1" else 0) + min(o["rr"], 5) * 10 + (5 if rs_tag in ("strong", "weak") else 0)
                    candidates.append((score, coin, side, mode, o, patt, rs_tag, risk_mult))
                else:
                    print(f"skip {coin} {mode} {side} @ {L}: {why}")
                break     # одна попытка на монету/режим за свечу

    # одна лучшая карточка за запуск + пауза + дневной лимит + выключатель «позиция открыта»
    if candidates:
        today_key = now.strftime("%Y-%m-%d")
        day = st.setdefault("cards_day", {})
        if today_key not in day:
            day.clear()
            day[today_key] = 0
        last_card = st.get("last_card_ts", 0)
        cooling = time.time() - last_card < COOLDOWN_H * 3600
        if settings.get("position_open", 0) >= 1:
            print("position_open=1 — карточки не шлём")
        elif cooling:
            print("пауза после прошлой карточки")
        elif day[today_key] >= MAX_CARDS_DAY:
            print("дневной лимит карточек")
        else:
            candidates.sort(key=lambda x: -x[0])
            score, coin, side, mode, o, patt, rs_tag, risk_mult = candidates[0]
            msg = order_msg(coin, side, mode, o, patt, rs_tag, risk_mult)
            others = sorted({x[1] for x in candidates[1:]} - {coin})
            if others:
                msg += f"\n<i>Похожие сигналы ({', '.join(others)}) — пропускаем: одна позиция.</i>"
            send_tg(msg)
            st["last_card_ts"] = time.time()
            day[today_key] += 1

    # статус раз в день (и при ручном запуске)
    today = now.strftime("%Y-%m-%d")
    if MANUAL or (now.hour >= STATUS_HOUR_VN and st.get("status_date") != today):
        if btc_L:
            btc_txt = f"BTC {fmt(btc_c4[-1]['c'])} · фильтр {fmt(btc_L)} " + \
                      ("✅" if btc_ok else "⛔ лонги по альтам выкл")
        else:
            btc_txt = f"BTC {fmt(btc_c4[-1]['c'])}"
        upcoming = [n for n in news if now <= n["dt"] <= now + timedelta(hours=36)]
        news_txt = "; ".join(f"{n['dt']:%d.%m %H:%M} {esc(n['name'])}" for n in upcoming) or "нет"
        send_tg(f"🤖 <b>Бот работает · {now:%d.%m %H:%M}</b>\n"
                f"{btc_txt}\n"
                f"💪 Сильнее BTC: {', '.join(summary['strong']) or '—'}\n"
                f"🥀 Слабее BTC: {', '.join(summary['weak']) or '—'}\n"
                f"📰 Новости 36 ч: {news_txt}\n"
                f"Риск ${settings['risk_usd']:.2f} · депозит ${settings['deposit']:.0f}"
                + (f"\n⚠️ Нет данных: {', '.join(failed)}" if failed else ""))
        if not MANUAL:
            st["status_date"] = today

    save_state(st)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa
        try:
            send_tg(f"⚠️ Бот упал: {esc(type(e).__name__)}: {esc(e)}")
        finally:
            raise
