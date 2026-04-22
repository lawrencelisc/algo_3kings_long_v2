import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
import sys
import json
from datetime import datetime

# 嘗試導入Telegram Bot
try:
    from telegram_bot import telegram_notifier
    TELEGRAM_ENABLED = telegram_notifier.enabled
    if TELEGRAM_ENABLED:
        print("✅ Telegram Bot 已成功加載")
    else:
        print("⚠️ Telegram Bot 配置不完整，請檢查.env中的 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
except ImportError:
    TELEGRAM_ENABLED = False
    print("⚠️ Telegram Bot模塊未找到，通知功能將禁用")
except Exception as e:
    TELEGRAM_ENABLED = False
    print(f"⚠️ Telegram Bot 初始化失敗: {e}")

# ==========================================
# ⚙️ [系統/參數] 模組初始化與 API 配置 V6.7 BugFixed
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_Long_V6.7_BugFixed')

from dotenv import load_dotenv
load_dotenv()

API_KEY    = os.getenv('BYBIT_API_KEY')
API_SECRET = os.getenv('BYBIT_SECRET')

if not API_KEY or not API_SECRET:
    logger.error("❌ API keys not found! Please set BYBIT_API_KEY and BYBIT_SECRET in .env file.")
    sys.exit(1)

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'rateLimit': 120,
    'options': {'defaultType': 'swap'},
})

# 添加 load_markets() 的重試機制
max_retries = 3
for retry in range(max_retries):
    try:
        exchange.load_markets()
        logger.info("✅ 交易所市場信息加載成功")
        break
    except Exception as e:
        if retry == max_retries - 1:
            logger.error(f"❌ 加載市場信息失敗 {max_retries} 次: {str(e)[:100]}...")
            logger.warning("⚠️ 將嘗試繼續運行，但某些功能可能受限")
            # 不退出，讓程式繼續嘗試
        else:
            wait_time = 2 ** retry
            logger.warning(f"⚠️ 加載市場信息失敗 (嘗試 {retry+1}/{max_retries}): {str(e)[:100]}...")
            logger.warning(f"   等待 {wait_time} 秒後重試...")
            time.sleep(wait_time)

# 符號轉換輔助函數
def convert_to_bybit_symbol(ccxt_symbol):
    """
    將 CCXT 格式的符號轉換為 Bybit 需要的格式
    例如: 'BTC/USDT:USDT' -> 'BTCUSDT'
    """
    try:
        # 如果 load_markets() 成功，使用 exchange.market_id()
        return exchange.market_id(ccxt_symbol)
    except:
        # 如果 load_markets() 失敗，使用手動映射
        # 移除 '/USDT:USDT' 後綴，只保留基礎幣種部分
        if ccxt_symbol.endswith('/USDT:USDT'):
            base = ccxt_symbol.replace('/USDT:USDT', '')
            return f"{base}USDT"
        return ccxt_symbol

# ==========================================
# ⚙️ [FIX-SIM] Simulation 模式開關
#
# 設定方式（三選一）：
#   .env 加入  SIMULATION_MODE=true
#   Shell      export SIMULATION_MODE=true
#   直接改     SIMULATION_MODE = True
#
# Sim 模式差異：
#   - 所有下單/取消/槓桿設定 → 本地模擬帳本
#   - fetch_balance            → sim_balance（本地）
#   - fetch_positions          → sim_positions（本地）
#   - 公開 API（K線、Ticker、Trades）仍使用真實交易所數據
#   - 所有交易記錄寫入獨立 CSV 以利事後分析
# ==========================================
SIMULATION_MODE = os.getenv('SIMULATION_MODE', 'false').lower() == 'true'

# Sim 帳本狀態（僅 SIMULATION_MODE=True 時有效）
SIM_INITIAL_BALANCE = float(os.getenv('SIM_BALANCE', '1000.0'))
sim_balance         = SIM_INITIAL_BALANCE   # 可用 USDT 餘額
sim_equity          = SIM_INITIAL_BALANCE   # 總資產（含未實現 PnL）
sim_positions: dict = {}                    # {symbol: {amount, entry_price, tp, sl, ...}}
sim_trade_count     = 0                     # 累計成交筆數
sim_total_pnl       = 0.0                   # 累計已實現 PnL

# ==========================================
# 📁 檔案與路徑設定
# ==========================================
LOG_DIR    = "result"
STATUS_DIR = "../status"

# Sim/Live 使用不同 CSV，避免數據污染
_mode_tag      = "sim" if SIMULATION_MODE else "live"
LOG_FILE       = f"{LOG_DIR}/{_mode_tag}_long_log.csv"
STATUS_FILE    = f"{STATUS_DIR}/btc_regime_long.csv"
BLACKLIST_FILE = f"{STATUS_DIR}/dynamic_blacklist_long.json"

if not os.path.exists(LOG_DIR):    os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

if SIMULATION_MODE:
    print("=" * 60)
    print("🔵 SIMULATION MODE 已啟動")
    print(f"   初始資金  : ${SIM_INITIAL_BALANCE:.2f}")
    print(f"   交易日誌  : {LOG_FILE}")
    print(f"   數據來源  : Bybit 真實行情（公開 API）")
    print("=" * 60)

# 系統狀態記憶體
positions          = {}
cooldown_tracker   = {}
consecutive_losses = {}
recent_sl_times    = []  # Cascade Pause 追蹤器

# ADX 和 Score 趨勢追蹤
_last_scout_adx    = 0.0   # 上次 scout 的 ADX
_last_scout_score  = 0.0   # 上次 scout 的 Score

# 市場狀態記憶（用於Telegram通知）
_last_market_signal = 0     # 上次市場信號
_last_market_notification_time = 0  # 上次通知時間

# ==========================================
# ⚙️ [系統/參數] 策略與風控全局變數
# ==========================================
WORKING_CAPITAL        = 1000.0
MAX_LEVERAGE           = 10.0
RISK_PER_TRADE         = 0.005
MIN_NOTIONAL           = 5.0
MAX_NOTIONAL_PER_TRADE = 200.0

NET_FLOW_SIGMA = 1.2
TP_ATR_MULT    = 5.0
SL_ATR_MULT    = 3.0

MAX_CONSECUTIVE_LOSSES = 3
DYNAMIC_BAN_DURATION   = 86400

MAX_CONCURRENT_POSITIONS = 3
CASCADE_SL_WINDOW = 180    # 秒
CASCADE_SL_TRIGGER = 2     # 筆

SCOUTING_INTERVAL       = 125
POSITION_CHECK_INTERVAL = 4

BRAKE_ADX_HIGH_THRESHOLD = 40
TIMEOUT_SECONDS          = 2700

ACTIVE_LONG_SIGNALS  = [2]         # 測試期只用 +2 Trend Long
ACTIVE_SHORT_SIGNALS = [-2, -3]    # 排除 -1 MR Short

# ==========================================
# 🚀 緩存設定
# ==========================================
REGIME_CACHE_TTL    = 60
POSITIONS_CACHE_TTL = 8
ATR_CACHE_TTL       = 60

_regime_cache    = {'data': None, 'ts': 0}
_positions_cache = {'data': None, 'ts': 0}
_atr_cache       = {}

BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT',  'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT','USDP/USDT:USDT',  'EURS/USDT:USDT',
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT',  'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT',   'RLUSD/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT',  'WAVAX/USDT:USDT',
    'stETH/USDT:USDT','cbETH/USDT:USDT','WHT/USDT:USDT'
]

WHITELIST = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'DOGE/USDT:USDT',
    'DOT/USDT:USDT', 'MATIC/USDT:USDT', 'LINK/USDT:USDT', 'UNI/USDT:USDT',
    'PEPE/USDT:USDT', 'SHIB/USDT:USDT', 'ARB/USDT:USDT', 'OP/USDT:USDT',
    'APT/USDT:USDT', 'SUI/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT'
]

CSV_COLUMNS = [
    'timestamp', 'symbol', 'action', 'price', 'amount', 'trade_value',
    'atr', 'net_flow', 'tp_price', 'sl_price', 'reason',
    'realized_pnl', 'actual_balance', 'effective_balance',
    'sim_mode', 'sim_equity', 'sim_total_pnl',
    'regime_signal', 'mean_adx', 'market_score'    # ← 新增
]
STATUS_COLUMNS = [
    'timestamp', 'btc_price', 'target_price', 'hma20', 'hma50',
    'adx', 'signal_code', 'decision_text'
]


# ==========================================
# 🛠️ [輔助模組] 記錄、帳戶與訂單管理
# ==========================================
def log_to_csv(data_dict):
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # [FIX-SIM] Sim 模式自動標記
    if SIMULATION_MODE:
        row['sim_mode']      = 'SIM'
        row['sim_equity']    = round(sim_equity, 4)
        row['sim_total_pnl'] = round(sim_total_pnl, 4)
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(
        LOG_FILE, mode='a', index=False, header=not os.path.exists(LOG_FILE)
    )


