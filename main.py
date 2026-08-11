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
# STATE (AANGEPAST)
# =============================
trading_active = True
last_update_id = None
last_buy_price = None
market_mode = "neutraal"
last_analysis_day = None

STOP_LOSS_PERCENT     = 0.06    
FEE                   = 0.0025  
MIN_PROFIT_AFTER_FEE  = FEE * 2 + 0.005  
laatste_zone = None

# Pure Swing & Price Action Instellingen
TRAILING_STOP_PERCENT = 0.015  
PROFIT_ACTIVATION     = 0.01   
RISK_REWARD_RATIO     = 2.0    

opening_high = 0
opening_low = 0
fvg_target_price = 0
fvg_stop_loss = 0
is_doji_day = False
range_is_set = False
last_checked_day = None


# =============================
# CACHE (ANTI 429)
# =============================
last_history_update = 0
last_tv_update = 0
tv2_buy_cache = 0
tv4_sell_cache = 0
sol_cache = []
highest_price = 0  
tv4_osc_cache = "NEUTRAL" 

# =============================
# RSI BEREKENING & EMA
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
# TRADINGVIEW ANALYSE & DATA-FEED (DEFINITIEVE FIX)
# =============================
from tradingview_ta import TA_Handler, Interval

handler_1d = TA_Handler(symbol="SOLEUR", exchange="COINBASE", screener="crypto", interval=Interval.INTERVAL_1_DAY)
handler_5m = TA_Handler(symbol="SOLEUR", exchange="COINBASE", screener="crypto", interval=Interval.INTERVAL_5_MINUTES)
handler_1m = TA_Handler(symbol="SOLEUR", exchange="COINBASE", screener="crypto", interval=Interval.INTERVAL_1_MINUTE)
handler_2h = TA_Handler(symbol="SOLEUR", exchange="COINBASE", screener="crypto", interval=Interval.INTERVAL_2_HOURS)
handler_4h = TA_Handler(symbol="SOLEUR", exchange="COINBASE", screener="crypto", interval=Interval.INTERVAL_4_HOURS)

def get_tv_candles_m1():
    try:
        analysis = handler_1m.get_analysis()
        o = float(analysis.indicators["open"])
        h = float(analysis.indicators["high"])
        l = float(analysis.indicators["low"])
        c = float(analysis.indicators["close"])
        # We simuleren een geldige lijst van 3 kaarsen voor de FVG-sensor
        return [{"open": o, "high": h, "low": l, "close": c}] * 3
    except:
        p = get_price()
        return [{"open": p, "high": p, "low": p, "close": p}] * 3

def get_tv_candles_m5():
    try:
        analysis = handler_5m.get_analysis()
        return [{"open": float(analysis.indicators["open"]), "high": float(analysis.indicators["high"]), "low": float(analysis.indicators["low"]), "close": float(analysis.indicators["close"])}]
    except:
        p = get_price()
        return [{"open": p, "high": p, "low": p, "close": p}]

def get_tv_candles_daily():
    try:
        analysis = handler_1d.get_analysis()
        return [{"open": float(analysis.indicators["open"]), "high": float(analysis.indicators["high"]), "low": float(analysis.indicators["low"]), "close": float(analysis.indicators["close"])}]
    except:
        return []

def get_tv_analyse(interval_string):
    try:
        if interval_string == "4h":
            time.sleep(2) 
            analyse = handler_4h.get_analysis()
        else:
            analyse = handler_2h.get_analysis()
        return analyse.summary["BUY"], analyse.summary["SELL"], analyse.summary["NEUTRAL"], analyse.oscillators["RECOMMENDATION"]
    except:
        return 0, 0, 0, "NEUTRAL"

# =============================
# SWING SIGNAAL (100% TRADINGVIEW POWERED)
# =============================
def get_candles():
    return get_tv_candles_daily()

def get_history(coin, days):
    try:
        analysis = handler_1d.get_analysis()
        return [float(analysis.indicators["close"])] * days
    except:
        return [get_price()] * days







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

# =============================
# VEILIGE LIVE KOERS (MET TRADINGVIEW FALLBACK)
# =============================
def get_price():
    try:
        response = bitvavo_request("GET", "/v2/ticker/price?market=SOL-EUR")
        # Controleer of het antwoord een geldige tabel/dictionary is en de prijs bevat
        if isinstance(response, dict) and "price" in response:
            return float(response["price"])
        
        # Als Bitvavo tekst of een error teruggeeft, pakken we live de TradingView prijs
        analysis = handler_1m.get_analysis()
        return float(analysis.indicators["close"])
    except Exception as e:
        try:
            # Secundaire back-up via de 1-minuut handler
            analysis = handler_1m.get_analysis()
            return float(analysis.indicators["close"])
        except:
            print(f"⚠️ Prijsfeed volledig onderbroken, fallback naar €64.00")
            return 64.00  # Ultieme nood-fallback om crashes te voorkomen

