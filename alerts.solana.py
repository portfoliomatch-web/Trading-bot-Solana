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
market_mode = "neutraal"

# ✅ AANGEPAST: realistische fee-bewuste waarden
STOP_LOSS_PERCENT     = 0.12    # 12% stop loss
# Trailing verwijderd — bot verkoopt op 7 dagen high
FEE                   = 0.0025  # Bitvavo taker fee 0.25%
MIN_PROFIT_AFTER_FEE  = FEE * 2 + 0.005  # minimaal 1% netto winst

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
    sol_prices_20 = get_history("solana", 20)  # ✅ was 30, nu 20

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
    global last_buy_price, highest_price

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
        

# =============================
# SELL
# =============================
def sell_all(reden=""):
    global last_buy_price, highest_price

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
        

# =============================
# MAIN
# =============================
def main():
    global trading_active, last_buy_price, last_analysis_day
    global sol_cache, btc_cache, last_history_update, market_mode

    send("🤖 Bot live 🚀 — Swing RSI strategie actief")

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
                    btc_cache = []  # niet meer nodig, Bitvavo heeft geen BTC nodig
                    last_history_update = time.time()

                    # Market mode bijwerken via dagcandles (consistent met daganalyse)
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
            bull_trend = ema20 > ema50  # zonder buffer
            candles = get_candles()
            koop_signaal, koop_reden = check_buy_signaal(candles)
            verkoop_signaal, verkoop_reden = check_sell_signaal(candles)

            eur, sol = get_balances()

            # =============================
            # ✅ SWING STRATEGIE
            # =============================
            if trading_active:

                # --- BUY ---
                # Alleen kopen tussen 00:00 en 00:30 na sluiting dagcandle
                koop_window = (now.hour == 0 and now.minute <= 30)  # middernacht
                
                if sol == 0 and eur > 5:

                    sterke_buy = (
                        rsi < 40
                        and bull_trend
                        and koop_window
                    )

                    if sterke_buy:
                        send(f"📈 STRONG BUY — {koop_reden} | RSI: {rsi:.1f} | mode: {market_mode}")
                        buy_all()

                # --- SELL ---
                elif sol > 0:

                    # Swing top verkoop alleen om middernacht
                    verkoop_window = (now.hour == 0 and now.minute <= 30)

                    high_7d = bereken_7d_high(candles)
                    near_high = sol_price >= high_7d * 0.95

                    if rsi > 55 and not bull_trend and verkoop_window:
                        send(f"📉 SELL signaal — {verkoop_reden} | RSI: {rsi:.1f}")
                        sell_all("(swing top)")

                    # Stop loss
                    elif last_buy_price and sol_price <= last_buy_price * (1 - STOP_LOSS_PERCENT):
                        sell_all("(stop loss)")

            # =============================
            # COMMANDS
            # =============================
            for msg in messages:

                if "/analyse" in msg:
                    send(analyse_market())

                elif "/update" in msg:
                    totaal = eur + (sol * sol_price)
                    status = "BUY" if sol > 0 else "SELL"
                    send(
                        f"📊 Update\n"
                        f"Koers: €{sol_price:.2f}\n"
                        f"Status: {status}\n"
                        f"Saldo: €{totaal:.2f}\n"
                        f"===========================\n"
                        f"RSI: {rsi:.1f} ({'bullish' if rsi < 40 else 'bearish' if rsi > 60 else 'neutraal'})\n"
                        f"EMA20: {ema20:.2f} ({'bullish' if bull_trend else 'bearish'})\n"
                        f"Koop signaal: {koop_signaal} ({koop_reden})\n"
                        f"Verkoop signaal: {verkoop_signaal} ({verkoop_reden})"
                    )
                elif "/sell" in msg:
                    sell_all("(handmatig)")
                elif "/buy" in msg:
                    buy_all()
                    send("🟢 Handmatige BUY uitgevoerd")
                elif "/rsi" in msg:
                    send(f"RSI: {rsi:.1f}\nKoop signaal: {koop_signaal} ({koop_reden})\nVerkoop signaal: {verkoop_signaal} ({verkoop_reden})")
                elif "/reset" in msg:
                    last_buy_price = None
                    highest_price = None
                    send("🔄 Reset gedaan — bot staat op SELL")
        except Exception as e:
            print("Fout:", e)

        time.sleep(15)

# =============================
# START
# =============================
if __name__ == "__main__":
    main()