def log_status_to_csv(data_dict):
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(
        STATUS_FILE, mode='a', index=False, header=not os.path.exists(STATUS_FILE)
    )


# ==========================================
# 🔵 [FIX-SIM] Simulation 帳本核心函數
# ==========================================
def sim_open_long(symbol, amount, price):
    """
    模擬開多單
    - 假設即時以 price 成交（IOC 市價近似）
    - 收取 Bybit taker fee 0.055%
    - 返回 (actual_amount, actual_price) 或 (0, 0) 若餘額不足
    """
    global sim_balance, sim_trade_count
    fee      = amount * price * 0.00055   # Bybit taker fee
    cost     = amount * price + fee
    if sim_balance < cost:
        logger.warning(f"🔵 [SIM] {symbol} 餘額不足 (需 {cost:.2f}, 有 {sim_balance:.2f})")
        return 0, 0
    sim_balance     -= cost
    sim_trade_count += 1
    logger.info(f"🔵 [SIM] OPEN LONG {symbol} | 數量:{amount} @ {price:.4f} | 費用:{fee:.4f} | 餘額:{sim_balance:.2f}")
    return amount, price


def sim_close_long(symbol, amount, price):
    """
    模擬平多單
    - 收取 taker fee
    - 更新 sim_balance、sim_total_pnl、sim_equity
    - 返回 realized_pnl
    """
    global sim_balance, sim_total_pnl, sim_equity, sim_trade_count
    if symbol not in sim_positions:
        logger.warning(f"🔵 [SIM] {symbol} 找不到持倉，無法平倉")
        return 0.0
    pos         = sim_positions[symbol]
    entry_price = pos['entry_price']
    fee         = amount * price * 0.00055
    gross_pnl   = (price - entry_price) * amount
    net_pnl     = gross_pnl - fee
    proceeds    = amount * price - fee
    sim_balance    += proceeds
    sim_total_pnl  += net_pnl
    sim_trade_count += 1
    sim_equity      = sim_balance + sum(
        sim_positions[s]['amount'] * exchange.fetch_ticker(s)['last']
        for s in sim_positions if s != symbol
    ) if len(sim_positions) > 1 else sim_balance
    logger.info(
        f"🔵 [SIM] CLOSE LONG {symbol} | 出場:{price:.4f} 入場:{entry_price:.4f} "
        f"| PnL:{net_pnl:+.4f} | 總PnL:{sim_total_pnl:+.4f} | 餘額:{sim_balance:.2f}"
    )
    return round(net_pnl, 4)


def sim_get_positions():
    """
    將 sim_positions 轉換為與 exchange.fetch_positions() 相同的格式
    供 get_live_positions_cached() 統一處理
    """
    result = []
    for symbol, pos in sim_positions.items():
        result.append({
            'symbol':     symbol,
            'side':       'long',
            'contracts':  pos['amount'],
            'entryPrice': pos['entry_price'],
            'stopLoss':   pos.get('sl_price', 0),
            'takeProfit': pos.get('tp_price', 0),
            'info':       {'side': 'Buy'},
            'createdTime': pos.get('entry_time', time.time()) * 1000
        })
    return result


def sim_report():
    """列印 Simulation 績效摘要"""
    global sim_equity
    # 更新 equity（含未實現 PnL）
    unrealized = 0.0
    for symbol, pos in sim_positions.items():
        try:
            curr_p    = exchange.fetch_ticker(symbol)['last']
            unrealized += (curr_p - pos['entry_price']) * pos['amount']
        except:
            pass
    sim_equity = sim_balance + unrealized
    roi        = (sim_equity - SIM_INITIAL_BALANCE) / SIM_INITIAL_BALANCE * 100

    print("=" * 60)
    print("📊 [SIM] 績效摘要")
    print(f"   初始資金       : ${SIM_INITIAL_BALANCE:.2f}")
    print(f"   可用餘額       : ${sim_balance:.2f}")
    print(f"   未實現 PnL     : ${unrealized:+.4f}")
    print(f"   總資產 (Equity): ${sim_equity:.2f}")
    print(f"   累計已實現 PnL : ${sim_total_pnl:+.4f}")
    print(f"   ROI            : {roi:+.2f}%")
    print(f"   總成交筆數     : {sim_trade_count}")
    print(f"   當前持倉       : {list(sim_positions.keys())}")
    print("=" * 60)


# ==========================================
# 🛠️ Sim/Live 統一介面
# ==========================================
def get_live_usdt_balance():
    """[FIX-SIM] Sim 模式回傳本地餘額，Live 模式呼叫交易所"""
    if SIMULATION_MODE:
        return sim_balance
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    """[FIX-SIM] Sim 模式跳過取消訂單（無掛單概念）"""
    if SIMULATION_MODE:
        logger.debug(f"🔵 [SIM] {symbol} skip cancel_all_v5")
        return
    try:
        exchange.cancel_all_orders(symbol, params={'category': 'linear'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'tpslOrder'})
    except:
        pass
    try:
        exchange.private_post_v5_position_trading_stop({
            'category': 'linear', 'symbol': exchange.market_id(symbol),
            'takeProfit': "0", 'stopLoss': "0", 'positionIdx': 0
        })
    except:
        pass


def process_native_exit_log(symbol, pos, position_type='long'):
    """
    [FIX-SIM] Native Exit PnL 結算
    - Sim 模式：用公開 ticker 計算 PnL，不呼叫私有 API
    - Live 模式：原有邏輯
    """
    if SIMULATION_MODE:
        try:
            curr_p   = exchange.fetch_ticker(symbol)['last']
        except:
            curr_p = pos['entry_price']
        real_pnl = round((curr_p - pos['entry_price']) * pos['amount'], 4)
        log_to_csv({
            'symbol': symbol, 'action': 'NATIVE_EXIT',
            'price': curr_p, 'amount': pos['amount'],
            'reason': 'Sim Native TP/SL', 'realized_pnl': real_pnl
        })
        return real_pnl

    # ── Live 原有邏輯 ──
    real_exit_price = pos['entry_price']
    real_pnl        = 0.0
    try:
        pnl_res = exchange.private_get_v5_position_closed_pnl({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'limit': 1
        })
        if pnl_res and pnl_res.get('result') and pnl_res['result'].get('list'):
            last_trade      = pnl_res['result']['list'][0]
            real_exit_price = float(last_trade['avgExitPrice'])
            real_pnl        = float(last_trade['closedPnl'])
        else:
            raise ValueError("empty")
    except Exception as e:
        logger.debug(f"⚠️ {symbol} PnL 備用估算: {e}")
        try:
            curr_p          = exchange.fetch_ticker(symbol)['last']
            real_exit_price = curr_p
            real_pnl        = round((curr_p - pos['entry_price']) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol, 'action': 'NATIVE_EXIT', 'price': real_exit_price,
        'amount': pos['amount'], 'reason': 'Bybit Native TP/SL', 'realized_pnl': real_pnl
    })
    return real_pnl


def get_live_positions_cached():
    """
    [FIX-SIM] fetch_positions 統一介面
    - Sim 模式：回傳本地 sim_positions（無 cache TTL 限制）
    - Live 模式：8 秒緩存版
    """
    if SIMULATION_MODE:
        return sim_get_positions()

    if (time.time() - _positions_cache['ts']) < POSITIONS_CACHE_TTL and _positions_cache['data'] is not None:
        return _positions_cache['data']
    try:
        data = exchange.fetch_positions(params={'category': 'linear'})
        _positions_cache['data'] = data
        _positions_cache['ts']   = time.time()
        return data
    except Exception as e:
        logger.warning(f"⚠️ fetch_positions 失敗: {e}")
        return _positions_cache['data'] or []


# ==========================================
# 🛠️ 其他輔助函數（Sim/Live 共用）
# ==========================================
def get_3_layer_avg_price(symbol, side='bids'):
    try:
        ob     = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([lv[0] for lv in levels]) / len(levels)
    except:
        return None


def get_market_metrics(symbol):
    """ATR 計算（60 秒緩存，Sim/Live 共用公開 API）"""
    cached = _atr_cache.get(symbol)
    if cached and (time.time() - cached['ts']) < ATR_CACHE_TTL:
        return cached['atr'], cached['is_volatile']

    max_retries = 2
    for retry in range(max_retries):
        try:
            # 將 CCXT 符號格式轉換為 Bybit 需要的格式
            market_symbol = convert_to_bybit_symbol(symbol)
            ohlcv = exchange.fetch_ohlcv(
                market_symbol,
                timeframe='5m',
                limit=100,  # 減少數據量
                params={'category': 'linear'}
            )
            
            df    = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
            df['tr'] = np.maximum(
                df['h'] - df['l'],
                np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1)))
            )
            atr         = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]
            is_volatile = (atr / df['c'].iloc[-1]) > 0.0015

            if pd.isna(atr) or atr == 0:
                return None, False

            _atr_cache[symbol] = {'atr': atr, 'is_volatile': is_volatile, 'ts': time.time()}
            return atr, is_volatile
            
        except Exception as e:
            if retry == max_retries - 1:
                logger.warning(f"⚠️ {symbol} ATR計算失敗: {str(e)[:80]}...")
                return None, False
            time.sleep(2 ** retry)  # 指數退避
    
    return None, False


