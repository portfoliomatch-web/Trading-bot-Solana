import requests
import time
import os
import hmac
import hashlib
import json
from dotenv import load_dotenv
from datetime import datetime 

load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")



# =============================
# STATE
# =============================
trading_active = True
last_update_id = None

last_buy_price = None
highest_price = None  # ✅ FIXED: Initialize this variable (was undefined)
buy_time = None  # ✅ NEW: Track when we entered position
market_mode = "neutraal"

# ✅ IMPROVED PARAMETERS FOR SWING TRADING
STOP_LOSS_PERCENT     = 0.08    # ✅ CHANGED: 8% stop loss (was 12% - tighter for swing trading)
TAKE_PROFIT_PERCENT   = 0.07    # ✅ NEW: 7% take profit target
RSI_BUY_THRESHOLD     = 35      # ✅ CHANGED: RSI < 35 (was 40 - stricter entry)
RSI_SELL_THRESHOLD    = 65      # ✅ NEW: RSI > 65 in bearish = sell
MAX_HOLD_DAYS         = 5       # ✅ NEW: Max 5 days holding
FEE                   = 0.0025  # Bitvavo taker fee 0.25%

last_analysis_day = None

# =============================
# CACHE (ANTI 429)
# =============================
last_history_update = 0
sol_cache = []
btc_cache = []

# =============================
# RSI BEREKENING
# =============================
def bereken_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50

    gains = []
    losses = []

    for i in range(1, period + 1):
        delta = prices[-period - 1 + i] - prices[-period - 2 + i]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    k = 2 / (period + 1)
    ema_value = prices[0]
    for price in prices[1:]:
        ema_value = price * k + ema_value * (1 - k)
    return ema_value

# =============================
# SWING SIGNAAL
# =============================
def get_candles():
    url = "https://api.bitvavo.com/v2/SOL-EUR/candles?interval=1d&limit=10"
    response = requests.get(url)
    data = response.json()
    candles = []
    for c in data:
        candles.append({
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
        })
    candles.reverse()
    return candles

def is_green(c):
    return c["close"] > c["open"]

def is_red(c):
    return c["close"] < c["open"]

def is_hammer(c):
    body = abs(c["close"] - c["open"])
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    if body == 0:
        return False
    return lower_wick >= 2 * body and upper_wick <= body

def is_shooting_star(c):
    body = abs(c["close"] - c["open"])
    upper_wick = c["high"] - max(c["open"], c["close"])
    lower_wick = min(c["open"], c["close"]) - c["low"]
    if body == 0:
        return False
    return upper_wick >= 2 * body and lower_wick <= body

def is_bullish_engulfing(c_prev, c_curr):
    return (
        is_red(c_prev)
        and is_green(c_curr)
        and c_curr["open"] < c_prev["close"]
        and c_curr["close"] > c_prev["open"]
    )

def is_bearish_engulfing(c_prev, c_curr):
    return (
        is_green(c_prev)
        and is_red(c_curr)
        and c_curr["open"] > c_prev["close"]
        and c_curr["close"] < c_prev["open"]
    )

def drie_rode_candles(candles):
    if len(candles) < 3:
        return False
    return (
        is_red(candles[-1])
        and is_red(candles[-2])
        and is_red(candles[-3])
    )

def check_buy_signaal(candles):
    if len(candles) < 2:
        return False, ""
    c_prev = candles[-2]
    c_curr = candles[-1]
    midden_rood = (c_prev["open"] + c_prev["close"]) / 2

    if is_green(c_prev) and is_green(c_curr) and c_curr["close"] > c_prev["close"]:
        return True, "2 groene candles stijgend"
    if is_red(c_prev) and is_green(c_curr) and c_curr["close"] > midden_rood:
        return True, "groene candle boven midden rode"
    if is_red(c_prev) and is_hammer(c_curr):
        return True, "hammer na rode candle"
    if is_bullish_engulfing(c_prev, c_curr):
        return True, "bullish engulfing"
    return False, ""

def check_sell_signaal(candles):
    if len(candles) < 2:
        return False, ""
    c_prev = candles[-2]
    c_curr = candles[-1]
    midden_groen = (c_prev["open"] + c_prev["close"]) / 2

    if is_green(c_prev) and is_red(c_curr) and c_curr["close"] < midden_groen:
        return True, "rode candle onder midden groene"
    if is_green(c_prev) and is_shooting_star(c_curr):
        return True, "shooting star na groene candle"
    if is_bearish_engulfing(c_prev, c_curr):
        return True, "bearish engulfing"
    return False, ""

def bereken_support(candles):
    lows = [c["low"] for c in candles]
    return min(lows[-20:])
