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
last_tv_update = 0
tv2_buy_cache = 0
tv4_sell_cache = 0
sol_cache = []
highest_price = 0  # 🟢 NIEUW: Houdt de hoogste koers bij tijdens de trade
tv4_osc_cache = "NEUTRAL" # 🟢 NIEUW


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
# TRADINGVIEW ANALYSE (GLOBAL SETUP)
# =============================
from tradingview_ta import TA_Handler, Interval

# Maak de handlers één keer vast aan bovenin het script
handler_2h = TA_Handler(
    symbol="SOLUSDT",
    exchange="BINANCE",
    screener="crypto",
    interval=Interval.INTERVAL_2_HOURS
)

handler_4h = TA_Handler(
    symbol="SOLUSDT",
    exchange="BINANCE",
    screener="crypto",
    interval=Interval.INTERVAL_4_HOURS
)

def get_tv_analyse(interval_string):
    try:
        import time
        if interval_string == "4h":
            time.sleep(2) 
            analyse = handler_4h.get_analysis()
        else:
            analyse = handler_2h.get_analysis()
            
        buy = analyse.summary["BUY"]
        sell = analyse.summary["SELL"]
        neutral = analyse.summary["NEUTRAL"]
        osc = analyse.oscillators["RECOMMENDATION"] # 🟢 NIEUW
        return buy, sell, neutral, osc
    except Exception as e:
        print(f"TV live data-feed vertraging: {e}")
        return 0, 0, 0, "NEUTRAL"






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
    global laatste_zone, last_tv_update, tv2_buy_cache, tv4_sell_cache, tv4_osc_cache  # 🟢 HIER ERBIJ PLAKKEN
    global highest_price  # 🟢 NIEUW: Maak de variabele hier bereikbaar




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

             
## ==========================================================
# 📊 NEW CONFIGURATION & BOT STATE
# ==========================================================
# Risicobeheer instellingen (Geen indicatoren meer nodig)
TRAILING_STOP_PERCENT = 0.015  # 1.5% trailing stop-loss
PROFIT_ACTIVATION     = 0.01   # Trailing pas actief na +1.0% winst
RISK_REWARD_RATIO     = 2.0    # Vaste 1:2 verhouding voor targets

# State variabelen voor het nieuwe mechanisme
opening_high = 0
opening_low = 0
fvg_target_price = 0
fvg_stop_loss = 0
is_doji_day = False
range_is_set = False
last_checked_day = None

# ==========================================================
# 🔍 STRATEGIE SENSORS: DOJI, OPENING RANGE & FVG DETECTIE
# ==========================================================
def scan_market_structure(now, candles_m1, candles_m5, daily_candles):
    global opening_high, opening_low, range_is_set, is_doji_day
    global fvg_target_price, fvg_stop_loss, last_checked_day

    # 1. Dagelijkse Doji Filter (Elke nacht om 01:01 uur jouw tijd)
    if now.hour == 0 and now.minute == 1 and last_checked_day != now.day:
        last_checked_day = now.day
        if len(daily_candles) > 0:
            yesterday = daily_candles[-1]
            totale_range = yesterday["high"] - yesterday["low"]
            body_grootte = abs(yesterday["close"] - yesterday["open"])
            # Doji definitie: Body is kleiner dan of gelijk aan 10% van de totale range
            is_doji_day = (body_grootte / totale_range) <= 0.10 if totale_range > 0 else False
            range_is_set = False  # Reset opening range voor de nieuwe dag

    # 2. Opening Range Vastzetten (Exact om 09:35 uur, na de eerste 5-minuten kaars)
    # Let op: Tijdstip is af te stemmen op de gewenste geografische marktopening
    if now.hour == 9 and now.minute == 35 and not range_is_set:
        if len(candles_m5) > 0:
            first_candle = candles_m5[-1]
            opening_high = first_candle["high"]
            opening_low = first_candle["low"]
            range_is_set = True

    # 3. 1-Minuut Fair Value Gap (FVG) Uitbraak Scanner
    if range_is_set and len(candles_m1) >= 3:
        c1, c2, c3 = candles_m1[-3], candles_m1[-2], candles_m1[-1]
        
        # Bullish Uitbraak + FVG (Gat tussen high van 1 en low van 3)
        if c3["close"] > opening_high and c3["low"] > c1["high"]:
            fvg_gap_size = c3["low"] - c1["high"]
            risk = c3["close"] - c1["high"]
            
            fvg_stop_loss = c1["high"]  # Harde bodem vlak onder de gap
            fvg_target_price = c3["close"] + (risk * RISK_REWARD_RATIO)  # Harde 1:2 winsttarget
            return "BUY_SIGNAL"

    return "WAITING"