def fetch_tickers_for_positions(symbols):
    """批次取得持倉現價（Sim/Live 共用公開 API）"""
    if not symbols:
        return {}
    try:
        result = exchange.fetch_tickers(symbols)
        return {s: t['last'] for s, t in result.items() if t.get('last')}
    except Exception as e:
        logger.warning(f"⚠️ batch fetch_tickers 失敗，逐一降級: {e}")
        prices = {}
        for s in symbols:
            try:
                prices[s] = exchange.fetch_ticker(s)['last']
                time.sleep(0.05)
            except:
                pass
        return prices


# ==========================================
# 🛠️ JSON 記憶與動態黑名單
# ==========================================
def save_dynamic_blacklist():
    data = {'consecutive_losses': consecutive_losses, 'cooldown_tracker': cooldown_tracker}
    try:
        with open(BLACKLIST_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except:
        pass


def load_dynamic_blacklist():
    global consecutive_losses, cooldown_tracker
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, 'r') as f:
                data = json.load(f)
            consecutive_losses.update(data.get('consecutive_losses', {}))
            cooldown_tracker.update(data.get('cooldown_tracker', {}))
            curr_t  = time.time()
            expired = [k for k, v in cooldown_tracker.items() if v < curr_t]
            for k in expired:
                del cooldown_tracker[k]
                if k in consecutive_losses: del consecutive_losses[k]
            if expired: save_dynamic_blacklist()
        except:
            pass


def handle_trade_result(symbol, pnl):
    global consecutive_losses, cooldown_tracker, recent_sl_times
    if pnl > 0:
        consecutive_losses[symbol] = 0
        if symbol in cooldown_tracker: del cooldown_tracker[symbol]
    elif pnl < 0:
        consecutive_losses[symbol] = consecutive_losses.get(symbol, 0) + 1
        recent_sl_times.append(time.time())  # 記錄SL時間
        if consecutive_losses[symbol] >= MAX_CONSECUTIVE_LOSSES:
            cooldown_tracker[symbol] = time.time() + DYNAMIC_BAN_DURATION
        else:
            cooldown_tracker[symbol] = max(
                cooldown_tracker.get(symbol, 0), time.time() + 480
            )
    save_dynamic_blacklist()


