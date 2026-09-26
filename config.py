"""All tunables in one place. Risk limits here are HARD caps: learning never changes them."""
import os

# Load secrets from .env (gitignored) into the environment. Never hardcode or log the secret.
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    with open(_env) as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SYMBOL = "BTC/USDT"          # kline format; order API uses "BTCUSDT"
INTERVAL = "15t"             # Mudrex aggregation code for 15 minutes
INTERVAL_SEC = 900
HISTORY_DAYS = 365
IST_OFFSET = 19800           # daily loss cap resets at IST midnight

# Money (backtest equity is in USDT-equivalent; live INR wallet converts via hedge_rate)
START_EQUITY = 1000.0
RISK_PCT = 0.01              # risk per trade, fraction of current equity
LEVERAGE = 3                 # max notional = equity * LEVERAGE
DAILY_LOSS_CAP = 0.03        # no new entries after losing 3% in an IST day
DD_HALVE = 0.10              # halve risk when 10% below peak equity
DD_HALT = 0.20               # stop trading when 20% below peak equity
QTY_STEP = 0.001             # TODO verify via GET /futures/{asset_id} once API key exists
MIN_QTY = 0.001
MIN_NOTIONAL = 5.0           # Mudrex min_notional_value, USDT
INR_PER_USDT = 102.0         # Mudrex hedge_rate seen on the user's INR positions (Sep 2026)

# Costs
TAKER_FEE = 0.0005           # 0.05% (Mudrex 0.03-0.05%), worst case
GST = 0.18                   # on fees, INR and USDT futures
SLIPPAGE = 0.0002            # per fill
FUNDING_PER_DAY = 0.0003     # 0.01% per 8h, charged on notional for time held, always paid (worst case)

# Strategy
WARMUP = 600                 # bars before first signal (3x trend EMA so it converges)
DEFAULT_PARAMS = dict(
    entry="ema",             # "ema" crossover | "breakout" (Donchian) | "rsi" / "bb" (mean reversion)
    fast=9, slow=21,         # ema entry
    don_n=20,                # breakout entry: close breaks previous don_n-bar high/low
    rsi_len=14, rsi_lo=30, rsi_hi=70,  # rsi entry: buy dip below lo, sell rip above hi; exit at RSI 50
    bb_n=20, bb_k=2.0,       # bb entry: fade close outside mid +/- k*std; exit back at mid
    trend=200,               # trend filter EMA: long only above, short only below; 0 = off
    atr_len=14, sl_atr=1.5,  # initial stop = sl_atr * ATR from entry
    rr=2.0,                  # take-profit at rr * stop distance; 0 = no TP
    trail_atr=0,             # trailing stop sl at trail_atr * ATR behind bar extreme; 0 = off
    adx_min=0, adx_len=14,   # skip entries when ADX < adx_min; 0 = off
    adx_max=0,               # skip entries when ADX > adx_max (mean reversion wants chop); 0 = off
    vol_mult=0, vol_len=20,  # skip entries when volume < vol_mult * avg volume; 0 = off
)

# Walk-forward learning
GRID = dict(fast=[5, 9, 13, 20], slow=[21, 34, 55], sl_atr=[1.0, 1.5, 2.0, 2.5], rr=[1.5, 2.0, 3.0])
TRAIN_DAYS = 90
TEST_DAYS = 14
MIN_TRADES = 20              # fewer trades on train window = no confidence, not eligible
SWITCH_MARGIN = 1.10         # new params must score 10% better than current to replace them