def bereken_7d_low(candles):
    lows = [c["low"] for c in candles[-7:]]
    return min(lows)
def bereken_7d_high(candles):
    highs = [c["high"] for c in candles[-7:]]
    return max(highs)   

# =============================
# TELEGRAM
# =============================
def send(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})

def check_messages():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        if last_update_id:
            url += f"?offset={last_update_id + 1}"
        data = requests.get(url).json()
        if "result" not in data:
            return []
        messages = []
        for update in data["result"]:
            last_update_id = update["update_id"]
            message = update.get("message", {})
            text = message.get("text")
            if text:
                messages.append(text.strip().lower())
        return messages
    except Exception as e:
        print("Telegram error:", e)
        return []

# =============================
# BITVAVO
# =============================
def bitvavo_request(method, endpoint, body=None):
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',', ':')) if body else ""
    message = timestamp + method + endpoint + body_str

    signature = hmac.new(
        bytes(API_SECRET, 'utf-8'),
        bytes(message, 'utf-8'),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "Bitvavo-Access-Key": API_KEY,
        "Bitvavo-Access-Signature": signature,
        "Bitvavo-Access-Timestamp": timestamp,
        "Bitvavo-Access-Window": "60000",
        "Content-Type": "application/json"
    }

    url = "https://api.bitvavo.com" + endpoint

    if method == "GET":
        r = requests.get(url, headers=headers)
    else:
        r = requests.post(url, headers=headers, json=body)

    return r.json()

def get_price():
    response = bitvavo_request("GET", "/v2/ticker/price?market=SOL-EUR")
    if "price" in response:
        return float(response["price"])
    raise Exception(f"Price not found in response: {response}")

def get_history(coin, days):
    limit = days * 6  # 4h candles: 6 per dag
    url = f"https://api.bitvavo.com/v2/SOL-EUR/candles?interval=4h&limit={limit}"
    response = requests.get(url)
    print("Candles status:", response.status_code)
    data = response.json()
    prices = [float(candle[4]) for candle in data]
    prices.reverse()
    return prices

# =============================
# BALANCE
# =============================
def get_balances():
    balances = bitvavo_request("GET", "/v2/balance")
    eur = next((float(b['available']) for b in balances if b['symbol'] == 'EUR'), 0)
    sol = next((float(b['available']) for b in balances if b['symbol'] == 'SOL'), 0)
    return eur, sol

# =============================
# DATA
# =============================
def bepaal_trend(prices):
    change = (prices[-1] - prices[0]) / prices[0] * 100
    if change > 3:
        return "stijgend"
    elif change < -3:
        return "dalend"
    else:
        return "neutraal"

# =============================
# ANALYSE
# =============================
def analyse_market():
    global market_mode

    sol_prices_7  = get_history("solana", 7)
    sol_prices_20 = get_history("solana", 20)

    price = sol_prices_7[-1]

    change_7d  = ((price - sol_prices_7[0])  / sol_prices_7[0])  * 100
    change_20d = ((price - sol_prices_20[0]) / sol_prices_20[0]) * 100

    trend_7d  = bepaal_trend(sol_prices_7)
    trend_20d = bepaal_trend(sol_prices_20)

    rsi = bereken_rsi(sol_prices_7)

    if trend_20d == "dalend":
        market_mode = "bearish"
    elif trend_20d == "stijgend":
        market_mode = "bullish"
    else:
        market_mode = "neutraal"

    bericht = (
        f"📊 Daganalyse Solana\n\n"
        f"Koers: €{price:.2f}\n"
        f"7 dagen: {change_7d:.2f}%\n"
        f"20 dagen: {change_20d:.2f}%\n\n"
        f"Trend 7d:  {trend_7d}\n"
        f"Trend 20d: {trend_20d}\n"
        f"RSI (14):  {rsi:.1f}\n\n"
        f"Market mode: {market_mode}\n"
    )

    return bericht

# =============================
# BUY
# =============================
def buy_all():
    global last_buy_price, highest_price, buy_time

    eur, _ = get_balances()

    if eur > 5:
        price = get_price()

        body = {
            "market": "SOL-EUR",
            "side": "buy",
            "orderType": "market",
            "amountQuote": str(eur),
            "operatorId": str(int(time.time() * 1000))
        }

        response = bitvavo_request("POST", "/v2/order", body)
        send(f"🟢 BUY @ €{price:.2f}\n{response}")

        last_buy_price = price
        buy_time = time.time()  # ✅ NEW: Track entry time
        