# ==========================================
# ╔══════════════════════════════════════╗
# ║  🔧 BUG FIX 1：score vs mr_thr      ║
# ║     量綱錯誤修復                      ║
# ║  🔧 BUG FIX 2：MACRO_BEAR_CONSEC    ║
# ║     > 資料長度修復                    ║
# ║  🔧 BUG FIX 3：rolling_7d_return    ║
# ║     np.roll 計算錯誤修復              ║
# ╚══════════════════════════════════════╝
# ==========================================
def get_btc_regime_v3_fast():
    """
    雙向市場狀態檢測器 V6.7（三項 Bug 修復版）

    ┌─────────────────────────────────────────────────────────┐
    │ BUG FIX 1：score 量綱修復                               │
    │   舊版：mr_thr = percentile(ADX_values, 70) ≈ 30~50    │
    │         score ∈ [0, 1]  → score >= mr_thr 永遠 False   │
    │   新版：MR_SCORE_THR = 0.55（固定，與 score 同量綱）    │
    │         TR_SCORE_THR = 0.35                             │
    │         all_scores 改為追蹤複合分數，供 percentile 用   │
    │                                                         │
    │ BUG FIX 2：MACRO_BEAR_CONSEC 修復                      │
    │   舊版：RET_7D_BARS = 2016，CONSEC = 144               │
    │         但 limit=300 → consec 永遠累不到 144            │
    │   新版：RET_7D_BARS = 288（1天，在300根內有效）         │
    │         MACRO_BEAR_CONSEC = 36（3小時，可在300根內觸發）│
    │                                                         │
    │ BUG FIX 3：rolling_7d_return np.roll 修復              │
    │   舊版：np.roll(closes, win) 前 win 個值是陣列末尾，   │
    │         計算出來的收益率在 index < win 全部是垃圾值     │
    │   新版：直接切片 closes[:-win] / closes[win:]           │
    │         只計算 index >= win 的部分                      │
    └─────────────────────────────────────────────────────────┘

    信號類型：
      +1  MR 多頭（均值回歸看漲）
      +2  趨勢多頭
      -1  MR 空頭
      -2  趨勢空頭
      -3  熊市強制空頭
       0  無信號（高波動或條件不滿足）
    """
    if (time.time() - _regime_cache['ts']) < REGIME_CACHE_TTL and _regime_cache['data'] is not None:
        return _regime_cache['data']

    try:
        TIMEFRAME = '5m'
        OHLCV_LIMIT = 300           # fetch 根數

        # ── 技術指標參數 ──
        ADX_WIN    = 14
        BB_WIN     = 20
        ZSCORE_WIN = 60
        ATR_WIN    = 14
        EMA_WIN    = 21

        # ── [BUG FIX 1] 固定閾值，與 score ∈ [0,1] 同量綱 ──
        MR_SCORE_THR   = 0.55   # score > 0.55 → MR 市況
        TR_SCORE_THR   = 0.40   # score ≤ 0.40 → 趨勢市況

        # ── Z-Score 百分位（保留動態，量綱本身就是 z-score）──
        Z_LONG_PCT  = 20
        Z_SHORT_PCT = 80

        # ── EMA / BB 參數 ──
        EMA_SLOPE_BARS = 3
        TR_BB_PCT      = 60

        # ── 高波動參數 ──
        HVOL_ATR_PCT = 85

        # ── [BUG FIX 2] Macro Bear：調整為 300 根資料內有效的值 ──
        #   RET_7D_BARS      = 288  →  1天 (288根 × 5min = 1440min = 24h)
        #   MACRO_BEAR_CONSEC = 36  →  3小時 (36根 × 5min = 180min)
        #   MACRO_BULL_RTN_THR 不變
        RET_7D_BARS        = 288    # [BUG FIX 2] 舊值 2016 → 288
        MACRO_BEAR_RTN_THR = -0.04
        MACRO_BULL_RTN_THR = +0.03
        MACRO_BEAR_CONSEC  = 36     # [BUG FIX 2] 舊值 144 → 36

        # ── 權重（四個指標等權）──
        W1, W2, W3, W4 = 0.25, 0.25, 0.25, 0.25

        REGIME_ASSETS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

        # ────────────────────────────────────────────────
        # 內部指標函數
        # ────────────────────────────────────────────────
        def rolling_adx_simple(highs, lows, closes, win=ADX_WIN):
            """Wilder 平滑 ADX（純 NumPy，已向量化 TR/DM）"""
            n   = len(closes)
            adx = np.full(n, 25.0)
            pdi = np.full(n, 25.0)
            ndi = np.full(n, 25.0)

            prev_h = np.roll(highs, 1);  prev_h[0] = highs[0]
            prev_l = np.roll(lows,  1);  prev_l[0] = lows[0]
            prev_c = np.roll(closes, 1); prev_c[0] = closes[0]

            hl  = highs - lows
            hpc = np.abs(highs - prev_c)
            lpc = np.abs(lows  - prev_c)
            tr  = np.maximum(hl, np.maximum(hpc, lpc))
            tr[0] = hl[0]

            up  = highs - prev_h
            dn  = prev_l - lows
            pdm = np.where((up > dn) & (up > 0), up, 0.0)
            ndm = np.where((dn > up) & (dn > 0), dn, 0.0)

            if n > win:
                atr_s = np.zeros(n); pdm_s = np.zeros(n); ndm_s = np.zeros(n)
                atr_s[win] = tr[1:win+1].sum()
                pdm_s[win] = pdm[1:win+1].sum()
                ndm_s[win] = ndm[1:win+1].sum()
                for i in range(win+1, n):
                    atr_s[i] = atr_s[i-1] - atr_s[i-1]/win + tr[i]
                    pdm_s[i] = pdm_s[i-1] - pdm_s[i-1]/win + pdm[i]
                    ndm_s[i] = ndm_s[i-1] - ndm_s[i-1]/win + ndm[i]

                with np.errstate(divide='ignore', invalid='ignore'):
                    _pdi = np.where(atr_s > 0, 100*pdm_s/atr_s, 0.0)
                    _ndi = np.where(atr_s > 0, 100*ndm_s/atr_s, 0.0)
                    dx   = np.where((_pdi+_ndi) > 0,
                                    100*np.abs(_pdi-_ndi)/(_pdi+_ndi), 0.0)

                adx[2*win] = dx[win:2*win].mean()
                for i in range(2*win+1, n):
                    adx[i] = (adx[i-1]*(win-1) + dx[i]) / win
                adx[:2*win] = adx[2*win]
                pdi[win:] = _pdi[win:]; pdi[:win] = _pdi[win]
                ndi[win:] = _ndi[win:]; ndi[:win] = _ndi[win]

            return adx, pdi, ndi

        def rolling_bbwidth_fast(closes, win=BB_WIN):
            s   = pd.Series(closes)
            mid = s.rolling(win).mean()
            std = s.rolling(win).std(ddof=0)
            bbw = (4*std / mid.replace(0, np.nan)).fillna(0.0).values.copy()
            fv  = win - 1
            if len(bbw) > fv and bbw[fv] != 0.0:
                bbw[:fv] = bbw[fv]
            return bbw

        def rolling_zscore_fast(closes, win=ZSCORE_WIN):
            s  = pd.Series(closes)
            mu = s.rolling(win).mean()
            sg = s.rolling(win).std(ddof=0)
            return ((s - mu) / sg.replace(0, np.nan)).fillna(0.0).values

        def rolling_ema(closes, win=EMA_WIN):
            ema   = np.zeros(len(closes))
            alpha = 2.0 / (win + 1)
            ema[0] = closes[0]
            for i in range(1, len(closes)):
                ema[i] = alpha*closes[i] + (1-alpha)*ema[i-1]
            return ema

        def rolling_atr_pct_fast(highs, lows, closes, win=ATR_WIN):
            prev_c = np.roll(closes, 1); prev_c[0] = closes[0]
            tr     = np.maximum(highs-lows,
                                np.maximum(np.abs(highs-prev_c), np.abs(lows-prev_c)))
            tr[0]  = highs[0] - lows[0]
            atr    = pd.Series(tr).ewm(span=win, adjust=False).mean().values
            return np.where(closes > 0, atr/closes, 0.0)

        # ── [BUG FIX 3] rolling_7d_return：使用切片取代 np.roll ──
        def rolling_return(closes, win=RET_7D_BARS):
            """
            計算 win 根 K 線前的收益率
            舊版 np.roll 問題：
              np.roll(arr, 5) → arr[-5:] 被移到 arr[:5]，
              導致 index < win 的位置包含陣列末尾的值，
              計算出來的收益率在最早期的 win 根是垃圾值。
            新版：只計算 index >= win 的部分，其餘填 0.0
            """
            n      = len(closes)
            ret    = np.zeros(n)
            if n <= win:
                return ret                     # 資料不足，全部返回 0
            prev   = closes[:-win]             # closes[0 .. n-win-1]
            curr   = closes[win:]              # closes[win .. n-1]
            valid  = prev > 0
            ret[win:] = np.where(valid, (curr - prev) / prev, 0.0)
            return ret
        # ── [BUG FIX 3 END] ──

        # ────────────────────────────────────────────────
        # 資料收集
        # ────────────────────────────────────────────────
        print("📊 開始計算市場狀態信號...")
        regime_data = {}

        # [BUG FIX 1] 改為追蹤複合 score，不再混入 ADX
        all_scores_list  = []   # 收集各幣各 bar 的複合 score
        all_z_scores     = []
        all_bbw          = []
        all_atr_pct      = []
        all_ret_list     = []

        for sym in REGIME_ASSETS:
            try:
                # 將 CCXT 符號格式轉換為 Bybit 需要的格式
                market_symbol = convert_to_bybit_symbol(sym)
                ohlcv = exchange.fetch_ohlcv(
                    market_symbol,
                    timeframe=TIMEFRAME,
                    limit=OHLCV_LIMIT,
                    params={'category': 'linear'}
                )
                if len(ohlcv) < 100:
                    continue

                df     = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
                closes = df['c'].values.astype(float)
                highs  = df['h'].values.astype(float)
                lows   = df['l'].values.astype(float)

                adx, pdi, ndi = rolling_adx_simple(highs, lows, closes)
                bbw           = rolling_bbwidth_fast(closes)
                zscore        = rolling_zscore_fast(closes)
                ema21         = rolling_ema(closes, EMA_WIN)
                ema_slope     = np.diff(ema21, prepend=ema21[0])
                atr_pct       = rolling_atr_pct_fast(highs, lows, closes)
                ret_arr       = rolling_return(closes, RET_7D_BARS)  # [BUG FIX 3]

                regime_data[sym] = {
                    'closes': closes, 'adx': adx, 'bbw': bbw,
                    'zscore': zscore, 'ema_slope': ema_slope,
                    'atr_pct': atr_pct, 'ret': ret_arr,
                    'pdi': pdi, 'ndi': ndi
                }

                # ── [BUG FIX 1] 用於統計閾值的資料 ──
                # 取最近 50 bar 計算分位數邊界
                start_idx = max(0, len(closes) - 50)

                # 逐 bar 計算複合 score 並加入 all_scores_list
                adx_vals  = adx[start_idx:]
                bbw_vals  = bbw[start_idx:]
                z_vals    = zscore[start_idx:]
                atr_vals  = atr_pct[start_idx:]
                ret_vals  = ret_arr[start_idx:]

                # 前置計算 bar-level score（暫用 adx/bbw/z 的三項近似，Hurst 後續可擴充）
                adx_lo_b = np.percentile(adx_vals, 10) if len(adx_vals) else 20
                adx_hi_b = np.percentile(adx_vals, 90) if len(adx_vals) else 50
                bbw_lo_b = np.percentile(bbw_vals, 10) if len(bbw_vals) else 0.01
                bbw_hi_b = np.percentile(bbw_vals, 90) if len(bbw_vals) else 0.1
                z_abs    = np.abs(z_vals)
                z_lo_b   = np.percentile(z_abs, 10) if len(z_abs) else 0.0
                z_hi_b   = np.percentile(z_abs, 90) if len(z_abs) else 2.0

                def _norm_arr(arr, lo, hi):
                    span = hi - lo
                    if span <= 0: return np.full(len(arr), 0.5)
                    return np.clip((arr - lo) / span, 0.0, 1.0)

                adx_n_arr = _norm_arr(adx_vals, adx_lo_b, adx_hi_b)
                bbw_n_arr = _norm_arr(bbw_vals, bbw_lo_b, bbw_hi_b)
                z_n_arr   = _norm_arr(z_abs,    z_lo_b,   z_hi_b)

                # score ∈ [0, 1]，與 MR_SCORE_THR / TR_SCORE_THR 同量綱
                bar_scores = (W1*(1 - adx_n_arr) +
                              W2*(1 - bbw_n_arr) +
                              W3*0.5 +             # Hurst 固定（待後續改進）
                              W4*z_n_arr)
                all_scores_list.extend(bar_scores.tolist())

                all_z_scores.extend(z_vals.tolist())
                all_bbw.extend(bbw_vals.tolist())
                all_atr_pct.extend(atr_vals.tolist())
                all_ret_list.extend(ret_vals[ret_vals != 0.0].tolist())  # 去除補零

            except Exception as e:
                logger.warning(f"⚠️ {sym} 指標計算失敗: {e}")
                continue

        if not regime_data:
            result = {'signal': 0, 'brake': False, 'soft_brake': False,
                      'brake_reason': 'No data', 'regime_signal': 0}
            _regime_cache['data'] = result
            _regime_cache['ts']   = time.time()
            return result

        # ────────────────────────────────────────────────
        # 統計閾值（Z-Score、BBW 仍動態；score 改固定）
        # ────────────────────────────────────────────────
        def safe_pct(arr, p):
            a = np.asarray(arr)
            return float(np.percentile(a, p)) if len(a) > 0 else 0.0

        # [BUG FIX 1] score 閾值改為固定常數，不再從 all_scores_list 取 percentile
        # （保留 all_scores_list 供日後擴充診斷用）
        mr_thr = MR_SCORE_THR   # 0.55  ← 量綱修復
        tr_thr = TR_SCORE_THR   # 0.35  ← 量綱修復

        zl_thr    = safe_pct(all_z_scores, Z_LONG_PCT)
        zs_thr    = safe_pct(all_z_scores, Z_SHORT_PCT)
        bb_thr    = safe_pct(all_bbw,      TR_BB_PCT)
        bear_z_thr = safe_pct(all_z_scores, 55)
        atr_hi    = safe_pct(all_atr_pct,  HVOL_ATR_PCT)

        # ────────────────────────────────────────────────
        # 最後一根 bar 的市場指標均值
        # ────────────────────────────────────────────────
        last_adx  = []; last_bbw  = []; last_z    = []
        last_atr  = []; last_ndipdi = []

        for sym, data in regime_data.items():
            idx = len(data['closes']) - 1
            if idx >= 0:
                last_adx.append(data['adx'][idx])
                last_bbw.append(data['bbw'][idx])
                last_z.append(data['zscore'][idx])
                last_atr.append(data['atr_pct'][idx])
                last_ndipdi.append(data['ndi'][idx] - data['pdi'][idx])

        if not last_adx:
            result = {'signal': 0, 'brake': False, 'soft_brake': False,
                      'brake_reason': 'No recent data', 'regime_signal': 0}
            _regime_cache['data'] = result
            _regime_cache['ts']   = time.time()
            return result

        mean_adx    = float(np.mean(last_adx))
        mean_bbw    = float(np.mean(last_bbw))
        mean_z      = float(np.mean(last_z))
        mean_atr    = float(np.mean(last_atr))
        mean_ndipdi = float(np.mean(last_ndipdi))

        # ── 計算當前 bar 的複合 score ──
        def _norm(val, lo, hi):
            return float(np.clip((val - lo) / (hi - lo + 1e-9), 0.0, 1.0))

        all_scores_np = np.array(all_scores_list)
        all_adx_np    = np.array(last_adx)
        all_bbw_np    = np.array(last_bbw)
        all_z_abs_np  = np.abs(np.array(last_z))

        adx_lo = safe_pct(all_scores_np, 10);  adx_hi = safe_pct(all_scores_np, 90)
        bbw_lo = safe_pct(all_bbw,       10);  bbw_hi = safe_pct(all_bbw, 90)
        z_lo   = safe_pct(np.abs(np.array(all_z_scores)), 10)
        z_hi   = safe_pct(np.abs(np.array(all_z_scores)), 90)

        adx_n  = _norm(mean_adx,        adx_lo, adx_hi)
        bbw_n  = _norm(mean_bbw,        bbw_lo, bbw_hi)
        z_n    = _norm(abs(mean_z),     z_lo,   z_hi)
        score  = W1*(1-adx_n) + W2*(1-bbw_n) + W3*0.5 + W4*z_n

        is_highvol = (mean_atr > atr_hi)

        # ────────────────────────────────────────────────
        # [BUG FIX 2] Macro Bear Gate
        # 使用修正後的 rolling_return（win=288, consec=36）
        # 遍歷合併的 all_ret_list 已包含有效收益率
        # ────────────────────────────────────────────────
        # 改為直接用各資產最後 1 根 bar 的 ret 值做投票
        bear_votes = sum(
            1 for sym, data in regime_data.items()
            if len(data['ret']) > 0 and data['ret'][-1] < MACRO_BEAR_RTN_THR
        )
        bull_votes = sum(
            1 for sym, data in regime_data.items()
            if len(data['ret']) > 0 and data['ret'][-1] > MACRO_BULL_RTN_THR
        )
        n_assets    = len(regime_data)
        # 超過半數資產 1 天收益率 < -4% → 熊市閘門開啟
        is_bear     = (bear_votes > n_assets // 2)
        # 超過半數資產 1 天收益率 > +3% → 強制解除熊市
        if bull_votes > n_assets // 2:
            is_bear = False

        # ── EMA 方向（多數決，修正門檻為 60% 而非 50%+1）──
        def _ema_direction(ema_slope_dict, slope_bars):
            up_c = dn_c = 0
            for data in ema_slope_dict.values():
                idx = len(data) - 1
                if idx < slope_bars: continue
                sl  = data[idx-slope_bars+1:idx+1]
                if len(sl) >= slope_bars:
                    if np.all(sl > 0): up_c += 1
                    elif np.all(sl < 0): dn_c += 1
            threshold = max(1, int(n_assets * 0.6))  # 60% 多數決
            if up_c >= threshold: return  1
            if dn_c >= threshold: return -1
            return 0

        ema_dir = _ema_direction(
            {k: v['ema_slope'] for k, v in regime_data.items()}, EMA_SLOPE_BARS
        )

        # ────────────────────────────────────────────────
        # 信號生成
        # ────────────────────────────────────────────────
        regime_signal = 0

        if is_highvol:
            regime_signal = 0
        elif score >= mr_thr:             # [BUG FIX 1] score ∈[0,1] vs 0.55
            if mean_z <= zl_thr:
                regime_signal = 0 if is_bear else +1
            elif mean_z >= zs_thr:
                regime_signal = -1
        elif score <= tr_thr and mean_adx >= 20 and mean_bbw >= bb_thr:
            if mean_ndipdi < -5 and ema_dir == +1:
                regime_signal = 0 if is_bear else +2
            elif mean_ndipdi > +5 and ema_dir == -1:
                regime_signal = -2
        elif is_bear and score >= mr_thr and mean_z >= bear_z_thr:
            regime_signal = -3

        # ── 轉換為 signal / brake 格式（向下兼容）──
        if regime_signal > 0:
            signal = 1; brake = False; soft_brake = False; brake_reason = ""
        elif regime_signal < 0:
            signal = -1; brake = True; soft_brake = False
            brake_reason = f"市場狀態信號: {regime_signal}"
        else:
            signal = 0; brake = False
            soft_brake = True if is_highvol else False
            brake_reason = "高波動期" if is_highvol else "市場狀態中性"

        btc_price = regime_data.get('BTC/USDT:USDT', {}).get('closes', [0])[-1]

        signal_names = {0:"無信號", +1:"MR多頭", +2:"趨勢多頭",
                        -1:"MR空頭", -2:"趨勢空頭", -3:"熊市強制空頭"}
        status_text = f"📊 市場狀態: {signal_names.get(regime_signal,'未知')}"
        if is_highvol: status_text += " ⚠️ 高波動期"
        if is_bear:    status_text += " 🐻 巨集觀熊市"

        log_status_to_csv({
            'btc_price':     round(btc_price, 2) if btc_price else 0,
            'adx':           round(mean_adx, 2),
            'signal_code':   signal,
            'decision_text': status_text
        })

        print("-" * 60)
        current_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"🌐 市場狀態 V6.7 BugFixed（{len(regime_data)} 個資產）[{current_time}]"
              + (" [SIM]" if SIMULATION_MODE else " [LIVE]"))
        
        # 顯示三個資產的現價
        btc_p = regime_data.get('BTC/USDT:USDT', {}).get('closes', [0])[-1]
        eth_p = regime_data.get('ETH/USDT:USDT', {}).get('closes', [0])[-1]
        sol_p = regime_data.get('SOL/USDT:USDT', {}).get('closes', [0])[-1]
        
        # === 合併為一張表：左欄指標名，右欄對應值 ===
        labels = [
            'BTC/ETH/SOL Price', 'Composite Score', 'Z-Score', 'ADX(20/25)',
            'BBW', 'ATR%', 'EMA Direction',
            'HighVol/Bear', 'bear_votes', 'bull_votes', 'Signal', 'Decision'
        ]
        values = [
            f"{btc_p:.0f} / {eth_p:.0f} / {sol_p:.1f}",
            f"{score:.3f} (MR: >={mr_thr:.2f} | TR: <={tr_thr:.2f})",
            f"{mean_z:+.3f} (long: <{zl_thr:.3f} | short: >{zs_thr:.3f})",
            f"{mean_adx:.1f} (>=20 trend | >=25 strong)",
            f"{mean_bbw:.4f} (>={bb_thr:.4f} trend)",
            f"{mean_atr:.4f} (highvol_threshold: {atr_hi:.4f})",
            f"{'↑' if ema_dir==1 else '↓' if ema_dir==-1 else '→'}",
            f"highvol: {'Y' if is_highvol else 'N'} | bear: {'ON' if is_bear else 'OFF'}",
            f"{bear_votes}/{n_assets}",
            f"{bull_votes}/{n_assets}",
            f"{signal_names.get(regime_signal,'No Signal')}",
            status_text
        ]
        max_len = max(len(l) for l in labels)
        pad = max(len(max(labels, key=len)) + 4, 18)
        sep_len = pad + 45

        print("-" * sep_len)
        for lbl, val in zip(labels, values):
            print(f"  {lbl:<{pad}}{val}")
        print("-" * sep_len)
        
        # Telegram市場狀態通知（每小時或信號變化時）
        global _last_market_signal, _last_market_notification_time
        current_time = time.time()
        
        # 檢查是否需要發送通知：
        # 1. 信號發生變化
        # 2. 距離上次通知超過1小時
        # 3. Telegram已啟用
        if (TELEGRAM_ENABLED and 
            (_last_market_signal != regime_signal or 
             current_time - _last_market_notification_time > 3600)):
            
            try:
                # 準備市場數據（完整版）
                market_data = {
                    'signal_names': signal_names.get(regime_signal, '無信號'),
                    'mean_adx': mean_adx,
                    'market_score': score,
                    'is_highvol': is_highvol,
                    'is_bear': is_bear,
                    'btc_price': btc_p,
                    'eth_price': eth_p,
                    'sol_price': sol_p,
                    'positions_count': len(positions),
                    'total_pnl': sim_total_pnl if SIMULATION_MODE else 0
                }
                
                telegram_notifier.send_market_status(market_data)
                
                # 更新記憶
                _last_market_signal = regime_signal
                _last_market_notification_time = current_time
                
            except Exception as e:
                logger.warning(f"⚠️ Telegram市場狀態通知失敗: {e}")

        result = {
            'signal':       signal,
            'brake':        brake,
            'soft_brake':   soft_brake,
            'brake_reason': brake_reason,
            'regime_signal': regime_signal,
            'market_score': score,
            'mean_z':       mean_z,
            'mean_adx':     mean_adx,
            'is_highvol':   is_highvol,
            'is_bear':      is_bear
        }
        _regime_cache['data'] = result
        _regime_cache['ts']   = time.time()
        return result

    except Exception as e:
        logger.error(f"⚠️ 市場狀態檢測器故障: {e}")
        if _regime_cache['data'] is not None:
            logger.warning("⚠️ 使用上次緩存結果繼續運行")
            return _regime_cache['data']
        return {'signal': 0, 'brake': True, 'soft_brake': False,
                'brake_reason': f'API Error: {e}', 'regime_signal': 0}