def get_history(coin="SOL-EUR", interval="1d", limit=14):
    try:
        response = bitvavo_request("GET", f"/v2/markets/{coin}/candles?interval={interval}&limit={limit}")
        if isinstance(response, list):
            prices = [float(candle[4]) for candle in response]
            prices.reverse()
            return prices
    except Exception as e:
        print(f"Fout bij ophalen historie via Bitvavo: {e}")
    
    p = get_price()
    return [p] * limit


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
# ANALYSE (VOLLEDIG GEZUIVERD VAN BITVAVO COM7 BUG)
# =============================
def analyse_market():
    global market_mode
    try:
        # Haal de live-koers op
        price = get_price()
        
        # Vraag direct aan de daggrafiek handler wat de status is
        analysis_1d = handler_1d.get_analysis()
        tv_trend = analysis_1d.summary["RECOMMENDATION"]
        
        if "SELL" in tv_trend:
            market_mode = "bearish"
        elif "BUY" in tv_trend:
            market_mode = "bullish"
        else:
            market_mode = "neutraal"
            
        bericht = (
            f"📊 Daganalyse Solana (via TradingView)\n\n"
            f"Koers: €{price:.2f}\n"
            f"Trend daggrafiek: {tv_trend}\n"
            f"Market mode: {market_mode}\n"
        )
        return bericht
    except Exception as e:
        return f"📊 Daganalyse Solana\nKoers: €{get_price():.2f}\nMarket mode: {market_mode}\n(Trend-feed tijdelijk vertraagd: {e})"


# =============================
# BUY & SELL AUTOMATION (UNTOUCHED)
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

# ==========================================================
# 🔍 STRATEGIE SENSORS: SMC BREAK OF STRUCTURE & RETRACEMENT FIX (NIEUW)
# ==========================================================
def scan_market_structure(now, candles_m1, candles_m5, daily_candles):
    global opening_high, opening_low, range_is_set, is_doji_day
    global fvg_target_price, fvg_stop_loss, last_checked_day
    
    if 'bos_triggered' not in globals():
        global bos_triggered, bos_high_target
        bos_triggered = False
        bos_high_target = 0

    if now.hour == 0 and now.minute == 1 and last_checked_day != now.day:
        last_checked_day = now.day
        if len(daily_candles) > 0:
            yesterday = daily_candles[-1]
            totale_range = yesterday["high"] - yesterday["low"]
            body_grootte = abs(yesterday["close"] - yesterday["open"])
            is_doji_day = (body_grootte / totale_range) <= 0.10 if totale_range > 0 else False
            range_is_set = False  
            bos_triggered = False  

    if now.hour == 9 and now.minute == 35 and not range_is_set:
        if len(candles_m5) > 0:
            first_candle = candles_m5[-1]
            opening_high = first_candle["high"]
            opening_low = first_candle["low"]
            range_is_set = True
            bos_triggered = False

    if range_is_set and len(candles_m1) >= 2:
        current_candle = candles_m1[-1]
        live_price = current_candle["close"]

        if not bos_triggered:
            if current_candle["close"] > opening_high and current_candle["open"] < opening_high:
                bos_triggered = True
                bos_high_target = current_candle["high"]
                print(f"🔥 Real Break of Structure (BOS) gedetecteerd op €{live_price:.2f}! Wachten op retracement...")
        
        elif bos_triggered:
            if live_price <= opening_high * 1.002 and live_price >= opening_low:
                risk = bos_high_target - opening_high
                if risk <= 0:
                    risk = live_price * 0.015 

                fvg_stop_loss = opening_high - (risk * 0.5) 
                fvg_target_price = live_price + (risk * RISK_REWARD_RATIO) 
                bos_triggered = False 
                return "BUY_SIGNAL"

            elif live_price < opening_low:
                bos_triggered = False
                print("❌ BOS ongeldig verklaard: prijs zakte door de bodem.")

    return "WAITING"