# ==========================================================
# ⚡ CORE EXECUTION LOOP: INKOPEN & INGEBOUWDE SWING EXITS
# ==========================================================
# Plak dit deel inside jouw bestaande `while True` loop waar 'if trading_active:' staat:

    if trading_active:
        # Haal de benodigde kaarsdata live op uit je databronnen
        candles_m1 = get_candles() # Pas aan naar de juiste M1 feed indien beschikbaar
        candles_m5 = get_candles() # Pas aan naar de juiste M5 feed indien beschikbaar
        daily_candles = get_candles() # Maakt al gebruik van je bestaande daggrafiek feed
        
        signal = scan_market_structure(now, candles_m1, candles_m5, daily_candles)
        
        # --- AUTOMATISCH KOPEN OP BASIS VAN PRIJS EN VOLUME ---
        if sol < 0.01 and eur > 5 and signal == "BUY_SIGNAL":
            # Pas startbedrag aan op basis van het Doji-filter (Agressiever als gisteren een Doji was)
            trade_remark = "🚨 AGGRESSIVE DOJI BREAKOUT" if is_doji_day else "🛒 REGULAR OPENING BREAKOUT"
            send(f"{trade_remark} — FVG Gevonden! | Target: €{fvg_target_price:.2f} | Stop: €{fvg_stop_loss:.2f}")
            buy_all()
            highest_price = sol_price
            sol = 1  # Direct vergrendelen tegen dubbele aankopen
            
        # --- AUTOMATISCH VERKOPEN EN DYNAMISCH WINSTBEHEER ---
        elif sol > 0 and last_buy_price is not None:
            # Update de hoogst behaalde koers tijdens de rit
            if sol_price > highest_price:
                highest_price = sol_price

            # Check A: Harde 1:2 Winsttarget Bereikt
            if sol_price >= fvg_target_price:
                send(f"🎯 WINSDOEL BEREIKT (1:2 Ratio) — Target €{fvg_target_price:.2f} geraakt!")
                sell_all("(Take Profit)")
                highest_price = 0

            # Check B: Harde Initiële FVG Stop Loss Ruisbescherming
            elif sol_price <= fvg_stop_loss:
                send(f"🚨 FVG STOP LOSS GERAAKT — Risico afgekapt op €{fvg_stop_loss:.2f}")
                sell_all("(Stop Loss)")
                highest_price = 0

            # Check C: Dynamische Trailing Stop Loss (Zodra er minimaal +1% winst is opgebouwd)
            elif highest_price >= last_buy_price * (1 + PROFIT_ACTIVATION):
                active_trailing_level = highest_price * (1 - TRALING_STOP_PERCENT)
                if sol_price <= active_trailing_level:
                    send(f"🚀 TRAILING STOP LOSS GETRIGGERD — Winst veiliggesteld op €{sol_price:.2f} vanaf top €{highest_price:.2f}")
                    sell_all("(Trailing Winstrust)")
                    highest_price = 0

            # =============================
            # COMMANDS
            # =============================
            for msg in messages:

                if "/analyse" in msg:
                    send(analyse_market())

                elif "/update" in msg:
                    # Live Binance TV waarden ophalen voor het update overzicht
                    # Haal de gecachte of live scores op voor het update overzicht
                    tv2_buy, _, _, _ = get_tv_analyse("2h")  # 🟢 BIJGEWERKT: 4 waarden uitpakken
                    _, tv4_sell, _, tv4_osc = get_tv_analyse("4h")  # 🟢 BIJGEWERKT: 4 waarden uitpakken

                    
                    

                    totaal = eur + (sol * sol_price)
                    status = "BUY" if sol > 0 else "SELL"
                    
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
                        f"Zone: {'🟢 Koop zone' if near_support else '🔴 Verkoop zone' if near_resistance else '⏸️ Geen zone'}\n\n"                 
                        f"Support: €{low_7d:.2f} | Resistance: €{high_7d:.2f}\n"
                        f"=========================\n\n"
                        f"📊 TradingView Live (Binance):\n"
                        f"🟢 2H Buy Score: {tv2_buy} / 26\n"
                        f"❌ 4H Sell Score: {tv4_sell} / 26\n"
                        f"🔍 4H Oscillators: {tv4_osc}\n"
                        f"=========================\n"
                        f"=========================\n"
                        f"📊 Check Moving Averages (4H):\nhttps://www.tradingview.com/symbols/SOLEUR/technicals/?exchange=BINANCE&interval=2h\n"
                        f"✅ Kopen: 10+ Buy\n❌ Verkopen: 10+ Sell"
                    )

                
                elif "/pauzeon" in msg:
                    trading_active = False
                    send("⏸️ Bot gepauzeerd — geen trades")

                elif "/pauzeoff" in msg:
                    trading_active = True
                    send("▶️ Bot actief — trades hervat")
                    
                elif "/sell" in msg:
                    if sol > 0.01:
                         send("⏳ Handmatige verkoop gestart op Bitvavo...")
                         sell_all("(Handmatig via Telegram)")
                         highest_price = 0  # 🟢 RESET: Zet de trailing-top direct weer op nul
                         send("✅ Alle Solana succesvol verkocht. Status staat weer op SELL.")
                    else:
                         send("❌ Actie geweigerd: Je bezit momenteel geen Solana om te verkopen.")



                
                elif "/buy" in msg:
                    buy_all()
                    send("🟢 Handmatige BUY uitgevoerd")
                
                elif "/rsi" in msg:
                    # Bereken de actuele RSI en geef de live status van je TV indicators door
                    rsi_now = bereken_rsi(sol_cache)
                    send(
                        f"📊 Live Indicator Status:\n"
                        f"RSI (14): {rsi_now:.1f}\n\n"
                        f"🛒 Koop Status (2H Buy): {tv2_buy} / 26 (Koopt bij >= 10)\n"
                        f"📉 Verkoop Status (4H Sell): {tv4_sell} / 26 (Verkoopt bij >= 10 op Candle-Close)"
                    )

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