# ==========================================
# 📡 市場掃描
# ==========================================
def scouting_strong_coins(scouting_coins=8):
    try:
        tickers = exchange.fetch_tickers()
        data    = []
        for s, t in tickers.items():
            if (s.endswith(':USDT') and
                s in WHITELIST and
                s not in BLACKLIST and
                t.get('percentage') is not None):
                ask, bid = t.get('ask'), t.get('bid')
                if ask and bid and bid > 0:
                    spread = (ask - bid) / bid
                    if spread < 0.0010:
                        data.append({'symbol': s, 'volume': t['quoteVolume'],
                                     'change': t['percentage']})
        df = pd.DataFrame(data)
        if df.empty: return []
        return df.sort_values('change', ascending=False).head(scouting_coins)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Majors Scouting Error: {e}")
        return []


# ==========================================
# 🔍 Lee-Ready 引擎
# ==========================================
def check_flow_health(symbol):
    try:
        trades = exchange.fetch_trades(symbol, limit=100)
        if not trades or len(trades) < 50: return None

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1,
                             np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol        = df['amount'].mean()
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        flow_std = df['net_flow'].std()
        if flow_std == 0: return None

        flow_mean      = df['net_flow'].mean()
        recent_25_flow = df['net_flow'].tail(25).sum()
        z_score        = (recent_25_flow - (flow_mean * 25)) / (flow_std * np.sqrt(25))

        if z_score < -3.0:
            return "Flow Reversal (Long Dump Detected)"

        flow_older_25 = df['net_flow'].iloc[-50:-25].sum()
        acceleration  = recent_25_flow - flow_older_25
        accel_z       = acceleration / (flow_std * np.sqrt(25))

        if accel_z < -2.0 and recent_25_flow < 0:
            try:
                ob        = exchange.fetch_order_book(symbol, limit=20)
                bids_vol  = sum([b[1] for b in ob['bids']])
                asks_vol  = sum([a[1] for a in ob['asks']])
                imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) \
                            if (bids_vol + asks_vol) > 0 else 0
                if imbalance < -0.15:
                    return "Flow Deceleration (Momentum Died)"
            except:
                pass
        return None
    except:
        return None


