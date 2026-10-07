#!/usr/bin/env python3
"""
Сканер сетапов 4H + 1H -> Telegram.
Только сигналы. Решение и ордера — руками, по чек-листу.

Запускается раз в час (GitHub Actions). Стандартная библиотека Python, без установки пакетов.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ===== НАСТРОЙКИ =====
COINS = ["BTC", "ETH", "SOL", "LINK", "BNB", "XRP"]
ZONE_PCT = 0.004          # ширина зоны вокруг уровня: ±0,4%
ATR_MAX = 5.0             # ATR(14) 4H в % от цены — выше этого монета «слишком нервная»
NEWS_BEFORE_H = 2         # Tier 1: не входим за 2 часа до выхода
NEWS_AFTER_MIN = 30       # и 30 минут после
DEFAULT_RISK = 4.55       # если в таблице нет строки risk_usd
DEFAULT_DEPOSIT = 455.0   # если в таблице нет строки deposit
MMR = 0.005               # поддерживающая маржа (оценка для цены ликвидации)
SWING_BARS = 6            # стоп за экстремумом последних N свечей 1H
STOP_BUFFER = 0.001       # +0,1% за свинг
STATUS_HOUR_VN = 7        # во сколько присылать утренний статус
EMA50_ZONE = True         # EMA 50 (4H) как динамическая зона: поддержка в лонг-контексте, сопротивление в шорт-контексте
MAX_TRADES_DAY = 2        # напоминание в сигналах

VN = timezone(timedelta(hours=7))
API = "https://data-api.binance.vision/api/v3/klines"
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
LEVELS_CSV_URL = os.environ.get("LEVELS_CSV_URL", "")
NEWS_CSV_URL = os.environ.get("NEWS_CSV_URL", "")
MANUAL = os.environ.get("MANUAL", "false").lower() == "true"


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


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("---- TG (нет токена) ----\n" + text + "\n")
        return
    data = urllib.parse.urlencode({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def klines(symbol, interval, limit):
    """Только ЗАКРЫТЫЕ свечи."""
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    raw = json.loads(http_get(f"{API}?{q}"))
    now_ms = int(time.time() * 1000)
    out = []
    for k in raw:
        if int(k[6]) >= now_ms:
            continue
        out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4])})
    return out


# ===== ТАБЛИЦА =====
def parse_num(s):
    s = str(s).strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
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
    text = http_get(url)
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items()})
    return rows


def load_levels():
    levels = {c: [] for c in COINS}
    settings = {"risk_usd": DEFAULT_RISK, "deposit": DEFAULT_DEPOSIT}
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
            levels[coin].append({"price": price, "type": typ, "comment": r.get("comment", "")})
    return levels, settings


def parse_dt(s):
    s = s.strip()
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S",
              "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=VN)
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


def atr_pct(c4, n=14):
    if len(c4) < n + 1:
        return None
    trs = []
    for i in range(1, len(c4)):
        h, l, pc = c4[i]["h"], c4[i]["l"], c4[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a / c4[-1]["c"] * 100


def trend_4h(c4):
    closes = [x["c"] for x in c4]
    e50, e200 = ema(closes, 50)[-1], ema(closes, 200)[-1]
    c = closes[-1]
    if e50 is None or e200 is None:
        return "?", e50, e200
    if c > e200 and e50 > e200:
        return "long", e50, e200
    if c < e200 and e50 < e200:
        return "short", e50, e200
    return "flat", e50, e200


TREND_TXT = {"long": "📈 лонг-контекст", "short": "📉 шорт-контекст",
             "flat": "↔️ неясно (EMA перепутаны)", "?": "?"}


# ===== ПАТТЕРНЫ 1H =====
def touched(c, L):
    return c["l"] <= L * (1 + ZONE_PCT) and c["h"] >= L * (1 - ZONE_PCT)


def bull_pattern(p, c):
    rng = c["h"] - c["l"]
    body = abs(c["c"] - c["o"])
    if p["c"] < p["o"] and c["c"] > c["o"] and c["c"] >= p["o"] and c["o"] <= p["c"] * 1.0005:
        return "бычье поглощение"
    if rng > 0:
        lower = min(c["o"], c["c"]) - c["l"]
        if lower >= 2 * body and lower >= 0.55 * rng and c["c"] >= c["l"] + 0.5 * rng:
            return "пин-бар (хвост вниз)"
    if p["c"] < p["o"] and c["c"] > p["h"]:
        return "закрытие выше хая красной свечи"
    return None


def bear_pattern(p, c):
    rng = c["h"] - c["l"]
    body = abs(c["c"] - c["o"])
    if p["c"] > p["o"] and c["c"] < c["o"] and c["c"] <= p["o"] and c["o"] >= p["c"] * 0.9995:
        return "медвежье поглощение"
    if rng > 0:
        upper = c["h"] - max(c["o"], c["c"])
        if upper >= 2 * body and upper >= 0.55 * rng and c["c"] <= c["h"] - 0.5 * rng:
            return "пин-бар (хвост вверх)"
    if p["c"] > p["o"] and c["c"] < p["l"]:
        return "закрытие ниже лоя зелёной свечи"
    return None


# ===== ПРОБОИ 4H =====
def breakout_state(c4, L, up):
    """first / confirmed / failed для последней закрытой 4H; held — пробой подтверждён и держится."""
    cl = [x["c"] for x in c4]
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


# ===== РАСЧЁТ ПОЗИЦИИ =====
def fmt(x):
    if x is None:
        return "—"
    a = abs(x)
    if a >= 1000:
        s = f"{x:,.1f}".replace(",", " ")
    elif a >= 100:
        s = f"{x:.2f}"
    elif a >= 10:
        s = f"{x:.3f}"
    else:
        s = f"{x:.4f}"
    return s


# Шаг цены и количества на Bybit (USDT Perpetual). Проверь в карточке контракта, если ордер не принимается.
PRICE_DEC = {"BTC": 1, "ETH": 2, "SOL": 2, "LINK": 3, "BNB": 2, "XRP": 4}
QTY_STEP = {"BTC": 0.001, "ETH": 0.01, "SOL": 0.1, "LINK": 0.1, "BNB": 0.01, "XRP": 1}


def px(coin, x):
    """Цена без пробелов — чтобы копировать прямо в Bybit."""
    return f"{x:.{PRICE_DEC.get(coin, 4)}f}"


def plan(coin, side, c1, all_levels, settings, blockers):
    """Карточка ордера. Если есть хоть один блокер — вместо карточки «ПРОПУСКАЕМ»."""
    blockers = list(blockers)
    entry = c1[-1]["c"]
    last = c1[-SWING_BARS:]
    if side == "long":
        stop = min(x["l"] for x in last) * (1 - STOP_BUFFER)
        targets = sorted(l["price"] for l in all_levels if l["price"] > entry * 1.003)
    else:
        stop = max(x["h"] for x in last) * (1 + STOP_BUFFER)
        targets = sorted((l["price"] for l in all_levels if l["price"] < entry * 0.997), reverse=True)
    target = targets[0] if targets else None
    dist = abs(entry - stop)
    if dist <= 0:
        return "⛔ Стоп не посчитался — смотри график."

    risk = settings["risk_usd"]
    dep = settings["deposit"]
    step = QTY_STEP.get(coin, 0.001)
    qty = int(risk / dist / step) * step
    if qty <= 0:
        blockers.append("объём меньше минимального лота")
    risk_real = qty * dist
    notional = qty * entry
    lev = 3
    margin = notional / lev
    if margin > dep * 0.9:
        lev = 5
        margin = notional / lev
        if margin > dep * 0.9:
            blockers.append("маржа не влезает даже при 5x — стоп слишком близко")
    if side == "long":
        liq = entry * (1 - 1 / lev + MMR)
        liq_ok = liq < stop - dist
    else:
        liq = entry * (1 + 1 / lev - MMR)
        liq_ok = liq > stop + dist
    if not liq_ok:
        blockers.append("ликвидация близко к стопу")

    rr = abs(target - entry) / dist if target else None
    rr_net = (abs(target - entry) - 0.1 * dist) / dist if target else None  # комиссии ~0,1R
    if target is None:
        blockers.append("нет цели в таблице по направлению сделки")
    elif rr_net < 2:
        blockers.append(f"R:R {rr_net:.1f} с комиссией — меньше 2")

    if blockers:
        return ("⛔ <b>ПРОПУСКАЕМ</b>\n" + "\n".join(f"• {b}" for b in blockers)
                + f"\n<i>Для справки: вход ~{fmt(entry)}, стоп {fmt(stop)}, тейк {fmt(target)}</i>")

    side_ru = "🟩 ЛОНГ (Buy)" if side == "long" else "🟥 ШОРТ (Sell)"
    qty_txt = f"{qty:.{max(0, len(str(step).split('.')[-1]) if step < 1 else 0)}f}"
    return "\n".join([
        f"📋 <b>ОРДЕР {coin}USDT — {side_ru}</b>",
        f"Вход (лимит):  <code>{px(coin, entry)}</code>",
        f"Стоп-лосс:      <code>{px(coin, stop)}</code>  (−{dist / entry * 100:.2f}%)",
        f"Тейк-профит:  <code>{px(coin, target)}</code>",
        f"Кол-во:            <code>{qty_txt}</code> {coin}  (≈${notional:.0f})",
        f"Плечо:             <b>{lev}x Isolated</b>, маржа ≈ ${margin:.0f}",
        f"Риск ${risk_real:.2f} | R:R {rr:.1f} (с комиссией {rr_net:.1f}) | ликв. ≈ {fmt(liq)}",
        "",
        "✔️ TP/SL ставь прямо в ордере, после исполнения проверь их в карточке позиции.",
        "⚠️ Цифры механические: сверь стоп со свингом и цель с уровнем 4H на своём графике.",
    ])


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


def chart(coin):
    return f'<a href="https://www.tradingview.com/chart/?symbol=BYBIT:{coin}USDT.P">📊 график</a>'


# ===== ОСНОВНОЙ ЦИКЛ =====
def main():
    now = datetime.now(VN)
    st = load_state()
    st.setdefault("last_1h", {})
    st.setdefault("last_4h", {})
    st.setdefault("news_warned", [])

    levels, settings = load_levels()
    news = load_news()

    # новостное окно
    blackout = None
    for n in news:
        if n["dt"] - timedelta(hours=NEWS_BEFORE_H) <= now <= n["dt"] + timedelta(minutes=NEWS_AFTER_MIN):
            blackout = n
            break
    news_line = (f"⛔ Новостное окно: {n_esc(blackout['name'])} в {blackout['dt']:%H:%M} — НЕ входим"
                 if blackout else "📰 Новости: окна Tier 1 нет ✅")

    # предупреждение за 2–3 часа до Tier 1
    for n in news:
        key = n["dt"].strftime("%Y-%m-%d %H:%M") + n["name"]
        if n["dt"] - timedelta(hours=3) <= now < n["dt"] - timedelta(hours=NEWS_BEFORE_H) and key not in st["news_warned"]:
            send_tg(f"⏰ <b>{n_esc(n['name'])}</b> в {n['dt']:%d.%m %H:%M}\n"
                    f"С {(n['dt'] - timedelta(hours=NEWS_BEFORE_H)):%H:%M} новых входов нет.\n"
                    f"Открытая позиция к релизу — закрыта или стоп в безубытке.")
            st["news_warned"].append(key)
    st["news_warned"] = st["news_warned"][-30:]

    data = {}
    for coin in COINS:
        sym = coin + "USDT"
        data[coin] = {"c4": klines(sym, "4h", 1000), "c1": klines(sym, "1h", 100)}

    # фильтр BTC для альтов
    btc_f = [l for l in levels["BTC"] if l["type"] == "btc_filter"]
    btc_close4 = data["BTC"]["c4"][-1]["c"]
    if btc_f:
        L = btc_f[0]["price"]
        btc_ok = btc_close4 > L
        btc_line = (f"BTC 4H закрыт {fmt(btc_close4)} > {fmt(L)} ✅" if btc_ok
                    else f"⛔ BTC 4H закрыт {fmt(btc_close4)} < {fmt(L)} — лонги по альтам запрещены")
    else:
        btc_ok, btc_line = None, "BTC-фильтр не задан в таблице (строка btc_filter)"

    status_rows = []
    for coin in COINS:
        c4, c1 = data[coin]["c4"], data[coin]["c1"]
        if len(c4) < 210 or len(c1) < SWING_BARS + 2:
            continue
        sym = coin + "USDT"
        tr, e50, e200 = trend_4h(c4)
        atr = atr_pct(c4)
        atr_line = f"ATR 4H {atr:.1f}% " + ("✅" if atr <= ATR_MAX else "⛔ слишком нервная")
        lv = levels[coin]
        new_1h = c1[-1]["t"] > st["last_1h"].get(sym, 0)
        new_4h = c4[-1]["t"] > st["last_4h"].get(sym, 0)
        events = []

        supports = [l for l in lv if l["type"] == "support"]
        resists = [l for l in lv if l["type"] == "resistance"]

        # --- EMA 50 (4H) как динамическая зона, только по направлению 4H ---
        if EMA50_ZONE and e50:
            tol = e50 * ZONE_PCT * 2
            if tr == "long" and not any(abs(l["price"] - e50) <= tol for l in supports):
                supports.append({"price": e50, "type": "ema50", "comment": ""})
            elif tr == "short" and not any(abs(l["price"] - e50) <= tol for l in resists):
                resists.append({"price": e50, "type": "ema50", "comment": ""})

        # --- пробои на закрытии 4H ---
        for l in lv:
            if l["type"] not in ("break_up", "break_down"):
                continue
            up = l["type"] == "break_up"
            ev, held = breakout_state(c4, l["price"], up)
            word = "выше" if up else "ниже"
            if new_4h and ev == "first":
                events.append(f"🔵 <b>ПРОБОЙ?</b> 4H закрылась {word} {fmt(l['price'])}.\n"
                              f"Ждём следующую 4H: если не закроется обратно — пробой засчитан. Сейчас НЕ входим.")
            elif new_4h and ev == "confirmed":
                events.append(f"✅ <b>ПРОБОЙ ПОДТВЕРЖДЁН</b> {fmt(l['price'])} (2 закрытия 4H {word}).\n"
                              f"Ждём ретест уровня + подтверждение на 1H. Без ретеста — пропускаем.")
            elif new_4h and ev == "failed":
                events.append(f"❌ <b>Пробой {fmt(l['price'])} не удержали</b> — 4H закрылась обратно. Сценарий отменён.")
            if held:
                (supports if up else resists).append({"price": l["price"], "type": "retest", "comment": "ретест пробоя"})

        # --- зоны и подтверждения на закрытии 1H ---
        if new_1h:
            p, c = c1[-2], c1[-1]
            for side, group in (("long", supports), ("short", resists)):
                for l in group:
                    L = l["price"]
                    hit_now, hit_prev = touched(c, L), touched(p, L)
                    label = {"retest": "РЕТЕСТ", "ema50": "EMA 50 (4H)"}.get(l["type"], "ЗОНА")
                    side_txt = "лонг" if side == "long" else "шорт"
                    if hit_now and not hit_prev:
                        events.append(f"🟡 <b>{label} {fmt(L)}</b> ({side_txt}) — цена в зоне. Жди подтверждение на 1H.")
                    if not (hit_now or hit_prev):
                        continue
                    if side == "long":
                        patt = bull_pattern(p, c) if c["c"] >= L * (1 - ZONE_PCT) else None
                    else:
                        patt = bear_pattern(p, c) if c["c"] <= L * (1 + ZONE_PCT) else None
                    if patt:
                        blockers = []
                        if tr != side:
                            blockers.append("против 4H-контекста")
                        if side == "long" and coin != "BTC" and btc_ok is False:
                            blockers.append("BTC не держит поддержку — лонг по альту запрещён")
                        if atr > ATR_MAX:
                            blockers.append(f"ATR {atr:.1f}% — слишком нервная")
                        if blackout:
                            blockers.append(f"новостное окно: {n_esc(blackout['name'])}")
                        where = (label + " ") if l["type"] == "ema50" else ""
                        events.append(
                            f"🟢 <b>ПОДТВЕРЖДЕНИЕ 1H</b> ({side_txt}) у {where}{fmt(L)}: {patt}\n\n"
                            + plan(coin, side, c1, lv, settings, blockers))

        if events:
            head = f"<b>{coin}</b> {fmt(c1[-1]['c'])} | {TREND_TXT[tr]}\n{atr_line}\n"
            if coin != "BTC":
                head += btc_line + "\n"
            head += news_line + "\n\n"
            tail = (f"\n\n☝️ Одна позиция на все монеты, максимум {MAX_TRADES_DAY} сделки в день, дневной стоп −2R.\n"
                    f"Решение — по чек-листу и скринам. {chart(coin)}")
            send_tg(head + "\n\n".join(events) + tail)

        st["last_1h"][sym] = c1[-1]["t"]
        st["last_4h"][sym] = c4[-1]["t"]

        near_s = [l["price"] for l in supports if l["price"] < c1[-1]["c"]]
        near_txt = ""
        if near_s:
            ns = max(near_s)
            near_txt = f", до поддержки {fmt(ns)}: {(c1[-1]['c'] - ns) / c1[-1]['c'] * 100:.1f}%"
        status_rows.append(f"<b>{coin}</b> {fmt(c1[-1]['c'])} — {TREND_TXT[tr]}, ATR {atr:.1f}%{near_txt}")

    # утренний статус / ручной запуск
    today = now.strftime("%Y-%m-%d")
    if MANUAL or (now.hour == STATUS_HOUR_VN and st.get("status_date") != today):
        upcoming = [n for n in news if now <= n["dt"] <= now + timedelta(hours=36)]
        nl = "\n".join(f"• {n['dt']:%d.%m %H:%M} — {n_esc(n['name'])}" for n in upcoming) or "• Tier 1 в ближайшие 36 ч нет"
        send_tg(f"🤖 <b>Статус {now:%d.%m %H:%M}</b>"
                + (" (ручной запуск)" if MANUAL else "") + "\n\n"
                + "\n".join(status_rows) + f"\n\n{btc_line}\n\n<b>Новости:</b>\n{nl}\n\n"
                f"Риск ${settings['risk_usd']:.2f} | депозит ${settings['deposit']:.0f}")
        if not MANUAL:
            st["status_date"] = today

    save_state(st)


def n_esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa
        try:
            send_tg(f"⚠️ Бот сигналов упал: {n_esc(e)}")
        finally:
            raise