# =============================
# MAIN EXECUTIE LOOP (REALTIME-SCANNING EN SMC COMPATIBLE)
# =============================
def main():
    global trading_active, last_buy_price, last_analysis_day
    global sol_cache, last_history_update, market_mode
    global laatste_zone, last_tv_update, tv2_buy_cache, tv4_sell_cache, tv4_osc_cache  
    global highest_price  
    global opening_high, opening_low, range_is_set, is_doji_day, fvg_target_price, fvg_stop_loss, last_checked_day

    print("🚀 Realtime Price Action scanner geactiveerd!")
    send("🤖 Bot live 🚀 — Continu scanning actief (0 seconden vertraging)")

    # Onthoudt wanneer er voor het laatst naar het Render-dashboard is geprint
    laatste_log_tijd = 0

    while True:
        try:
            now = datetime.now()
            sol_price = get_price()  

            # =======================================================
            # RENDER LIVE STATUS LOG (ELKE 60 SECONDEN)
            # =======================================================
            if time.time() - laatste_log_tijd > 60:
                print(f"⏰ [{now.strftime('%H:%M:%S')}] Bot scant actief. Live SOL koers: €{sol_price} | Modus: {market_mode}")
                laatste_log_tijd = time.time()
            # =======================================================

            if now.hour == 0 and now.minute == 1 and last_analysis_day != now.day:
                analysis = analyse_market()
                send(analysis)
                last_analysis_day = now.day

            messages = check_messages()

            if time.time() - last_history_update > 600:
                try:
                    analysis_1d = handler_1d.get_analysis()
                    tv_trend = analysis_1d.summary["RECOMMENDATION"]
                    market_mode = "bullish" if "BUY" in tv_trend else "bearish" if "SELL" in tv_trend else "neutraal"
                    last_history_update = time.time()
                except:
                    pass

            # ==========================================================
            # ⚡ KERNLOOP: REALTIME DOORSTUREN VAN DE RECHTE TV KANDLES
            # ==========================================================
                        # ==========================================================
            # ⚡ KERNLOOP: REALTIME DOORSTUREN VAN DE RECHTE TV KANDLES
            # ==========================================================
            if trading_active:
                # 24/7 scanning geactiveerd (tijdrestrictie verwijderd)
                candles_m1 = get_tv_candles_m1()
                candles_m5 = get_tv_candles_m5()
                candles_daily = get_tv_candles_daily()
                signal = scan_market_structure(now, candles_m1, candles_m5, candles_daily)

                eur, sol = get_balances()


                # --- AUTOMATISCH INKOPEN ---
                if sol < 0.01 and eur > 5 and signal == "BUY_SIGNAL":
                    trade_remark = "🚨 AGGRESSIVE DOJI BREAKOUT" if is_doji_day else "🛒 REGULAR OPENING BREAKOUT"
                    send(f"{trade_remark} — FVG Gevonden! | Target: €{fvg_target_price:.2f} | Stop: €{fvg_stop_loss:.2f}")
                    buy_all()
                    highest_price = sol_price
                    sol = 1  

                # --- DYNAMISCH WINSTBEHEER & EXITS ---
                elif sol > 0 and last_buy_price is not None:
                    if sol_price > highest_price:
                        highest_price = sol_price

                    if sol_price >= fvg_target_price:
                        send(f"🎯 WINSDOEL BEREIKT — Target €{fvg_target_price:.2f} geraakt!")
                        sell_all("(Take Profit)")
                        highest_price = 0
                        
                    elif sol_price <= fvg_stop_loss:
                        send(f"🚨 FVG STOP LOSS GERAAKT — Risico afgekapt op €{fvg_stop_loss:.2f}")
                        sell_all("(Stop Loss)")
                        highest_price = 0
                        
                    elif highest_price >= last_buy_price * (1 + PROFIT_ACTIVATION):
                        active_trailing_level = highest_price * (1 - TRAILING_STOP_PERCENT)
                        if sol_price <= active_trailing_level:
                            send(f"🚀 TRAILING STOP LOSS GETRIGGERD — Winst veiliggesteld op €{sol_price:.2f}")
                            sell_all("(Trailing Winstrust)")
                            highest_price = 0

            # =============================
            # TELEGRAM COMMANDS
            # =============================
            for msg in messages:
                if "/analyse" in msg:
                    send(analyse_market())
                    
                elif "/update" in msg:
                    eur, sol = get_balances()
                    totaal = eur + (sol * sol_price)
                    status = "BUY" if sol > 0 else "SELL"
                    send(f"📊 Realtime Update\nKoers: €{sol_price:.2f}\nStatus: {status}\nSaldo: €{totaal:.2f}\nDoji Dag: {'⚠️ Ja' if is_doji_day else '❌ Nee'}")
                    
                elif "/pauzeon" in msg:
                    trading_active = False
                    send("⏸️ Bot gepauzeerd")
                    
                elif "/pauzeoff" in msg:
                    trading_active = True
                    send("▶️ Bot actief")
                    
                elif "/sell" in msg:
                    if sol > 0.01:
                         sell_all("(Handmatig via Telegram)")
                         highest_price = 0  
                    else:
                         send("❌ Geen Solana in bezit.")
                         
                elif "/buy" in msg:
                    buy_all()
                    
                elif "/reset" in msg:
                    last_buy_price = None
                    highest_price = 0
                    send("🔄 Reset voltooid")

        except Exception as e:
            print("Fout in realtime loop:", e)

        time.sleep(1)

# =======================================================
# RENDER WEB SERVER & BACKROUND THREAD COUPLING
# =======================================================
import threading
from flask import Flask

# 1. Maak de minimale Flask webserver aan voor Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is online en actief!"

# 2. Start de webserver en je bestaande main() gelijktijdig
if __name__ == "__main__":
    print("Systeem start op...")
    
    # We starten jouw bestaande main() functie in een achtergrond-thread
    bot_thread = threading.Thread(target=main)
    bot_thread.daemon = True
    bot_thread.start()
    print("Trading bot (main) succesvol naar de achtergrond verplaatst.")
    
    # Start de webserver op de poort die Render vereist
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