def apply_lee_ready_long_logic(symbol):
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1,
                             np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol        = df['amount'].mean()
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        short_window_flow = df['net_flow'].tail(50).sum()
        acceleration      = df['net_flow'].tail(25).sum() - df['net_flow'].iloc[-50:-25].sum()

        try:
            ob        = exchange.fetch_order_book(symbol, limit=20)
            bids_vol  = sum([b[1] for b in ob['bids']])
            asks_vol  = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) \
                        if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        z_score   = 0
        is_strong = False
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))

        if (short_window_flow > 0) and (acceleration > 0) and (imbalance > 0.15):
            is_strong = True
            print(f"🔥 {symbol} Long Sniper! Accel:{acceleration:.0f} | Imbalance:{imbalance:.2f}")
        elif z_score > NET_FLOW_SIGMA:
            is_strong = True
            print(f"📈 {symbol} Long Z-Score Validated: {z_score:.2f}")

        if is_strong and imbalance < -0.1:
            is_strong = False
            print(f"⚠️ {symbol} 假突破陷阱！取消做多！")

        return short_window_flow, df['price'].iloc[-1], is_strong
    except Exception as e:
        print(f"⚠️ LR Logic Error [{symbol}]: {e}")
        return 0, 0, False


# ==========================================
# 🛡️ 持倉管理
# ==========================================
def sync_positions_on_startup():
    """[FIX-SIM] Sim 模式跳過交易所同步"""
    if SIMULATION_MODE:
        print("🔵 [SIM] 跳過倉位同步（模擬模式，初始持倉為零）")
        return

    print("🔄 正在同步交易所現有多倉...")
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols       = [p for p in live_positions_raw
                              if float(p.get('contracts', 0) or p.get('size', 0)) > 0]
        recovered_count    = 0
        for p in live_symbols:
            symbol    = p['symbol']
            side      = p.get('side', '').lower()
            info_side = p.get('info', {}).get('side', '').lower()
            if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                entry_price = float(p.get('entryPrice', 0))
                amount      = float(p.get('contracts', 0) or p.get('size', 0))
                sl_p        = float(p.get('stopLoss', 0))
                tp_p        = float(p.get('takeProfit', 0))
                atr, _      = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01
                if sl_p == 0:
                    sl_p = float(exchange.price_to_precision(symbol,
                                 entry_price - (SL_ATR_MULT * atr)))
                if tp_p == 0:
                    tp_p = float(exchange.price_to_precision(symbol,
                                 entry_price + (TP_ATR_MULT * atr)))
                is_be = True if (sl_p > entry_price and sl_p > 0) else False
                positions[symbol] = {
                    'amount': amount, 'entry_price': entry_price,
                    'tp_price': tp_p, 'sl_price': sl_p,
                    'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                    'entry_time': time.time()
                }
                recovered_count += 1
                print(f"✅ 尋回孤兒多單: {symbol} | 入場:{entry_price} | 保本:{is_be}")
        print(f"🔄 同步完成！共尋回 {recovered_count} 個多倉。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


def manage_long_positions(regime=None):
    """
    多單持倉管理（[FIX-SIM] Sim/Live 統一介面）

    管理流程完全相同，差異點：
      - get_live_positions_cached() → Sim 返回本地帳本
      - IOC 平倉指令 → Sim 呼叫 sim_close_long()
      - SL 更新指令  → Sim 只更新本地 positions dict
      - 平倉後清除 _positions_cache（Live only）
    """
    try:
        live_positions_raw = get_live_positions_cached()
        live_symbols = {
            p['symbol']: p for p in live_positions_raw
            if float(p.get('contracts', 0) or p.get('size', 0)) > 0
        }

        # ── 孤兒多單自動接管（Live only，Sim 不會有孤兒）──
        for s, p in live_symbols.items():
            if s not in positions:
                side      = p.get('side', '').lower()
                info_side = p.get('info', {}).get('side', '').lower()
                if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                    entry_p = float(p.get('entryPrice', 0))
                    amt     = float(p.get('contracts', 0) or p.get('size', 0))
                    atr, _  = get_market_metrics(s)
                    if not atr: atr = entry_p * 0.01
                    real_entry_time = float(
                        p.get('createdTime') or (time.time() * 1000)) / 1000.0
                    sl_p = float(p.get('stopLoss') or 0)
                    tp_p = float(p.get('takeProfit') or 0)
                    if sl_p == 0:
                        sl_p = float(exchange.price_to_precision(
                            s, entry_p - (SL_ATR_MULT * atr)))
                    if tp_p == 0:
                        tp_p = float(exchange.price_to_precision(
                            s, entry_p + (TP_ATR_MULT * atr)))
                    is_be = True if (sl_p > entry_p and sl_p > 0) else False
                    positions[s] = {
                        'amount': amt, 'entry_price': entry_p,
                        'tp_price': tp_p, 'sl_price': sl_p,
                        'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                        'entry_time': real_entry_time
                    }
                    print(f"🚨 [自癒] 接管孤兒多單: {s} | 入場:{entry_p}")

        # ── Native Exit 偵測 ──
        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 {'[SIM]' if SIMULATION_MODE else ''} 倉位已平: {s}")
                real_pnl = process_native_exit_log(s, positions[s], 'long')
                cancel_all_v5(s)
                handle_trade_result(s, real_pnl)
                del positions[s]
                if SIMULATION_MODE and s in sim_positions:
                    del sim_positions[s]
                continue

        if not positions:
            return

        current_prices = fetch_tickers_for_positions(list(positions.keys()))

        for s in list(positions.keys()):
            try:
                curr_p = current_prices.get(s)
                if curr_p is None:
                    logger.warning(f"⚠️ {s} 無現價，跳過")
                    continue

                pos     = positions[s]
                pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
                coin_vol_pct = pos['atr'] / pos['entry_price']
                sl_updated   = False

                if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
                pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

                # ── 保本 ──
                if not pos['is_breakeven'] and pnl_pct > (coin_vol_pct * 2.0):
                    pos['sl_price']     = pos['entry_price'] * 1.002
                    pos['is_breakeven'] = True
                    sl_updated          = True

                # ── 移動止損 ──
                if pos['is_breakeven']:
                    if regime and regime.get('brake'):
                        trail_sl = curr_p - (0.3 * pos['atr'])
                    elif regime and regime.get('soft_brake'):
                        trail_sl = curr_p - (0.6 * pos['atr'])
                    elif pos.get('deceleration_detected') and pnl_pct > (coin_vol_pct*2.5):
                        trail_sl = curr_p - (0.5 * pos['atr'])
                    elif pnl_pct > (coin_vol_pct * 5.0):
                        trail_sl = curr_p - (0.8 * pos['atr'])
                    elif pnl_pct > (coin_vol_pct * 3.5):
                        trail_sl = curr_p - (1.2 * pos['atr'])
                    else:
                        trail_sl = curr_p - (1.8 * pos['atr'])

                    if trail_sl > pos['sl_price']:
                        if (trail_sl - pos['sl_price']) / pos['sl_price'] > 0.0005:
                            sl_updated      = True
                            pos['sl_price'] = trail_sl

                # ── 推送止損到交易所（Live only）──
                if sl_updated and not SIMULATION_MODE:
                    f_sl = exchange.price_to_precision(s, pos['sl_price'])
                    try:
                        exchange.private_post_v5_position_trading_stop({
                            'category': 'linear', 'symbol': exchange.market_id(s),
                            'stopLoss': str(f_sl), 'tpslMode': 'Full', 'positionIdx': 0
                        })
                    except Exception as e:
                        logger.warning(f"⚠️ {s} Trail SL 更新失敗: {e}")

                # ── 離場判斷 ──
                exit_reason = None
                time_held   = time.time() - pos.get('entry_time', time.time())

                if time_held > TIMEOUT_SECONDS and pnl_pct < 0.005:
                    exit_reason = "Momentum Timeout (Stalled Zombie)"

                curr_t     = time.time()
                last_check = pos.get('last_flow_check', 0)
                if not exit_reason and (curr_t - last_check > 15):
                    pos['last_flow_check'] = curr_t
                    if time_held > 120:
                        flow_status = check_flow_health(s)
                        if flow_status == "Flow Reversal (Long Dump Detected)":
                            exit_reason = flow_status
                        elif flow_status == "Flow Deceleration (Momentum Died)":
                            if not pos.get('deceleration_detected'):
                                pos['deceleration_detected'] = True
                                print(f"⚠️ {s} 高位收油偵測！啟動防禦標記！")

                if not exit_reason:
                    if curr_p >= pos['tp_price']:
                        exit_reason = "TP (Long IOC Exit)"
                    elif curr_p <= pos['sl_price']:
                        exit_reason = ("Trail SL (Long IOC Exit)"
                                       if pos['is_breakeven'] else "SL (Long IOC Exit)")

                # ── 執行離場 ──
                if exit_reason:
                    print(f"⚔️ {exit_reason} | {s} | {time_held/60:.1f}分 | "
                          f"MaxPnL:{pos['max_pnl_pct']*100:.2f}% | 現:{pnl_pct*100:.2f}%"
                          + (" [SIM]" if SIMULATION_MODE else ""))

                    if SIMULATION_MODE:
                        # ── [FIX-SIM] Sim 模擬平倉 ──
                        ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                        ioc_pnl   = sim_close_long(s, pos['amount'], ioc_price)
                        if s in sim_positions:
                            del sim_positions[s]
                    else:
                        # ── Live 實際平倉 ──
                        ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                        try:
                            exchange.create_order(s, 'limit', 'sell', pos['amount'],
                                                  ioc_price,
                                                  {'timeInForce': 'IOC', 'reduceOnly': True})
                        except:
                            exchange.create_market_sell_order(
                                s, pos['amount'], {'reduceOnly': True})
                        ioc_pnl = round((ioc_price - pos['entry_price']) * pos['amount'], 4)
                        _positions_cache['ts'] = 0   # [BUG FIX 4] 清除快取

                    log_to_csv({
                        'symbol': s, 'action': 'LONG_EXIT', 'price': curr_p,
                        'amount': pos['amount'], 'reason': exit_reason,
                        'realized_pnl': ioc_pnl
                    })
                    
                    # 發送Telegram通知
                    if TELEGRAM_ENABLED:
                        try:
                            telegram_notifier.send_trade_alert(
                                symbol=s,
                                action='LONG_EXIT',
                                price=curr_p,
                                amount=pos['amount'],
                                reason=exit_reason,
                                pnl=ioc_pnl
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ Telegram通知發送失敗: {e}")
                    
                    cancel_all_v5(s)
                    handle_trade_result(s, ioc_pnl)
                    del positions[s]

            except Exception as e:
                if "10006" in str(e):
                    logger.warning("⚠️ Rate limit in position loop, sleeping 10s")
                    time.sleep(10)

    except Exception as e:
        logger.error(f"❌ manage_long_positions 外層錯誤: {e}")


def execute_live_long(symbol, net_flow, current_price, is_strong,
                      atr, is_volatile, regime=None, position_multiplier=1.0):
    """
    多單入場執行（[FIX-SIM] Sim/Live 統一介面）

    Sim 模式差異：
      - 槓桿設定 → 跳過
      - create_order  → sim_open_long()
      - fetch_order   → 直接使用模擬成交資料
      - trading_stop  → 只更新本地 positions dict
    
    Args:
        position_multiplier: 倉位調整乘數 (1.0=正常, 0.7=70%倉位等)
    """
    _r                = regime or {}
    regime_signal_tag = _r.get('regime_signal', 0)
    adx_tag           = round(_r.get('mean_adx', 0), 2)
    score_tag         = round(_r.get('market_score', 0), 4)

    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]: return
        else: del cooldown_tracker[symbol]

    if atr is None or atr == 0 or current_price == 0: return
    if not (is_strong and is_volatile and symbol not in positions): return

    # ── 硬性倉位上限（最後防線）──
    if len(positions) >= MAX_CONCURRENT_POSITIONS:
        logger.debug(f"⛔ {symbol} 倉位上限 {MAX_CONCURRENT_POSITIONS}，拒絕入場")
        return

    # ── Cascade SL 保護（最後防線）──
    now = time.time()
    recent_sl_times[:] = [t for t in recent_sl_times 
                        if now - t < CASCADE_SL_WINDOW]
    if len(recent_sl_times) >= CASCADE_SL_TRIGGER:
        logger.debug(f"⛔ {symbol} Cascade SL 保護中，拒絕入場")
        return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()   # [FIX-SIM] Sim → sim_balance
    eff_bal    = min(WORKING_CAPITAL, actual_bal)

    trade_val = min(
        (eff_bal * RISK_PER_TRADE * position_multiplier) / ((SL_ATR_MULT * atr) / current_price),
        eff_bal * MAX_LEVERAGE * 0.95 * position_multiplier,
        MAX_NOTIONAL_PER_TRADE * position_multiplier
    )

    # Sim 模式：精度用交易所規則，但不真正下單
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))
    if amount < exchange.markets[symbol]['limits']['amount']['min']: return

    ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    # ── 槓桿設定 ──
    if not SIMULATION_MODE:
        try:
            exchange.set_leverage(int(MAX_LEVERAGE), symbol)
        except Exception as e:
            if "110043" not in str(e):
                if "110026" in str(e): return
                logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    # ── 下單 ──
    if SIMULATION_MODE:
        # [FIX-SIM] 模擬成交
        actual_amount, actual_price = sim_open_long(symbol, amount, ioc_p)
        if actual_amount == 0:
            print(f"⏩ [SIM] {symbol} 餘額不足，跳過。")
            return
    else:
        # Live 實際下單
        try:
            order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p,
                                          {'timeInForce': 'IOC', 'positionIdx': 0})
            time.sleep(1)
            actual_price, actual_amount = ioc_p, 0
            try:
                od = exchange.fetch_order(order['id'], symbol,
                                          params={"acknowledged": True})
                actual_price  = float(od.get('average') or od.get('price') or ioc_p)
                actual_amount = float(od.get('filled', 0))
            except Exception as e:
                logger.warning(f"⚠️ {symbol} 訂單確認失敗，備用同步: {e}")
                time.sleep(0.5)
                for p in exchange.fetch_positions():
                    if (p['symbol'] == symbol and
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0):
                        actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                        actual_price  = float(p.get('entryPrice') or ioc_p)
                        break
            if actual_amount == 0:
                print(f"⏩ {symbol} IOC 未成交，撤單退出。")
                cancel_all_v5(symbol)
                return
        except Exception as e:
            logger.error(f"❌ {symbol} 做多執行失敗: {e}")
            return

    # ── 計算 TP/SL ──
    tp_p = float(exchange.price_to_precision(
        symbol, actual_price + (TP_ATR_MULT * atr)))
    sl_p = float(exchange.price_to_precision(
        symbol, actual_price - (SL_ATR_MULT * atr)))

    if (tp_p - actual_price) / actual_price < 0.003:
        print(f"🟡 放棄做多 [{symbol}]: 利潤空間太細！"
              + (" [SIM 無需平倉]" if SIMULATION_MODE else ""))
        if SIMULATION_MODE:
            global sim_balance
            refund = actual_amount * actual_price * (1 - 0.00055)
            sim_balance += refund
            logger.info(f"🔵 [SIM] {symbol} 退還資金 {refund:.4f}（利潤空間太細）")
        else:
            try:
                exchange.create_market_sell_order(
                    symbol, actual_amount, {'reduceOnly': True})
            except Exception as e:
                logger.error(f"❌ 緊急平倉失敗: {e}")
            cancel_all_v5(symbol)
        return

    # ── 設定 TP/SL（Live 推送到交易所，Sim 只記本地）──
    if not SIMULATION_MODE:
        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p), 'takeProfit': str(tp_p),
                'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} TP/SL 設置 | TP:{tp_p} | SL:{sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} TP/SL 設置異常: {e}")
        _positions_cache['ts'] = 0   # [BUG FIX 4] 清除快取
    else:
        print(f"🔵 [SIM] {symbol} 虛擬 TP:{tp_p} | SL:{sl_p}")
        sim_positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price,
            'tp_price': tp_p, 'sl_price': sl_p, 'entry_time': time.time()
        }

    # ── 更新本地持倉字典 ──
    positions[symbol] = {
        'amount': actual_amount, 'entry_price': actual_price,
        'tp_price': tp_p, 'sl_price': sl_p,
        'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0,
        'entry_time': time.time()
    }
    cooldown_tracker[symbol] = time.time() + 480
    save_dynamic_blacklist()

    log_to_csv({
        'symbol': symbol, 'action': 'LONG_ENTRY', 'price': actual_price,
        'amount': actual_amount,
        'trade_value': round(actual_amount * actual_price, 2),
        'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
        'tp_price': tp_p, 'sl_price': sl_p,
        'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal,
        'regime_signal': regime_signal_tag,   # ← 新增
        'mean_adx':      adx_tag,             # ← 新增
        'market_score':  score_tag            # ← 新增
    })
    print(f"📈 {'[SIM] ' if SIMULATION_MODE else ''}[入貨做多] {symbol} "
          f"@ {actual_price:.4f} | 數量:{actual_amount}")
    
    # 發送Telegram通知
    if TELEGRAM_ENABLED:
        try:
            telegram_notifier.send_trade_alert(
                symbol=symbol,
                action='LONG_ENTRY',
                price=actual_price,
                amount=actual_amount,
                reason=f"趨勢多頭 | ADX:{adx_tag} | Score:{score_tag}"
            )
        except Exception as e:
            logger.warning(f"⚠️ Telegram通知發送失敗: {e}")


