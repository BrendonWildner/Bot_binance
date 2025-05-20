import os
import time
import math
import pandas as pd
from threading import Thread
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import SMAIndicator, MACD
from colorama import Fore, Style

# Carrega variáveis do .env
load_dotenv()
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
ALAVANCAGEM = int(os.getenv("ALAVANCAGEM", 10))

client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = 'https://fapi.binance.com/fapi'

PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "LTCUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT"
]
QTY_USDT = 12
STOP_LOSS_PCT = -0.5  
TAKE_PROFIT_PCT = 0.5  
MAX_OPERACOES_ATIVAS = 2
STOP_GAIN_GLOBAL = 15
STOP_LOSS_GLOBAL = -10

lucro_total = 0
open_positions = {}

def get_min_qty(pair):
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == pair:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['minQty'])
    except Exception as e:
        print(f"[{pair}] ❌ Erro ao buscar minQty: {e}")
    return 0.001

def get_step_size(pair):
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == pair:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize'])
    except Exception as e:
        print(f"[{pair}] ❌ Erro ao buscar stepSize: {e}")
    return 0.001

def round_step_size(quantity, step_size):
    precision = int(round(-math.log(step_size, 10), 0))
    return round(quantity, precision)

def get_klines(pair):
    candles = client.futures_klines(symbol=pair, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100)
    df = pd.DataFrame(candles, columns=['time','o','h','l','c','v','ct','qav','nt','tbbav','tbqav','ig'])
    df['close'] = df['c'].astype(float)
    df['high'] = df['h'].astype(float)
    df['low'] = df['l'].astype(float)
    df['volume'] = df['v'].astype(float)
    return df

def signal_generator(df, pair):
    rsi = RSIIndicator(df['close'], window=14).rsi()
    sma7 = SMAIndicator(df['close'], window=7).sma_indicator()
    sma21 = SMAIndicator(df['close'], window=21).sma_indicator()
    macd_diff = MACD(df['close']).macd_diff()
    stoch = StochasticOscillator(df['high'], df['low'], df['close'], window=14, smooth_window=3)
    volume_ma = df['volume'].rolling(window=20).mean()

    last_close = df['close'].iloc[-1]
    last_rsi = rsi.iloc[-1]
    last_macd = macd_diff.iloc[-1]
    last_sma7 = sma7.iloc[-1]
    last_sma21 = sma21.iloc[-1]
    last_stoch_k = stoch.stoch().iloc[-1]
    last_stoch_d = stoch.stoch_signal().iloc[-1]
    last_volume = df['volume'].iloc[-1]
    avg_volume = volume_ma.iloc[-1]

    print(f"[{pair}] [DEBUG] RSI: {last_rsi:.2f}, MACD: {last_macd:.4f}, Stoch %K: {last_stoch_k:.2f}, %D: {last_stoch_d:.2f}, Close: {last_close:.2f}, SMA7: {last_sma7:.2f}, SMA21: {last_sma21:.2f}, Volume: {last_volume:.2f}, Média Volume: {avg_volume:.2f}")

    if last_volume > avg_volume * 0.85:
        buy_conditions = [
            last_rsi < 50 and last_close > last_sma7,
            last_macd > 0,
            last_stoch_k < 20 and last_stoch_k > last_stoch_d,
            last_sma7 > last_sma21
        ]
        if sum(buy_conditions) >= 2:
            return 'BUY'

        sell_conditions = [
            last_rsi > 50 and last_close < last_sma7,
            last_macd < 0,
            last_stoch_k > 80 and last_stoch_k < last_stoch_d,
            last_sma7 < last_sma21
        ]
        if sum(sell_conditions) >= 2:
            return 'SELL'

    return None

def set_leverage(pair):
    try:
        client.futures_change_leverage(symbol=pair, leverage=ALAVANCAGEM)
    except:
        pass