# =============================
# SELL
# =============================
def sell_all(reden=""):
    global last_buy_price, highest_price, buy_time

    _, sol = get_balances()

    if sol > 0:
        price = get_price()

        body = {
            "market": "SOL-EUR",
            "side": "sell",
            "orderType": "market",
            "amount": str(sol),
            "operatorId": str(int(time.time() * 1000))
        }

        response = bitvavo_request("POST", "/v2/order", body)

        winst = ""
        if last_buy_price:
            pct = ((price - last_buy_price) / last_buy_price) * 100
            netto = pct - (FEE * 2 * 100)
            winst = f"\nResultaat: {pct:.2f}% bruto / {netto:.2f}% netto"

        send(f"🔴 SELL @ €{price:.2f} {reden}{winst}\n{response}")

        last_buy_price = None
        buy_time = None  # ✅ NEW: Clear entry time
        

# =============================
# MAIN
# =============================
def main():
    global trading_active, last_buy_price, last_analysis_day
    global sol_cache, btc_cache, last_history_update, market_mode, buy_time

    send("🤖 Bot live 🚀 — ✅ IMPROVED Swing Trading Strategy\n📊 RSI<35 buy | +7% TP | -8% SL | 5d timeout")

    while True:
        try:
            now = datetime.now()

            # =============================
            # 00:01 DAGELIJKSE ANALYSE
            # =============================
            if now.hour == 0 and now.minute == 1:
                if last_analysis_day != now.day:
                    analysis = analyse_market()
                    send(analysis)
                    last_analysis_day = now.day

            messages = check_messages()
            print(messages)

            sol_price = get_price()

            # =============================
            # CACHED HISTORY (5 min)
            # =============================
            if time.time() - last_history_update > 300:
                try:
                    sol_cache = get_history("solana", 50)
                    btc_cache = []
                    last_history_update = time.time()

                    # Market mode update via daily candles
                    url_trend = "https://api.bitvavo.com/v2/SOL-EUR/candles?interval=1d&limit=20"
                    resp_trend = requests.get(url_trend)
                    dag_candles_trend = []
                    for c in resp_trend.json():
                        dag_candles_trend.append({"close": float(c[4])})
                    dag_candles_trend.reverse()
                    dag_closes = [c["close"] for c in dag_candles_trend]
                    trend_dag = bepaal_trend(dag_closes)
                    if trend_dag == "dalend":
                        market_mode = "bearish"
                    elif trend_dag == "stijgend":
                        market_mode = "bullish"
                    else:
                        market_mode = "neutraal"

                except Exception as e:
                    print("History error (using cache):", e)
                    print("Raw response check — mogelijk Bitvavo candles probleem")

            if not sol_cache or len(sol_cache) < 20:
                print("Nog niet genoeg data...")
                time.sleep(15)
                continue

            # =============================
            # RSI + SWING SIGNALEN
            # =============================
            rsi = bereken_rsi(sol_cache)
            ema20 = ema(sol_cache, 20)
            ema50 = ema(sol_cache, 50)
            bull_trend = ema20 > (ema50 + 0.15) or (ema20 > ema50 * 0.997)
            candles = get_candles()
            koop_signaal, koop_reden = check_buy_signaal(candles)
            verkoop_signaal, verkoop_reden = check_sell_signaal(candles)

            eur, sol = get_balances()

            # =============================
            # ✅ IMPROVED SWING STRATEGIE (24/7 TRADING)
            # =============================
            if trading_active:

                # --- BUY (24/7, prefer 00:00-06:00 UTC) ---
                if sol == 0 and eur > 5:
                    low_7d = bereken_7d_low(candles)
                    near_low = sol_price <= low_7d * 1.05

                    # ✅ IMPROVED: RSI < 35 (stricter), checks market mode
                    sterke_buy = (
                        rsi < RSI_BUY_THRESHOLD      # ✅ RSI < 35
                        and bull_trend 
                        and market_mode != "bearish"
                        and near_low                  # ✅ Price near support
                    )

                    if sterke_buy:
                        koop_uur = now.hour
                        if 0 <= koop_uur <= 6:
                            print(f"[OPTIMAL BUY] RSI: {rsi:.1f}, Price: €{sol_price:.2f}, Mode: {market_mode} @ {koop_uur}:00")
                            send(f"🟢 OPTIMAL BUY @ {koop_uur}:00 — {koop_reden} | RSI: {rsi:.1f} | Mode: {market_mode}")
                        else:
                            print(f"[BUY] RSI: {rsi:.1f}, Price: €{sol_price:.2f}, Mode: {market_mode} @ {koop_uur}:00 (off-peak)")
                            send(f"🟢 BUY @ {koop_uur}:00 — {koop_reden} | RSI: {rsi:.1f}")
                        buy_all()

                # --- SELL (24/7, NO TIME LIMITS) ---
                elif sol > 0 and last_buy_price:

                    pct_change = ((sol_price - last_buy_price) / last_buy_price) * 100
                    
                    # ✅ PRIORITY 1: TAKE PROFIT @ +7% (MOST IMPORTANT!)
                    if pct_change >= TAKE_PROFIT_PERCENT * 100:
                        print(f"[TAKE PROFIT] +{pct_change:.2f}% @ €{sol_price:.2f}")
                        send(f"💰 TAKE PROFIT +{pct_change:.2f}% @ €{sol_price:.2f}")
                        sell_all("(take profit +7%)")

                    # ✅ PRIORITY 2: SWING SIGNAL (24/7, no time window!)
                    elif verkoop_signaal:
                        print(f"[SWING SELL] {verkoop_reden} @ €{sol_price:.2f}, Profit: {pct_change:.2f}%")
                        send(f"📉 SWING SELL — {verkoop_reden} @ €{sol_price:.2f} | Profit: {pct_change:.2f}%")
                        sell_all("(swing signal)")

                    # ✅ PRIORITY 3: RSI OVERBOUGHT + bearish market
                    elif rsi > RSI_SELL_THRESHOLD and market_mode == "bearish":
                        print(f"[RSI EXIT] RSI {rsi:.1f} in bearish mode @ €{sol_price:.2f}")
                        send(f"📉 RSI OVERBOUGHT ({rsi:.1f}) in bearish | Profit: {pct_change:.2f}%")
                        sell_all("(rsi overbought bearish)")

                    # ✅ PRIORITY 4: STOP LOSS @ -8% (SAFETY)
                    elif sol_price <= last_buy_price * (1 - STOP_LOSS_PERCENT):
                        stop_price = last_buy_price * (1 - STOP_LOSS_PERCENT)
                        print(f"[STOP LOSS] {pct_change:.2f}% @ €{sol_price:.2f} (limit: €{stop_price:.2f})")
                        send(f"🛑 STOP LOSS {pct_change:.2f}% @ €{sol_price:.2f}")
                        sell_all("(stop loss -8%)")

                    # ✅ PRIORITY 5: TIME LIMIT @ 5 dagen (EXIT DISCIPLINE)
                    elif buy_time and (time.time() - buy_time) > (MAX_HOLD_DAYS * 24 * 60 * 60):
                        days_held = int((time.time() - buy_time) / (24 * 60 * 60))
                        print(f"[TIME LIMIT] {days_held} days held, Profit: {pct_change:.2f}%")
                        send(f"⏰ TIME LIMIT ({days_held}d) | Profit: {pct_change:.2f}% @ €{sol_price:.2f}")
                        sell_all("(5 day timeout)")

            # =============================
            # COMMANDS
            # =============================
            for msg in messages:

                if "/analyse" in msg:
                    send(analyse_market())

                elif "/update" in msg:
                    totaal = eur + (sol * sol_price)
                    status = "🟢 HOLDING" if sol > 0 else "🔴 CASH"
                    buy_info = ""
                    if sol > 0 and last_buy_price:
                        pct = ((sol_price - last_buy_price) / last_buy_price) * 100
                        buy_info = f"\nEntry: €{last_buy_price:.2f} | Profit: {pct:.2f}%"
                    send(
                        f"📊 Update\n"
                        f"Koers: €{sol_price:.2f}\n"
                        f"Status: {status}\n"
                        f"Saldo: €{totaal:.2f}{buy_info}\n"
                        f"===========================\n"
                        f"RSI: {rsi:.1f} ({'oversold <35' if rsi < 35 else 'overbought >65' if rsi > 65 else 'neutral'})\n"
                        f"EMA20/50: {ema20:.2f} / {ema50:.2f} ({'bullish ↑' if bull_trend else 'bearish ↓'})\n"
                        f"Market Mode: {market_mode}\n"
                        f"Buy Signal: {koop_signaal} ({koop_reden})\n"
                        f"Sell Signal: {verkoop_signaal} ({verkoop_reden})"
                    )
                elif "/sell" in msg:
                    sell_all("(manual)")
                elif "/buy" in msg:
                    buy_all()
                    send("🟢 Manual BUY executed")
                elif "/rsi" in msg:
                    send(f"RSI: {rsi:.1f}\nBuy: {koop_signaal} ({koop_reden})\nSell: {verkoop_signaal} ({verkoop_reden})\nMode: {market_mode}")
                elif "/reset" in msg:
                    last_buy_price = None
                    highest_price = None
                    buy_time = None
                    send("🔄 Reset — ready to trade")
        except Exception as e:
            print("Error:", e)
            import traceback
            traceback.print_exc()

        time.sleep(15)

# =============================
# START
# =============================
if __name__ == "__main__":
    main()