# ==========================================
# 🚀 主程序
# ==========================================
def main():
    mode_label = "🔵 SIMULATION" if SIMULATION_MODE else "🟢 LIVE TRADE"
    print(f"🚀 AI 實戰 V6.7 BugFixed [{mode_label}] 啟動...")
    print(f"📋 SL={SL_ATR_MULT}×ATR | TP={TP_ATR_MULT}×ATR | "
          f"Regime緩存={REGIME_CACHE_TTL}s | ATR緩存={ATR_CACHE_TTL}s | "
          f"Positions緩存={POSITIONS_CACHE_TTL}s")

    load_dynamic_blacklist()
    sync_positions_on_startup()

    last_scout_time   = 0
    target_coins      = []
    _last_brake_state = None
    _sim_report_ts    = time.time()   # Sim 模式定時報告計時器

    while True:
        try:
            regime = get_btc_regime_v3_fast()
            manage_long_positions(regime)

            curr_t = time.time()

            # ── [FIX-SIM] Sim 模式每 5 分鐘列印績效 ──
            if SIMULATION_MODE and (curr_t - _sim_report_ts > 300):
                sim_report()
                _sim_report_ts = curr_t

            if curr_t - last_scout_time > SCOUTING_INTERVAL:

                target_coins = scouting_strong_coins(20)  # 先 scout
                last_scout_time = curr_t

                _current_state = ('HARD' if regime.get('brake') else
                                  'SOFT' if regime.get('soft_brake') else 'GREEN')
                regime_signal = regime.get('regime_signal', 0)
                is_long_signal = regime_signal in ACTIVE_LONG_SIGNALS
                is_brake = regime_signal in ACTIVE_SHORT_SIGNALS

                _SIGNAL_LABEL = {
                    0: "中性", 1: "MR多頭[未啟用]", 2: "趨勢多頭✅",
                    -1: "MR空頭[未啟用]", -2: "趨勢空頭✅", -3: "熊市空頭✅"
                }
                if _current_state != _last_brake_state:
                    print(f"📡 Regime: {_SIGNAL_LABEL.get(regime_signal, '未知')} | "
                          f"啟用多頭:{ACTIVE_LONG_SIGNALS} 空頭:{ACTIVE_SHORT_SIGNALS}")

                if is_long_signal:
                    mean_adx = regime.get('mean_adx', 0)
                    curr_score = regime.get('market_score', 0)
                    
                    # ADX衰減檢測
                    global _last_scout_adx, _last_scout_score
                    adx_decay = _last_scout_adx - mean_adx if _last_scout_adx > 0 else 0
                    
                    # Score衰減檢測
                    score_decay = _last_scout_score - curr_score if _last_scout_score > 0 else 0
                    
                    # 分級調整邏輯
                    if mean_adx < 20:
                        print(f"⚠️ ADX={mean_adx:.1f} < 20，弱趨勢不交易")
                    elif score_decay > 0.05 and _last_scout_score > 0:
                        # Score衰減超過0.05：跳過本輪入場
                        print(f"⚠️ Score衰減 {score_decay:.3f}（{_last_scout_score:.3f}→{curr_score:.3f}），跳過本輪入場")
                        _last_scout_adx = mean_adx
                        _last_scout_score = curr_score
                        continue  # 跳過後續掃描
                    elif adx_decay > 2.0:
                        # ADX衰減超過2點：強制減倉至50%
                        position_multiplier = 0.5
                        print(f"⚠️ ADX衰減 {adx_decay:.1f}點（{_last_scout_adx:.1f}→{mean_adx:.1f}），倉位降至50%")
                    elif mean_adx < 25:
                        # 中等趨勢：降低倉位至70%
                        position_multiplier = 0.7
                        print(f"🟡 趨勢多頭+2（ADX={mean_adx:.1f}）中等趨勢，倉位調整至{position_multiplier*100:.0f}%"
                              f"{'[SIM]' if SIMULATION_MODE else ''}掃描...")
                    else:
                        # 強趨勢：正常倉位
                        position_multiplier = 1.0
                        print(f"🟢 趨勢多頭+2（ADX={mean_adx:.1f}）強趨勢，正常倉位"
                              f"{'[SIM]' if SIMULATION_MODE else ''}掃描...")
                    
                    _last_scout_adx = mean_adx    # 更新ADX記憶
                    _last_scout_score = curr_score # 更新Score記憶
                    
                    # 只有在非弱趨勢情況下才進行後續檢查
                    if mean_adx >= 20:
                        # 倉位上限檢查
                        if len(positions) >= MAX_CONCURRENT_POSITIONS:
                            print(f"⛔ 倉位已達上限 {MAX_CONCURRENT_POSITIONS}，跳過本輪掃描")
                            continue
                        
                        # Cascade Pause 檢查
                        now = time.time()
                        recent_sl_times[:] = [t for t in recent_sl_times 
                                            if now - t < CASCADE_SL_WINDOW]
                        if len(recent_sl_times) >= CASCADE_SL_TRIGGER:
                            print(f"⛔ SL 連環觸發保護：{len(recent_sl_times)} 筆 SL 在 {CASCADE_SL_WINDOW}s 內，暫停入場")
                            continue
                        
                        for s in target_coins:
                            try:
                                flow, last_p, is_strong = apply_lee_ready_long_logic(s)
                                atr, is_v = get_market_metrics(s)
                                if last_p > 0:
                                    execute_live_long(s, flow, last_p, is_strong,
                                                      atr, is_v, regime=regime,
                                                      position_multiplier=position_multiplier)
                            except Exception:
                                continue
                            time.sleep(0.3)
                elif is_brake:
                    if _current_state != _last_brake_state:
                        print(f"🚫 {_SIGNAL_LABEL.get(regime_signal, '空頭')}：暫停多單")
                else:
                    if _current_state != _last_brake_state:
                        if regime_signal == 1:
                            print("🟡 MR多頭+1 觸發，測試期未啟用，等待+2")
                        else:
                            print(f"🚦 {regime.get('brake_reason', '市場中性')}，暫停")

                _last_brake_state = _current_state
                bal_str = (f"SimBal:{sim_balance:.2f} PnL:{sim_total_pnl:+.4f}"
                           if SIMULATION_MODE else f"餘額:{get_live_usdt_balance():.2f}")
                print(f"⏳ {'[SIM]' if SIMULATION_MODE else ''} 多軍巡邏 | "
                      f"持倉:{list(positions.keys())} | {bal_str}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 手動終止。")
            if SIMULATION_MODE:
                sim_report()
            else:
                print(f"餘額:{get_live_usdt_balance():.2f} | 持倉:{list(positions.keys())}")
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ 主迴圈錯誤: {e}")
            time.sleep(30 if "10006" in str(e) else 10)


if __name__ == "__main__":
    main()