def open_trade(pair, side):
    global open_positions
    price = float(client.futures_symbol_ticker(symbol=pair)['price'])
    min_qty = get_min_qty(pair)
    step_size = get_step_size(pair)
    raw_qty = (QTY_USDT * ALAVANCAGEM) / price
    qty = round_step_size(raw_qty, step_size)

    if qty < min_qty:
        print(Fore.RED + f"[{pair}] ❌ Quantidade ({qty}) abaixo do mínimo ({min_qty}) — Ordem cancelada" + Style.RESET_ALL)
        return

    order_side = SIDE_BUY if side == 'BUY' else SIDE_SELL
    client.futures_create_order(
        symbol=pair,
        side=order_side,
        type=FUTURE_ORDER_TYPE_MARKET,
        quantity=qty
    )
    print(Fore.GREEN + f"[{pair}] ✅ ORDEM ENVIADA: {side} | Quantidade: {qty} | Preço: {price}" + Style.RESET_ALL)
    open_positions[pair] = {'entry': price, 'side': side, 'qty': qty}

    # Iniciar monitoramento em tempo real em nova thread
    t = Thread(target=monitor_trade_realtime, args=(pair,))
    t.start()

def monitor_trade_realtime(pair):
    global lucro_total, open_positions

    data = open_positions[pair]
    side = data['side']
    qty = data['qty']
    entry = data['entry']

    seconds = 0

    while True:
        price = float(client.futures_symbol_ticker(symbol=pair)['price'])

        # Cálculo do lucro em %
        pnl_pct = ((price - entry) / entry) * 100 if side == 'BUY' else ((entry - price) / entry) * 100

        # Print a cada 10 segundos
        if seconds % 10 == 0:
            print(Fore.BLUE + f"[{pair}] ⏱ Acompanhando | Lucro: {pnl_pct:.2f}%" + Style.RESET_ALL)

        # Condição de encerramento por STOP ou GAIN em %
        if pnl_pct <= STOP_LOSS_PCT or pnl_pct >= TAKE_PROFIT_PCT:
            close_side = SIDE_SELL if side == 'BUY' else SIDE_BUY
            client.futures_create_order(
                symbol=pair,
                side=close_side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty
            )
            print(Fore.YELLOW + f"[{pair}] 🔒 Posição encerrada | Resultado: {pnl_pct:.2f}%" + Style.RESET_ALL)
            lucro_total += pnl_pct
            del open_positions[pair]
            break

        time.sleep(1)
        seconds += 1

def monitor_trades():
    global open_positions, lucro_total
    while True:
        print(Fore.CYAN + f"\n📊 Status geral - {len(open_positions)} posição(ões) aberta(s) | Lucro acumulado: {lucro_total:.2f}%" + Style.RESET_ALL)

        for pair, data in open_positions.items():
            side = data['side']
            entry = data['entry']
            qty = data['qty']
            price = float(client.futures_symbol_ticker(symbol=pair)['price'])
            pnl_pct = ((price - entry) / entry) * 100 if side == 'BUY' else ((entry - price) / entry) * 100

            print(f"[{pair}] ➤ {side} | Entrada: {entry:.2f} | Atual: {price:.2f} | Lucro: {pnl_pct:.2f}%")

        time.sleep(10)


def check_limits():
    if lucro_total <= STOP_LOSS_GLOBAL:
        print(Fore.RED + f"🛑 STOP LOSS diário atingido! Lucro total: ${lucro_total:.2f}" + Style.RESET_ALL)
        return False
    if lucro_total >= STOP_GAIN_GLOBAL:
        print(Fore.GREEN + f"🎯 META DE LUCRO diária atingida! Lucro total: ${lucro_total:.2f}" + Style.RESET_ALL)
        return False
    return True

def main():
    print(Fore.CYAN + "🚀 Robô de Scalping iniciado com sucesso!" + Style.RESET_ALL)

    # Inicia monitoramento geral em thread paralela
    Thread(target=monitor_trades, daemon=True).start()

    while True:
        if not check_limits():
            break

        if len(open_positions) >= MAX_OPERACOES_ATIVAS:
            print(Fore.LIGHTBLACK_EX + f"⏸ Aguardando espaço para novas ordens..." + Style.RESET_ALL)
            time.sleep(5)
            continue

        for pair in PAIRS:
            if pair in open_positions:
                continue
            set_leverage(pair)
            df = get_klines(pair)
            signal = signal_generator(df, pair)
            if signal:
                print(Fore.MAGENTA + f"[{pair}] 📈 SINAL DETECTADO: {signal}" + Style.RESET_ALL)
                open_trade(pair, signal)
        time.sleep(1)
        
main()
