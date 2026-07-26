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
STOP_LOSS_PERCENT     = 0.06    # 6% stop loss
# Trailing verwijderd — bot verkoopt op 7 dagen high
FEE                   = 0.0025  # Bitvavo taker fee 0.25%
MIN_PROFIT_AFTER_FEE  = FEE * 2 + 0.005  # minimaal 1% netto winst

laatste_zone = None


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
    url = "https://api.bitvavo.com/v2/SOL-EUR/candles?interval=1d&limit=30"
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
    url = f"https://api.bitvavo.com/v2/SOL-EUR/candles?interval=1d&limit={days}"
    response = requests.get(url)
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
    global laatste_zone


    send("🤖 Bot live 🚀 — Swing strategie actief")

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
            ema20 = ema(sol_cache, 7)
            ema50 = ema(sol_cache, 20)
            bull_trend = ema20 > (ema50 + 0.50)
            candle_kleur = "🟢 Groen" if sol_cache[-1] > sol_cache[-2] else "🔴 Rood"
            eur, sol = get_balances()


           # =============================
            # ✅ SWING STRATEGIE
            # =============================
            if trading_active:

                # 24/7 support/resistance melding
                candles = get_candles()
                
                # Laatste lokale high en low vanaf vandaag terugkijkend
                recent_lows = [c["low"] for c in candles[-14:]]
                recent_highs = [c["high"] for c in candles[-14:]]
                
                # Bearish → laagste low zoeken
                # Bullish → hoogste high zoeken
                if not bull_trend:
                    low_7d = min(c["low"] for c in candles[-7:])
                    high_7d = max(c["high"] for c in candles[-7:])
                else:
                    low_7d = min(c["low"] for c in candles[-7:])
                    high_7d = max(c["high"] for c in candles[-7:])
                
                
                
                near_support = sol_price <= low_7d + 1.00
                near_resistance = sol_price >= high_7d - 1.00

                if near_support and laatste_zone != "support":
                    laatste_zone = "support"
                    if bull_trend and candle_kleur == "🟢 Groen":
                        send(
                            f"🚀 ALLES GROEN — KOOP SIGNAAL\n"
                            f"SOL: €{sol_price:.2f} | Support: €{low_7d:.2f}\n"
                            f"Trend: 🟢 Bullish | Candle: 🟢 Groen\n\n"
                            f"📊 Check Moving Averages:\n1H: https://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=COINBASE&interval=1h\n4H: https://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=COINBASE&interval=4h\n✅ Kopen: 1H 10+ Buy + 4H niet Strong sell\n❌ Verkopen: 4H 10+ Sell"
                            f"✅ Kopen: 10+ Buy"
                        )
                    else:
                        send(f"🟢 KOOP ZONE — SOL: €{sol_price:.2f} | Support: €{low_7d:.2f}")
                elif near_resistance and laatste_zone != "resistance":
                    laatste_zone = "resistance"
                    if not bull_trend and candle_kleur == "🔴 Rood":
                        send(
                            f"🚨 ALLES ROOD — VERKOOP SIGNAAL\n"
                            f"SOL: €{sol_price:.2f} | Resistance: €{high_7d:.2f}\n"
                            f"Trend: 🔴 Bearish | Candle: 🔴 Rood\n\n"
                            f"📊 Check Moving Averages:\n1H: https://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=COINBASE&interval=1h\n4H: https://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=COINBASE&interval=4h\n✅ Kopen: 1H 10+ Buy + 4H niet Strong sell\n❌ Verkopen: 4H 10+ Sell"
                            f"❌ Verkopen: 10+ Sell"
                        )
                    else:
                        send(f"🔴 VERKOOP ZONE — SOL: €{sol_price:.2f} | Resistance: €{high_7d:.2f}")
                    
                elif not near_support and not near_resistance:
                    laatste_zone = None

                # Stop loss blijft actief
                if sol > 0 and last_buy_price and sol_price <= last_buy_price * (1 - STOP_LOSS_PERCENT):
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
                    winst = ""
                    if sol > 0 and last_buy_price:
                        pct = ((sol_price - last_buy_price) / last_buy_price) * 100
                        winst = f"\nEntry: €{last_buy_price:.2f} | Winst: {pct:.2f}%"
                    signaal = "⚠️ KOOP SIGNAAL" if bull_trend else "⚠️ VERKOOP SIGNAAL"
                    
                    winst_info = ""
                    if sol > 0 and last_buy_price:
                        pct = ((sol_price - last_buy_price) / last_buy_price) * 100
                        netto = pct - (FEE * 2 * 100)
                        winst_info = f"\nEntry: €{last_buy_price:.2f} | Winst: {pct:.2f}% ({netto:.2f}% netto)"

                    send(
                        f"📊 Update\n"
                        f"Koers: €{sol_price:.2f}\n"
                        f"Status: {status}{winst_info}\n"
                        f"Saldo: €{totaal:.2f}\n"
                    
                        f"=========================\n"
                        f"EMA7: {ema20:.2f}\n"
                        f"EMA20: {ema50:.2f}\n"
                        f"Trend: {'🟢 Bullish' if bull_trend else '🔴 Bearish'}\n"
                        f"Candle: {candle_kleur}\n"
                        f"Zone: {'🟢 Koop zone' if near_support else '🔴 Verkoop zone' if near_resistance else '⏸️ Geen zone'}\n"
                        f"\n"
                        f"Support: €{low_7d:.2f} | Resistance: €{high_7d:.2f}\n"
                        f"=========================\n"
                        f"📊 Check Moving Averages:\n1H: https://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=COINBASE&interval=1h\n4H: https://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=COINBASE&interval=4h\n✅ Kopen: 1H 10+ Buy + 4H niet Strong sell\n❌ Verkopen: 4H 10+ Sell"
                    
                        
                    )
                
                elif "/pauzeon" in msg:
                    trading_active = False
                    send("⏸️ Bot gepauzeerd — geen trades")

                elif "/pauzeoff" in msg:
                    trading_active = True
                    send("▶️ Bot actief — trades hervat")
                    
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
