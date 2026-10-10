#!/usr/bin/env python3
"""
Bybit Combo Signal Bot - XAUUSD & BTCUSD, live -> Telegram + chart
Data: Bybit v5 REST kline 1m (default api.bybit.id). Pair Bybit XAUUSDT / BTCUSDT ditampilkan sebagai XAUUSD / BTCUSD.
Harga Bybit digeser dengan OFFSET supaya sama dengan harga CFD (mis. CFD 4000, Bybit 4004 -> offset -4).

Dua sinyal (logika sama dengan versi saham):
  [A] MOMENTUM LONG   : bias 5m (EMA20>EMA50, VWAP, ADX, slope) + trigger 1m breakout + RVOL. Entry = open bar berikutnya.
  [B] SHADOW FAKEOUT  : sweep di bawah low N bar + lower shadow + reclaim. Buy stop di atas high bar sinyal.
  [SF] SHADOW FAKEOUT 1M (port setia xau-btc / bot1.py): range 24 bar -> sweep shadow panjang -> breakbody/BOS -> retest.
       Pesan detail (Entry/Target/SL/Dasar/Confluence/Session/Demand-Supply/Pattern/Alasan/Konfirmasi) + chart putih:
       zona supply/demand, trendline pivot, EMA21, kotak Entry/SL/TP, volume profile + POC, histogram volume, trend-band.
Plus: notifikasi TP HIT / SL HIT / keluar max bar, dan perintah Telegram (/status, /offset).

Env wajib:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (API publik Bybit tidak butuh key)
Env opsional (default):
    OFFSET_XAUUSD=-4  OFFSET_BTCUSD=0   offset harga ke CFD (bisa diubah live lewat /offset)
    TELEGRAM_TOPIC_MOMENTUM=<topic id A>
    TELEGRAM_TOPIC_LEGACY_FAKEOUT=<topic id B>
    TELEGRAM_TOPIC_ACCUM_EXP=<topic id C>
    TELEGRAM_TOPIC_SHADOW_FAKEOUT_1M=<topic id SF>
    TELEGRAM_TOPIC_EVENTS=<topic id event/TP-SL>
    Topic kosong = fallback ke chat utama. Routing sinyal otomatis berdasarkan engine.
    BYBIT_BASE=https://api.bybit.id     domain API (global: https://api.bybit.com)
    DAY_RESET_HOUR=7    jam WIB reset VWAP/EMA 5m harian (07 = 00:00 UTC)
    XAU_PAUSE_WEEKEND=1 tidak kirim sinyal XAUUSD Sabtu 05:00 - Senin 05:00 WIB (CFD tutup)
    A_MAX_HOLD_BARS=180 batas bar sinyal A di tracker (0 = tanpa batas)
    A_ENABLED=1  B_ENABLED=1  AE_ENABLED=1  SF_ENABLED=1     (0 = matikan sinyal A / B / C / SF)
    SF_RANGE_LOOKBACK=24  SF_SETUP_LOOKBACK=10  SF_SHADOW_RATIO=1.2  SF_MIN_SHADOW_RANGE=0.35  SF_MIN_RR=1.5
    SF_SL_PIPS_XAU=50  SF_SL_PIPS_BTC=50  SF_PIP_XAU=0.01  SF_PIP_BTC=1.0     (SL = invalidasi + pips x pip size)
    SF_FALLBACK=1       sinyal cadangan sweep+shadow+reclaim tanpa BOS/retest (0 = hanya setup PDF strict seperti xau-btc)
    SF_COOLDOWN_MIN=30  jeda antar sinyal SF per simbol (0 = nonaktif; xau-btc: SIGNAL_COOLDOWN_MINUTES=30)
    SF_CHART_BARS=120  SF_CHART_POC=1  SF_CHART_SETUP=1   (candle di chart, sorot POC, penanda SWEEP/BOS/RETEST)

Pakai:
    python bybit_combo_bot.py --check            # tes koneksi, pair, tick, harga + offset
    python bybit_combo_bot.py --demo [--send]    # contoh chart + pesan (data sintetis)
    Topic Telegram: TELEGRAM_TOPIC_MOMENTUM / LEGACY_FAKEOUT / ACCUM_EXP / SHADOW_FAKEOUT_1M / EVENTS
    python bybit_combo_bot.py --once             # scan sekali
    python bybit_combo_bot.py --loop             # live
Perintah Telegram (saat --loop): /status  /offset  /offset XAUUSD -4  /help /whereami
"""
import os, sys, json, math, time, argparse, threading, html, struct, zlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# ============================================================ CONFIG
BASE = os.getenv("BYBIT_BASE", "https://api.bybit.id").rstrip("/")
CATEGORY = os.getenv("BYBIT_CATEGORY", "linear")
PAIRS = {"XAUUSD": os.getenv("BYBIT_XAU", "XAUUSDT").strip().upper(),     # nama tampil -> simbol Bybit
         "BTCUSD": os.getenv("BYBIT_BTC", "BTCUSDT").strip().upper()}
DEFAULT_TICK = {"XAUUSD": 0.01, "BTCUSD": 0.10}                            # dipakai bila instruments-info gagal
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
# Topic/thread routing otomatis. Isi ID topic Telegram masing-masing.
# Jika suatu topic kosong, sinyalnya fallback ke TELEGRAM_CHAT_ID utama.
TOPIC_IDS = {
    "A": os.getenv("TELEGRAM_TOPIC_MOMENTUM", "").strip(),
    "B": os.getenv("TELEGRAM_TOPIC_LEGACY_FAKEOUT", "").strip(),
    "C": os.getenv("TELEGRAM_TOPIC_ACCUM_EXP", "").strip(),
    "SF": os.getenv("TELEGRAM_TOPIC_SHADOW_FAKEOUT_1M", "").strip(),
    "EVENT": os.getenv("TELEGRAM_TOPIC_EVENTS", "").strip(),
}
TZ = "Asia/Jakarta"

# Persistent data directory. Railway: set DATA_DIR=/data and mount a volume there.
DATA_DIR = os.getenv("DATA_DIR", ".").strip() or "."
os.makedirs(DATA_DIR, exist_ok=True)

def _f(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return float(default)

# Telegram command security: only these user IDs may issue commands.
def _parse_ids(raw):
    out = set()
    for x in (raw or "").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.add(int(x))
        except ValueError:
            pass
    return out

ADMIN_IDS = _parse_ids(os.getenv("TELEGRAM_ADMIN_IDS", ""))

# Paper capital tracker (bookkeeping only; this bot does NOT place live orders).
CAPITAL_START = max(0.0, _f("CAPITAL_START_IDR", 1_000_000.0))
CAPITAL_TOPUP = max(0.0, _f("CAPITAL_TOPUP_IDR", 500_000.0))
TRADE_LOT = max(0.0001, _f("TRADE_LOT", 0.01))
USD_IDR = max(1.0, _f("USD_IDR", 16_000.0))  # bookkeeping assumption; change to desired rate
XAU_UNITS_PER_LOT = max(0.0001, _f("XAU_UNITS_PER_LOT", 100.0))
BTC_UNITS_PER_LOT = max(0.0001, _f("BTC_UNITS_PER_LOT", 1.0))
WEEKLY_RECAP_HOUR = int(_f("WEEKLY_RECAP_HOUR", 5.0))
WEEKLY_RECAP_MINUTE = int(_f("WEEKLY_RECAP_MINUTE", 0.0))
CAPITAL_FILE = os.path.join(DATA_DIR, "bybit_capital.json")

def is_admin(user_id):
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False

OFFSETS = {"XAUUSD": _f("OFFSET_XAUUSD", -5.0), "BTCUSD": _f("OFFSET_BTCUSD", 0.0)}
DAY_RESET_H = int(_f("DAY_RESET_HOUR", 7))
XAU_PAUSE = os.getenv("XAU_PAUSE_WEEKEND", "1") != "0"

SENT_FILE = os.path.join(DATA_DIR, "bybit_sent.json")
BOOK_FILE = os.path.join(DATA_DIR, "bybit_book.json")
OFFSET_FILE = os.path.join(DATA_DIR, "bybit_offsets.json")
CHART_DIR = os.path.join(DATA_DIR, "bybit_charts")
HISTORY_BARS = 600                            # ~10 jam bar 1m; cukup untuk engine dan mengurangi request startup
SIGNAL_MAX_AGE_MIN = 4                        # sinyal lebih tua dari ini dibuang (data telat)

# --- Sinyal A
EMA_FAST_1M, EMA_SLOW_1M = 9, 21
EMA_FAST_5M, EMA_SLOW_5M = 20, 50
ADX_PERIOD = ATR_PERIOD = 14
ADX_MIN, RVOL_MIN, VWAP_MAX_DISTANCE = 18.0, 1.50, 0.010
ATR_SL_MULT, A_RR = 1.20, 1.50
MIN_SCORE = 70
A_MAX_PER_DAY = int(_f("A_MAX_PER_DAY", 3))

# --- Sinyal C: ACCUMULATION → EXPANSION (port struktur Radar, diskalakan ke intraday Bybit)
A_ENABLED = os.getenv("A_ENABLED", "1") != "0"                # sinyal A (Momentum Long)
B_ENABLED = os.getenv("B_ENABLED", "1") != "0"                # sinyal B (Legacy Shadow Fakeout)
AE_ENABLED = os.getenv("AE_ENABLED", "1") != "0"
AE_TF_MINUTES = max(1, int(_f("AE_TF_MINUTES", 5)))
AE_BASE_BARS = max(30, int(_f("AE_BASE_BARS", 60)))
AE_BREAK_BARS = max(10, int(_f("AE_BREAK_BARS", 20)))
AE_MIN_SCORE = max(40, int(_f("AE_MIN_SCORE", 60)))
AE_WATCH_SCORE = max(AE_MIN_SCORE, int(_f("AE_WATCH_SCORE", 80)))
AE_LAST_BARS = max(1, int(_f("AE_LAST_BARS", 6)))
AE_MAX_PER_DAY = int(_f("AE_MAX_PER_DAY", 6))
AE_TP_ATR = max(1.0, _f("AE_TP_ATR", 2.20))
AE_SL_ATR = max(0.20, _f("AE_SL_ATR", 0.80))
AE_MIN_RVOL = max(1.0, _f("AE_MIN_RVOL", 1.20))

# --- Sinyal B
LOOKBACK = 20
SHADOW_MIN, RECLAIM_MIN, B_RR = 0.40, 0.25, 2.0
BUFFER_PCT = 0.0005
MAX_HOLD_BARS = 30                            # B: maks bar setelah entry (tracker)
B_EXPIRE_BARS = 30                            # B: buy stop kedaluwarsa bila belum terpicu
A_MAX_HOLD_BARS = int(_f("A_MAX_HOLD_BARS", 180))   # A: pasar 24 jam tidak punya "akhir hari"

# --- Manajemen posisi: BE / SL+ otomatis (paper tracker; tidak menempatkan order live)
BE_ENABLED = os.getenv("BE_ENABLED", "1") != "0"
BE_TRIGGER_R = max(0.0, _f("BE_TRIGGER_R", 0.80))       # saat profit mencapai +0.80R
BE_OFFSET_R = max(0.0, _f("BE_OFFSET_R", 0.05))         # kunci +0.05R (BE+)
SLPLUS_TRIGGER_R = max(BE_TRIGGER_R, _f("SLPLUS_TRIGGER_R", 1.20))  # naikkan SL lagi
SLPLUS_LOCK_R = max(0.0, _f("SLPLUS_LOCK_R", 0.30))    # kunci +0.30R

# --- Sinyal SF: Shadow Fakeout 1M dari xaubtc/bot1.py
SF_ENABLED = os.getenv("SF_ENABLED", "1") != "0"
SF_RANGE_LOOKBACK = max(10, int(_f("SF_RANGE_LOOKBACK", 24)))
SF_SETUP_LOOKBACK = max(4, int(_f("SF_SETUP_LOOKBACK", 10)))
SF_SHADOW_RATIO = max(1.0, _f("SF_SHADOW_RATIO", 1.20))
SF_MIN_SHADOW_RANGE = min(0.95, max(0.15, _f("SF_MIN_SHADOW_RANGE", 0.35)))
SF_MIN_RR = max(1.0, _f("SF_MIN_RR", 1.50))
SF_LAST_BARS = max(1, int(_f("SF_LAST_BARS", 6)))
SF_SL_PIPS_XAU = max(1.0, _f("SF_SL_PIPS_XAU", _f("MATERIAL_SL_PIPS", 50.0)))
SF_SL_PIPS_BTC = max(1.0, _f("SF_SL_PIPS_BTC", _f("MATERIAL_SL_PIPS", 50.0)))
SF_PIP_XAU = max(1e-9, _f("SF_PIP_XAU", 0.01))
SF_PIP_BTC = max(1e-9, _f("SF_PIP_BTC", 1.0))
SF_TOLERANCE_ATR = max(0.02, _f("SF_TOLERANCE_ATR", 0.08))
SF_FALLBACK = os.getenv("SF_FALLBACK", "1") != "0"
SF_FALLBACK_RECENT = max(5, int(_f("SF_FALLBACK_RECENT", 7)))
SF_MAX_HOLD_BARS = max(10, int(_f("SF_MAX_HOLD_BARS", 120)))
SF_COOLDOWN_MIN = max(0, int(_f("SF_COOLDOWN_MIN", 30)))      # jeda antar sinyal SF per simbol (0 = nonaktif)
SF_CHART_BARS = max(40, int(_f("SF_CHART_BARS", 120)))         # jumlah candle di chart SF (xau-btc: 120)
SF_CHART_POC = os.getenv("SF_CHART_POC", "1") != "0"          # sorot POC volume profile (bar + garis + label)
SF_CHART_SETUP = os.getenv("SF_CHART_SETUP", "1") != "0"      # gambar range + penanda SWEEP / BOS / RETEST


# ============================================================ DATA (Bybit v5)
def now_ts():
    return pd.Timestamp.now(tz=TZ)


def tday(idx):
    """Tanggal 'hari trading' (reset di DAY_RESET_H WIB) untuk pasar 24 jam."""
    return (pd.DatetimeIndex(idx) - pd.Timedelta(hours=DAY_RESET_H)).date


SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})
BYBIT_MIN_REQUEST_GAP = max(0.50, _f("BYBIT_MIN_REQUEST_GAP", 1.0))
BYBIT_RATE_BACKOFF = max(5.0, _f("BYBIT_RATE_BACKOFF", 12.0))
_BYBIT_LAST_REQUEST = 0.0
_BYBIT_LOCK = threading.Lock()


def bybit_get(path, params, retries=3):
    """REST Bybit yang rate-limit aware. Jangan retry 10006 secara cepat."""
    global _BYBIT_LAST_REQUEST
    last = None
    for a in range(retries):
        try:
            with _BYBIT_LOCK:
                wait = BYBIT_MIN_REQUEST_GAP - (time.monotonic() - _BYBIT_LAST_REQUEST)
                if wait > 0:
                    time.sleep(wait)
                _BYBIT_LAST_REQUEST = time.monotonic()
                r = SESSION.get(BASE + path, params=params, timeout=20)

            if r.status_code == 200:
                j = r.json()
                if j.get("retCode") == 0:
                    return j
                code = j.get("retCode")
                msg = j.get("retMsg")
                if code == 10006:
                    # Rate limit: tunggu jauh lebih lama, bukan retry cepat.
                    last = RuntimeError(f"Bybit retCode 10006: {msg}")
                    time.sleep(BYBIT_RATE_BACKOFF * (a + 1))
                    continue
                raise RuntimeError(f"Bybit retCode {code}: {msg}")
            last = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(3.0 * (a + 1))
        except requests.RequestException as e:
            last = e
            time.sleep(3.0 * (a + 1))
    raise last


EMPTY = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def parse_klines(rows, server_ms):
    """Bybit: [startMs, o, h, l, c, volume, turnover], urutan terbaru dulu."""
    if not rows:
        return EMPTY.copy(), None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    oldest = int(df["ts"].min())
    df = df[df["ts"] + 60_000 <= server_ms].dropna()               # buang bar yang masih berjalan
    if df.empty:
        return EMPTY.copy(), oldest
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(TZ)
    df = df.drop_duplicates("dt").sort_values("dt").set_index("dt")
    return df[["open", "high", "low", "close", "volume"]], oldest


def fetch_klines(sym, limit=200, end_ms=None):
    p = dict(category=CATEGORY, symbol=PAIRS[sym], interval="1", limit=limit)
    if end_ms:
        p["end"] = end_ms
    j = bybit_get("/v5/market/kline", p)
    return parse_klines(j["result"]["list"], int(j.get("time") or time.time() * 1000))


def fetch_history(sym, n=HISTORY_BARS):
    frames, end, got = [], None, 0
    for _ in range(10):
        df, oldest = fetch_klines(sym, 1000, end)
        if oldest is None:
            break
        frames.append(df); got += len(df)
        if got >= n:
            break
        end = oldest - 1
    if not frames:
        return EMPTY.copy()
    out = pd.concat(frames)
    return out[~out.index.duplicated()].sort_index().iloc[-n:]


RAW = {}       # sym -> bar 1m mentah dari Bybit (tanpa offset)


def get_raw(sym):
    """Riwayat di-cache; tiap scan hanya ambil 200 bar terakhir lalu digabung."""
    c = RAW.get(sym)
    if c is None or c.empty or (now_ts() - c.index[-1]) > pd.Timedelta(minutes=150):
        df = fetch_history(sym)
    else:
        new, _ = fetch_klines(sym, 200)
        df = pd.concat([c, new])
        df = df[~df.index.duplicated(keep="last")].sort_index().iloc[-(HISTORY_BARS + 200):]
    RAW[sym] = df
    return df


def adj(df, off):
    """Geser harga Bybit ke harga CFD: harga_cfd = harga_bybit + offset."""
    if not off or df.empty:
        return df
    d = df.copy()
    d[["open", "high", "low", "close"]] = d[["open", "high", "low", "close"]] + off
    return d


TICK = {}


def get_tick(sym):
    if sym not in TICK:
        try:
            lst = bybit_get("/v5/market/instruments-info", dict(category=CATEGORY, symbol=PAIRS[sym]))["result"]["list"]
            TICK[sym] = float(lst[0]["priceFilter"]["tickSize"]) if lst else DEFAULT_TICK[sym]
        except Exception:
            TICK[sym] = DEFAULT_TICK[sym]
    return TICK[sym]


def dec_of(tick):
    s = f"{tick:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def load_offsets():
    try:
        with open(OFFSET_FILE) as f:
            for k, v in json.load(f).items():
                if k in OFFSETS:
                    OFFSETS[k] = float(v)
    except Exception:
        pass


def save_offsets():
    with open(OFFSET_FILE, "w") as f:
        json.dump(OFFSETS, f)


def xau_closed(now):
    """CFD emas tutup akhir pekan; Bybit tetap jalan 24/7 (harga bisa menyimpang dari CFD)."""
    wd, h = now.weekday(), now.hour
    return (wd == 5 and h >= 5) or wd == 6 or (wd == 0 and h < 5)


# ============================================================ INDIKATOR (sama dengan versi saham; hari = hari trading)
def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(d, prev_close):
    return pd.concat([d["high"] - d["low"], (d["high"] - prev_close).abs(),
                      (d["low"] - prev_close).abs()], axis=1).max(axis=1)


def add_indicators_1m(df):
    d = df.copy()
    d["ema9"] = d["close"].ewm(span=EMA_FAST_1M, adjust=False).mean()
    d["ema21"] = d["close"].ewm(span=EMA_SLOW_1M, adjust=False).mean()
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    d["ema50"] = d["close"].ewm(span=50, adjust=False).mean()
    d["atr"] = rma(true_range(d, d["close"].shift(1)), ATR_PERIOD)
    typical = (d["high"] + d["low"] + d["close"]) / 3.0
    day = tday(d.index)
    d["vwap"] = (typical * d["volume"]).groupby(day).cumsum() / d["volume"].groupby(day).cumsum()
    d["rvol"] = (d["volume"] / d["volume"].shift(1).rolling(20).mean()).replace([np.inf, -np.inf], np.nan)
    return d


def add_indicators_5m(df):
    pieces = []
    for _, dd in df.groupby(tday(df.index)):
        x = dd[["open", "high", "low", "close", "volume"]].resample(
            "5min", origin="start_day", label="left", closed="left"
        ).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        if x.empty:
            continue
        x["ema20"] = x["close"].ewm(span=EMA_FAST_5M, adjust=False).mean()
        x["ema50"] = x["close"].ewm(span=EMA_SLOW_5M, adjust=False).mean()
        atr = rma(true_range(x, x["close"].shift(1)), ADX_PERIOD)
        up, down = x["high"].diff(), -x["low"].diff()
        pdm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=x.index)
        mdm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=x.index)
        pdi = 100 * rma(pdm, ADX_PERIOD) / atr.replace(0, np.nan)
        mdi = 100 * rma(mdm, ADX_PERIOD) / atr.replace(0, np.nan)
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        x["adx"] = rma(dx, ADX_PERIOD)
        typical = (dd["high"] + dd["low"] + dd["close"]) / 3.0
        vw = (typical * dd["volume"]).cumsum() / dd["volume"].cumsum()
        x["vwap"] = vw.resample("5min", origin="start_day", label="left", closed="left").last().reindex(x.index)
        x["prior_high"] = x["high"].shift(1)
        x["ema20_slope"] = x["ema20"] > x["ema20"].shift(1)
        pieces.append(x)
    return pd.concat(pieces).sort_index() if pieces else pd.DataFrame()


def make_bias_5m(df5):
    b = df5.copy()
    s = np.zeros(len(b))
    s += np.where(b["close"] > b["vwap"], 20, 0)
    s += np.where(b["ema20"] > b["ema50"], 20, 0)
    s += np.where(b["ema20_slope"], 15, 0)
    s += np.where(b["close"] > b["ema20"], 10, 0)
    s += np.where(b["adx"] > ADX_MIN, 10, 0)
    s += np.where(b["close"] > b["prior_high"], 10, 0)
    b["bias_score"] = s
    b["long_bias"] = s >= MIN_SCORE
    return b


def _ns(idx):
    idx = pd.DatetimeIndex(idx)
    idx = idx.tz_convert(TZ) if idx.tz is not None else idx.tz_localize(TZ)
    try:
        return idx.as_unit("ns")
    except Exception:
        return idx.astype(f"datetime64[ns, {TZ}]")


def attach_completed_5m_bias(df1, df5):
    df1 = df1.copy()
    if df5.empty:
        df1["long_bias_5m"] = False
        return df1
    df1.index = _ns(df1.index)
    b = df5.sort_index().add_suffix("_5m")
    b.index = _ns(_ns(b.index) + pd.Timedelta(minutes=5))   # candle 5m baru dipakai setelah selesai
    return pd.merge_asof(df1.sort_index(), b.sort_index(), left_index=True, right_index=True,
                         direction="backward")


def prepare(df):
    d1 = add_indicators_1m(df)
    d5 = make_bias_5m(add_indicators_5m(df))
    m = attach_completed_5m_bias(d1, d5)
    vdist = (m["close"] / m["vwap"] - 1.0).abs()
    m["trigger_a"] = (m["long_bias_5m"].astype("boolean").fillna(False).astype(bool)
                      & (m["close"] > m["vwap"]) & (m["ema9"] > m["ema21"])
                      & (m["close"] > m["high"].shift(1)) & (m["rvol"] >= RVOL_MIN)
                      & (vdist <= VWAP_MAX_DISTANCE) & m["atr"].notna())
    return m


# ============================================================ ACCUMULATION → EXPANSION ENGINE

def _resample_ohlcv(d, minutes):
    """Resample OHLCV ke TF intraday yang dipakai AE, hanya untuk data lengkap."""
    if d is None or d.empty:
        return pd.DataFrame()
    rule = f"{int(minutes)}min"
    x = d[["open", "high", "low", "close", "volume"]].resample(
        rule, label="left", closed="left", origin="start_day"
    ).agg({"open":"first", "high":"max", "low":"min", "close":"last", "volume":"sum"}).dropna()
    if x.empty:
        return x
    # Bar terakhir dapat masih parsial. Hanya gunakan bucket yang benar-benar sudah selesai.
    last_1m = pd.Timestamp(d.index[-1])
    cutoff = last_1m.floor(rule) - pd.Timedelta(minutes=int(minutes))
    return x[x.index <= cutoff].copy()


def _ae_rsi(s, n=14):
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0)


def _ae_score_intraday(df5):
    """Port inti _ae_analisa Radar ke Bybit: skor 0-100, tanpa saham/Yahoo/IDR."""
    need = max(AE_BASE_BARS + 2, 62)
    if df5 is None or len(df5) < need:
        return None
    d = df5.copy()
    o, h, l, c, v = [d[k].astype(float) for k in ("open","high","low","close","volume")]
    harga = float(c.iloc[-1])
    pre = d.iloc[:-1]
    pc, ph, pl, pv = [pre[k].astype(float) for k in ("close","high","low","volume")]
    if len(pre) < AE_BASE_BARS:
        return None

    # A. AKUMULASI 40 poin — struktur langsung mengikuti Radar.
    hh = float(ph.tail(AE_BASE_BARS).max())
    ll = float(pl.tail(AE_BASE_BARS).min())
    rng = max(hh - ll, 1e-12)
    base_pct = rng / max(abs(ll), 1e-12) * 100
    pos = (float(pc.iloc[-1]) - ll) / rng
    near_base = (base_pct <= 45) and (pos <= 0.65)

    mfm = ((pc - pl) - (ph - pc)) / (ph - pl).replace(0, np.nan)
    cmf = float((mfm * pv).tail(20).sum() / max(float(pv.tail(20).sum()), 1e-12))
    cmf_pos = cmf > 0.05

    obv = (np.sign(pc.diff().fillna(0)) * pv).cumsum()
    obv_up = bool(obv.iloc[-1] > obv.iloc[max(0, len(obv)-21)] and
                  obv.iloc[-1] > obv.rolling(20).mean().iloc[-1])

    tr = pd.concat([ph - pl, (ph - pc.shift()).abs(), (pl - pc.shift()).abs()], axis=1).max(axis=1)
    atr_ratio = float(tr.tail(10).mean() / max(float(tr.tail(40).mean()), 1e-12))
    compress = atr_ratio < 0.85
    sc_acc = 10*near_base + 10*cmf_pos + 10*obv_up + 10*compress

    # B. BREAKOUT 30 poin.
    hh20 = float(ph.tail(AE_BREAK_BARS).max())
    b20 = harga > hh20
    b60 = harga > hh
    rg = max(float(h.iloc[-1] - l.iloc[-1]), 1e-12)
    body = abs(float(c.iloc[-1] - o.iloc[-1])) / rg
    strong = body > 0.60 and float(c.iloc[-1]) > float(o.iloc[-1])
    upper_wick = (float(h.iloc[-1]) - max(float(o.iloc[-1]), harga)) / rg
    sc_brk = 10*b20 + 10*b60 + 10*strong

    # C. VOLUME 20 poin.
    baseline = max(float(v.iloc[-21:-1].mean()), 1e-12)
    rvol = float(v.iloc[-1] / baseline)
    sc_vol = 10*(rvol > 2) + 10*(rvol > 3)

    # D. MOMENTUM 10 poin.
    rsi = float(_ae_rsi(c).iloc[-1])
    e20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
    e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    rsi_ok = 55 <= rsi <= 75
    trend_ok = harga > e20 > e50
    sc_mom = 5*rsi_ok + 5*trend_ok

    score = int(sc_acc + sc_brk + sc_vol + sc_mom)
    status = "MOMENTUM WATCH" if score >= AE_WATCH_SCORE else ("SETUP" if score >= AE_MIN_SCORE else "LEMAH")
    return {
        "score": score, "status": status, "harga": harga, "rvol": rvol, "rsi": rsi,
        "cmf": cmf, "atr_ratio": atr_ratio, "base_pct": base_pct,
        "hh20": hh20, "hh60": hh, "ll60": ll, "body": body, "upper_wick": upper_wick,
        "ema20": e20, "ema50": e50, "range": float(rng),
        "cek": {"Base sideways":near_base,"CMF positif":cmf_pos,"OBV akumulasi":obv_up,
                "Volatilitas mengerut":compress,"Break high":b20,"Break high base":b60,
                "Candle body kuat":strong,"RVOL > 2":rvol>2,"RSI 55-75":rsi_ok,
                "Harga>EMA20>EMA50":trend_ok},
        "sub": {"Akumulasi":(sc_acc,40),"Breakout":(sc_brk,30),"Volume":(sc_vol,20),"Momentum":(sc_mom,10)},
    }


def find_accum_expansion(m, tick, last_bars=None, include_all=False):
    """Deteksi expansion pada TF 5m sambil tetap mengirim timestamp 1m yang relevan."""
    if not AE_ENABLED or m is None or m.empty:
        return []
    last_bars = max(1, int(last_bars or AE_LAST_BARS))
    d5 = _resample_ohlcv(m, AE_TF_MINUTES)
    if len(d5) < max(AE_BASE_BARS + 2, 62):
        return []
    out = []
    start = max(AE_BASE_BARS + 1, len(d5) - last_bars)
    for j in range(len(d5) - 1, start - 1, -1):
        q = d5.iloc[:j+1]
        a = _ae_score_intraday(q)
        if not a or a["score"] < AE_MIN_SCORE:
            continue
        sig5 = d5.index[j]
        sig1 = sig5 + pd.Timedelta(minutes=AE_TF_MINUTES - 1)
        # Signal candle harus benar-benar tersedia di 1m cache.
        sub = m[m.index <= sig1]
        if sub.empty:
            continue
        r1 = sub.iloc[-1]
        signal_time = sub.index[-1]
        atr = float(max((d5["high"] - d5["low"]).tail(14).mean(), tick))
        entry = round_tick(float(r1["close"]), "up", tick)
        sl = round_tick(min(float(r1["low"]), a["ll60"]) - AE_SL_ATR*atr, "down", tick)
        risk = entry - sl
        if risk <= 0:
            continue
        tp_distance = max(AE_TP_ATR * atr, 1.60 * risk)
        tp = round_tick(entry + tp_distance, "down", tick)
        rr = (tp-entry)/risk if risk > 0 else 0
        if rr < 1.0:
            continue
        out.append(dict(kind="C", side="BUY", order_type="NEXT_OPEN", i=int(len(sub)-1), time=signal_time,
                        entry=entry, sl=sl, tp=tp, risk=risk, risk_pct=risk/entry*100,
                        reward_pct=(tp-entry)/entry*100, rr_eff=rr, status="PENDING", tick=tick, dec=dec_of(tick),
                        ae_score=a["score"], ae_status=a["status"], ae_rvol=a["rvol"], ae_rsi=a["rsi"],
                        ae_cmf=a["cmf"], ae_base_pct=a["base_pct"], ae_atr_ratio=a["atr_ratio"],
                        breakout_high=a["hh20"], base_low=a["ll60"], mode="RADAR_PORT_5M"))
        if not include_all:
            break
    return out


# ============================================================ SHADOW FAKEOUT 1M — HELPER PLAN BYBIT

def _sf_params(sym):
    sym = sym.upper()
    if sym == "XAUUSD":
        return float(SF_SL_PIPS_XAU), float(SF_PIP_XAU)
    return float(SF_SL_PIPS_BTC), float(SF_PIP_BTC)


def _sf_candle_stats(o, h, l, c):
    body = abs(c-o)
    lower = max(0.0, min(o,c)-l)
    upper = max(0.0, h-max(o,c))
    full = max(h-l, 1e-12)
    return body, lower, upper, full


def _sf_make_plan(sym, m, i, sweep_idx, breakout_idx, range_low, range_high, side, sl, tp, strict, tick):
    row = m.iloc[i]
    entry = round_tick(float(row["close"]), "up" if side=="BUY" else "down", tick)
    risk = abs(entry-sl); reward = abs(tp-entry)
    rr = reward/max(risk,1e-12)
    if risk <= 0 or reward <= 0 or rr < SF_MIN_RR:
        return None
    sl_side_mode = "below sweep" if side=="BUY" else "above sweep"
    return dict(kind="SF", side=side, order_type="NEXT_OPEN", i=i, time=m.index[i],
                entry=entry, sl=round_tick(sl,"down" if side=="BUY" else "up",tick),
                tp=round_tick(tp,"down" if side=="BUY" else "up",tick), risk=risk,
                risk_pct=risk/entry*100, reward_pct=reward/entry*100, rr_eff=rr,
                status="PENDING", tick=tick, dec=dec_of(tick), sig_low=float(row["low"]),
                sig_high=float(row["high"]), range_low=float(range_low), range_high=float(range_high),
                sweep_index=int(sweep_idx), breakout_index=int(breakout_idx) if breakout_idx is not None else -1,
                strict=bool(strict), sf_mode="MATERIAL_STRICT" if strict else "SWEEP_RECLAIM_FALLBACK",
                sl_rule=sl_side_mode, invalidation=float(row["low"] if side=="BUY" else row["high"]))


# ============================================================ SHADOW FAKEOUT 1M — PORT SETIA XAU-BTC (bot1.py)
# Port dari bot1.py (xau-btc-nasdaq-termux-bot-final.zip):
#   engine : material_settings / material_entry_at / make_material_signal  ("PDF Shadow Fakeout + Pattern/BOS + Retest")
#   pesan  : chart_caption  (kartu detail: Entry/Target/SL/Dasar/Confluence/Session/Demand-Supply/Pattern/Alasan/Konfirmasi)
#   chart  : render_chart   (putih 960x540: zona SNR, trendline pivot, EMA21, kotak Entry/SL/TP, volume profile + POC,
#            histogram volume, trend-band, harmonic XABCD) — murni stdlib (zlib/struct), tanpa Pillow/matplotlib.
# Semua helper berawalan xb_/XB agar mudah dibandingkan baris-per-baris dengan bot1.py.
# Beda yang disengaja:
#   * candle bot ini sudah CLOSED semua (parse_klines membuang bar berjalan) -> tidak ada lagi pemotongan candle
#     terakhir seperti di bot1.py (candles[:-1]); harga sudah ber-offset CFD.
#   * spasi pada label chart ikut tampil (di bot1.py spasi hilang), label harga menyesuaikan lebar untuk BTC.
#   * tambahan: POC volume profile (bar + garis + label) dan penanda setup (range, SWEEP, BOS, RETEST).

@dataclass
class XBCandle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class XBSignal:
    symbol: str
    timeframe: str
    side: str
    entry: float
    stop_loss: float
    take_profit: float
    candle_timestamp: int
    session: str
    score: int
    reasons: list
    analysis: dict = field(default_factory=dict)
    support: float = 0.0
    resistance: float = 0.0
    demand_zone: str = ""
    supply_zone: str = ""
    patterns: list = field(default_factory=list)
    offset_applied: float = 0.0
    target_basis: str = "market structure"
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    setup: dict = field(default_factory=dict)       # tambahan Bybit: koordinat setup untuk gambar chart

    @property
    def risk(self):
        return abs(self.entry - self.stop_loss)


XB_NY = "America/New_York"


def _xb_local(ts, tz_name):
    return pd.Timestamp(int(ts), unit="s", tz="UTC").tz_convert(tz_name)


def xb_fmt_time(ts, tz_name=TZ):
    return _xb_local(ts, tz_name).strftime("%d-%m-%Y %H:%M")


def xb_fmt_price(value):
    return f"{value:,.2f}"


def xb_session_for(ts):
    """Jam sesi Pine ULTRA (America/New_York): NY 09:30-16:00, Asia 20:00-02:00, London 03:00-11:30, sisanya DEAD ZONE."""
    local = _xb_local(ts, XB_NY)
    minute = local.hour * 60 + local.minute
    if 9 * 60 + 30 <= minute <= 16 * 60:
        return "NEW YORK"
    if minute >= 20 * 60 or minute <= 2 * 60:
        return "ASIA"
    if 3 * 60 <= minute <= 11 * 60 + 30:
        return "LONDON"
    return "DEAD ZONE"


def xb_candles_from_df(df):
    """DataFrame OHLCV (index waktu tz-aware) -> list XBCandle. Semua bar di df sudah closed."""
    if df is None or len(df) == 0:
        return []
    ts = [int(t.timestamp()) for t in df.index]
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
    v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)          # volume kosong/NaN tidak boleh merusak profile
    return [XBCandle(ts[k], float(o[k]), float(h[k]), float(l[k]), float(c[k]), float(v[k])) for k in range(len(ts))]


def xb_ema(values, length):
    if not values:
        return []
    alpha = 2 / (length + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append((value * alpha) + (output[-1] * (1 - alpha)))
    return output


def xb_atr(candles, length=14):
    true_ranges = []
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.close
        true_ranges.append(
            max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close))
        )
    return xb_ema(true_ranges, max(1, length))


def xb_candle_range(candle):
    return max(candle.high - candle.low, 1e-9)


def xb_candle_patterns(candles):
    """Pola candle/struktur umum (registry sama dengan bot1.py candle_patterns)."""
    if len(candles) < 5:
        return []
    last = candles[-1]
    previous = candles[-2]
    body = abs(last.close - last.open)
    upper_wick = last.high - max(last.open, last.close)
    lower_wick = min(last.open, last.close) - last.low
    patterns = []

    if body <= xb_candle_range(last) * 0.12:
        patterns.append("Doji")
    if lower_wick >= max(body * 2, xb_candle_range(last) * 0.35) and upper_wick <= body:
        patterns.append("Hammer")
    if upper_wick >= max(body * 2, xb_candle_range(last) * 0.35) and lower_wick <= body:
        patterns.append("Shooting Star")
    if (
        previous.close < previous.open
        and last.close > last.open
        and last.open <= previous.close
        and last.close >= previous.open
    ):
        patterns.append("Bullish Engulfing")
    if (
        previous.close > previous.open
        and last.close < last.open
        and last.open >= previous.close
        and last.close <= previous.open
    ):
        patterns.append("Bearish Engulfing")
    if last.high < previous.high and last.low > previous.low:
        patterns.append("Inside Bar")

    recent = candles[-20:]
    prior_high = max(item.high for item in recent[:-3])
    prior_low = min(item.low for item in recent[:-3])
    if last.high >= prior_high and last.close < prior_high:
        patterns.append("Double Top / Rejection")
    if last.low <= prior_low and last.close > prior_low:
        patterns.append("Double Bottom / Rejection")

    if len(candles) >= 8:
        first_half = candles[-8:-4]
        second_half = candles[-4:]
        if min(item.low for item in second_half) > min(item.low for item in first_half):
            patterns.append("Higher Low")
        if max(item.high for item in second_half) > max(item.high for item in first_half):
            patterns.append("Higher High")
        if max(item.high for item in second_half) < max(item.high for item in first_half):
            patterns.append("Lower High")
        if min(item.low for item in second_half) < min(item.low for item in first_half):
            patterns.append("Lower Low")

    harmonic = xb_harmonic_pattern(candles)
    if harmonic:
        patterns.append(harmonic)
    return patterns[:10]


def xb_pivot_points(candles, left=2, right=2):
    highs, lows = [], []
    for index in range(left, len(candles) - right):
        current = candles[index]
        before = candles[index - left: index]
        after = candles[index + 1: index + right + 1]
        if current.high >= max(item.high for item in before + after):
            highs.append(current.high)
        if current.low <= min(item.low for item in before + after):
            lows.append(current.low)
    return highs, lows


def xb_ratio_close(value, target, tolerance=0.1):
    return abs(value - target) <= tolerance


def xb_pivot_swings(candles, left=2, right=2):
    """Swing high/low bergantian (indeks, 'H'/'L', harga) untuk trendline & harmonic."""
    start = max(0, len(candles) - 160)
    data = candles[start:]
    raw_points = []
    for local_index in range(left, len(data) - right):
        current = data[local_index]
        before = data[local_index - left: local_index]
        after = data[local_index + 1: local_index + right + 1]
        if current.high >= max(item.high for item in before + after):
            raw_points.append((local_index + start, "H", current.high))
        if current.low <= min(item.low for item in before + after):
            raw_points.append((local_index + start, "L", current.low))
    points = []
    minimum_gap = max(3, left + right)
    for candidate in sorted(raw_points):
        if not points:
            points.append(candidate)
            continue
        previous = points[-1]
        if candidate[1] == previous[1]:
            if candidate[0] - previous[0] < minimum_gap:
                is_more_extreme = candidate[2] > previous[2] if candidate[1] == "H" else candidate[2] < previous[2]
                if is_more_extreme:
                    points[-1] = candidate
            continue
        if candidate[0] - previous[0] >= minimum_gap:
            points.append(candidate)
    return points


def xb_harmonic_trace(candles):
    """Keluarga harmonic terdeteksi + titik X-A-B-C-D (untuk digambar di chart)."""
    swings = xb_pivot_swings(candles)
    if len(swings) < 5:
        return "", []
    selected = swings[-5:]
    x, a, b, c, d = [point[2] for point in selected]
    xa = abs(a - x)
    ab = abs(b - a)
    bc = abs(c - b)
    cd = abs(d - c)
    if min(xa, ab, bc, cd) <= 1e-9:
        return "", []
    ab_xa = ab / xa
    bc_ab = bc / ab
    cd_bc = cd / bc
    ad_xa = abs(d - a) / xa
    candidates = [
        ("Gartley", 0.618, (0.382, 0.886), (1.272, 1.618), 0.786),
        ("Bat", (0.382 + 0.5) / 2, (0.382, 0.886), (1.618, 2.618), 0.886),
        ("Butterfly", 0.786, (0.382, 0.886), (1.618, 2.24), 1.27),
        ("Crab", (0.382 + 0.618) / 2, (0.382, 0.886), (2.618, 3.618), 1.618),
    ]
    for name, ab_target, bc_range, cd_range, ad_target in candidates:
        ab_ok = xb_ratio_close(ab_xa, ab_target, 0.12) if isinstance(ab_target, float) else False
        if (
            ab_ok
            and bc_range[0] - 0.1 <= bc_ab <= bc_range[1] + 0.1
            and cd_range[0] - 0.2 <= cd_bc <= cd_range[1] + 0.2
            and xb_ratio_close(ad_xa, ad_target, 0.18)
        ):
            direction = "bullish" if d < c else "bearish"
            return f"Harmonic {name} {direction}", [(point[0], point[2]) for point in selected]
    return "", []


def xb_harmonic_pattern(candles):
    return xb_harmonic_trace(candles)[0]


def xb_market_structure(candles):
    """Support/resistance terdekat + zona demand/supply (lebar 0.35 ATR) + pola. `candles` = closed semua."""
    data = list(candles)
    if not data:
        return {"support": 0.0, "resistance": 0.0, "demand_low": 0.0, "demand_high": 0.0,
                "supply_low": 0.0, "supply_high": 0.0, "patterns": []}
    entry = data[-1].close
    current_atr = max(xb_atr(data, 14)[-1], entry * 0.0001)
    highs, lows = xb_pivot_points(data[-120:])
    below = [value for value in lows if value < entry]
    above = [value for value in highs if value > entry]
    support = max(below) if below else min(item.low for item in data[-30:])
    resistance = min(above) if above else max(item.high for item in data[-30:])
    zone_width = current_atr * 0.35
    return {
        "support": support,
        "resistance": resistance,
        "demand_low": support - zone_width,
        "demand_high": support + zone_width,
        "supply_low": resistance - zone_width,
        "supply_high": resistance + zone_width,
        "patterns": xb_candle_patterns(data),
    }


# ---------------------------------------------------------------- engine "PDF Shadow Fakeout + Pattern/BOS + Retest"
def xb_material_settings(symbol):
    """Parameter model Shadow Fakeout/PDF. 50 pip = contoh materi (bukan konstanta pasar): pip size per instrumen."""
    sl_pips, pip_size = _sf_params(symbol)
    return {
        "range_lookback": int(SF_RANGE_LOOKBACK),
        "setup_lookback": int(SF_SETUP_LOOKBACK),
        "shadow_ratio": float(SF_SHADOW_RATIO),
        "min_shadow_range": float(SF_MIN_SHADOW_RANGE),
        "sl_pips": float(sl_pips),
        "pip_size": float(pip_size),
        "min_rr": float(SF_MIN_RR),
        "tolerance": float(SF_TOLERANCE_ATR),          # fraksi lebar range (bot1.py: 0.08)
    }


def _xb_material_shadow(candle):
    """Body, lower shadow, upper shadow, dan range penuh candle."""
    body = abs(candle.close - candle.open)
    lower = max(0.0, min(candle.open, candle.close) - candle.low)
    upper = max(0.0, candle.high - max(candle.open, candle.close))
    return body, lower, upper, xb_candle_range(candle)


def xb_material_entry_at(candles, index, symbol, settings=None):
    """Satu setup PDF terkonfirmasi memakai candle sampai `index` saja (tanpa look-ahead).

    Urutan ketat: 1) range sebelumnya, 2) sweep shadow panjang di luar range, 3) body reclaim (breakbody/BOS),
    4) entry hanya bila candle berikutnya me-retest dan menahan zona yang direclaim.
    """
    settings = settings or xb_material_settings(symbol)
    range_lookback = int(settings["range_lookback"])
    setup_lookback = int(settings["setup_lookback"])
    if index < range_lookback + 3 or index >= len(candles):
        return None

    pip_buffer = float(settings["sl_pips"]) * float(settings["pip_size"])
    tolerance_fraction = float(settings.get("tolerance", 0.08))
    candidate_start = max(range_lookback, index - setup_lookback)
    candidates = []
    for sweep_index in range(candidate_start, index - 1):
        prior = candles[sweep_index - range_lookback: sweep_index]
        if len(prior) < range_lookback:
            continue
        range_low = min(item.low for item in prior)
        range_high = max(item.high for item in prior)
        range_size = max(range_high - range_low, pip_buffer * 2, 1e-9)
        sweep = candles[sweep_index]
        body, lower_shadow, upper_shadow, full_range = _xb_material_shadow(sweep)
        body_reference = max(body, range_size * 0.01, 1e-9)
        shadow_ratio = float(settings["shadow_ratio"])
        min_shadow_range = float(settings["min_shadow_range"])

        is_buy_sweep = (
            sweep.low < range_low
            and sweep.close > range_low
            and lower_shadow >= body_reference * shadow_ratio
            and lower_shadow / max(full_range, 1e-9) >= min_shadow_range
        )
        is_sell_sweep = (
            sweep.high > range_high
            and sweep.close < range_high
            and upper_shadow >= body_reference * shadow_ratio
            and upper_shadow / max(full_range, 1e-9) >= min_shadow_range
        )
        if not is_buy_sweep and not is_sell_sweep:
            continue

        side = "BUY" if is_buy_sweep else "SELL"
        reclaim_level = range_low if side == "BUY" else range_high
        breakout_index = None
        for possible_index in range(sweep_index + 1, index):
            possible = candles[possible_index]
            previous_close = candles[possible_index - 1].close
            if side == "BUY":
                body_reclaim = possible.close > reclaim_level and (
                    possible.open <= reclaim_level or previous_close <= reclaim_level
                )
            else:
                body_reclaim = possible.close < reclaim_level and (
                    possible.open >= reclaim_level or previous_close >= reclaim_level
                )
            if body_reclaim:
                breakout_index = possible_index
                break
        if breakout_index is None:
            continue

        retest = candles[index]
        retest_tolerance = max(range_size * tolerance_fraction, pip_buffer * 0.5)
        if side == "BUY":
            retest_holds = (
                retest.low <= reclaim_level + retest_tolerance
                and retest.close > reclaim_level
                and retest.close >= retest.open
                and retest.low > sweep.low
            )
            stop_loss = sweep.low - pip_buffer
            take_profit = range_high - pip_buffer
        else:
            retest_holds = (
                retest.high >= reclaim_level - retest_tolerance
                and retest.close < reclaim_level
                and retest.close <= retest.open
                and retest.high < sweep.high
            )
            stop_loss = sweep.high + pip_buffer
            take_profit = range_low + pip_buffer
        if not retest_holds:
            continue

        entry = retest.close
        risk = abs(entry - stop_loss)
        reward = abs(take_profit - entry)
        reward_risk = reward / max(risk, 1e-9)
        if risk <= 0 or reward <= 0 or reward_risk < float(settings["min_rr"]):
            continue
        candidates.append(
            {
                "side": side,
                "entry": entry,
                "sl": stop_loss,
                "tp": take_profit,
                "risk": risk,
                "rr": reward_risk,
                "range_low": range_low,
                "range_high": range_high,
                "sweep_index": sweep_index,
                "breakout_index": breakout_index,
                "timestamp": retest.timestamp,
            }
        )
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------- renderer chart (PNG murni Python, tanpa Pillow)
XB_GLYPHS = {
    "X": ("10001", "01010", "00100", "01010", "10001"),
    "A": ("01110", "10001", "11111", "10001", "10001"),          # bot1.py: 7 baris (lebih tinggi dari huruf lain)
    "B": ("11110", "10001", "11110", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "11110", "10000", "11111"),
    "F": ("11111", "10000", "11110", "10000", "10000"),
    "H": ("10001", "10001", "11111", "10001", "10001"),
    "K": ("10001", "10010", "11100", "10010", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001"),
    "T": ("11111", "00100", "00100", "00100", "00100"),
    "R": ("11110", "10001", "11110", "10100", "10010"),
    "Y": ("10001", "10001", "01010", "00100", "00100"),
    "S": ("01111", "10000", "01110", "00001", "11110"),
    "L": ("10000", "10000", "10000", "10000", "11111"),
    "P": ("11110", "10001", "11110", "10000", "10000"),
    "I": ("11111", "00100", "00100", "00100", "11111"),
    "G": ("01111", "10000", "10111", "10001", "01111"),
    "U": ("10001", "10001", "10001", "10001", "01110"),
    "O": ("01110", "10001", "10001", "10001", "01110"),
    "M": ("10001", "11011", "10101", "10001", "10001"),
    "V": ("10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10101", "11011", "10001"),          # tambahan Bybit (untuk label SWEEP)
    "Z": ("11111", "00010", "00100", "01000", "11111"),          # tambahan Bybit
    "J": ("00111", "00010", "00010", "10010", "01100"),          # tambahan Bybit
    "Q": ("01110", "10001", "10101", "10010", "01101"),          # tambahan Bybit
    "0": ("01110", "10001", "10011", "10101", "01110"),
    "1": ("00100", "01100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00010", "00100", "11111"),
    "3": ("11110", "00001", "01110", "00001", "11110"),
    "4": ("10010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "11110"),
    "6": ("01110", "10000", "11110", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "00100"),
    "8": ("01110", "10001", "01110", "10001", "01110"),
    "9": ("01110", "10001", "01111", "00001", "01110"),
    "-": ("00000", "00000", "11111", "00000", "00000"),
    ":": ("00000", "00100", "00000", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00100"),
    "/": ("00001", "00010", "00100", "01000", "10000"),
    "+": ("00000", "00100", "11111", "00100", "00000"),
}


class XBCanvas:
    """Renderer RGB PNG kecil tanpa dependensi (port PngCanvas bot1.py)."""

    def __init__(self, width=960, height=540, color=(255, 255, 255)):
        self.width = width
        self.height = height
        self.pixels = bytearray(bytes(color) * (width * height))

    def pixel(self, x, y, color):
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        index = (y * self.width + x) * 3
        self.pixels[index: index + 3] = bytes(color)

    def rect(self, x1, y1, x2, y2, color):
        left, right = sorted((max(0, x1), min(self.width - 1, x2)))
        top, bottom = sorted((max(0, y1), min(self.height - 1, y2)))
        for y in range(top, bottom + 1):
            start = (y * self.width + left) * 3
            end = (y * self.width + right + 1) * 3
            self.pixels[start:end] = bytes(color) * (right - left + 1)

    def line(self, x1, y1, x2, y2, color, width=1):
        dx = abs(x2 - x1)
        sx = 1 if x1 < x2 else -1
        dy = -abs(y2 - y1)
        sy = 1 if y1 < y2 else -1
        error = dx + dy
        while True:
            for offset in range(-(width // 2), width // 2 + 1):
                self.pixel(x1 + offset, y1, color)
                self.pixel(x1, y1 + offset, color)
            if x1 == x2 and y1 == y2:
                break
            twice = 2 * error
            if twice >= dy:
                error += dy
                x1 += sx
            if twice <= dx:
                error += dx
                y1 += sy

    def dashed(self, x1, y, x2, color):
        for start in range(x1, x2, 16):
            self.line(start, y, min(start + 8, x2), y, color)

    def dotted(self, x1, y, x2, color):
        for start in range(x1, x2, 6):
            self.line(start, y, min(start + 2, x2), y, color)

    def circle(self, center_x, center_y, radius, color):
        for y in range(center_y - radius, center_y + radius + 1):
            for x in range(center_x - radius, center_x + radius + 1):
                if (x - center_x) ** 2 + (y - center_y) ** 2 <= radius ** 2:
                    self.pixel(x, y, color)

    def label(self, x, y, text, color):
        """Label font bitmap: 7 px per huruf, spasi 4 px, latar putih agar terbaca di atas candle."""
        cells = []
        for char in text:
            if char == " ":
                cells.append(None)
                continue
            glyph = XB_GLYPHS.get(char.upper())
            if glyph:
                cells.append(glyph)
        glyphs = [cell for cell in cells if cell]
        if not glyphs:
            return
        steps = [4 if cell is None else len(cell[0]) + 2 for cell in cells]
        width = sum(steps)
        height = max(len(glyph) for glyph in glyphs) + 4
        self.rect(x - 3, y - 3, x + width, y + height, (255, 255, 255))
        cursor = x
        for cell, step in zip(cells, steps):
            if cell:
                for row, bits in enumerate(cell):
                    for column, bit in enumerate(bits):
                        if bit == "1":
                            self.rect(cursor + column, y + row, cursor + column + 1, y + row + 1, color)
            cursor += step

    def png(self):
        rows = b"".join(b"\x00" + bytes(self.pixels[row * self.width * 3: (row + 1) * self.width * 3])
                        for row in range(self.height))

        def chunk(name, payload):
            return (struct.pack(">I", len(payload)) + name + payload
                    + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF))

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b"")
        )


def xb_render_chart(symbol, timeframe, candles, signal=None, structure=None,
                    poc=True, setup=True, bars=120, demo=False):
    """Chart putih 960x540 (port render_chart bot1.py): header status, candle, EMA21, zona supply/demand, trendline
    pivot, kotak Entry/SL/TP, volume profile + POC, histogram volume, trend-band, tanggal-jam, harmonic XABCD.

    `candles` = candle closed sampai (dan termasuk) candle sinyal. `poc` menyorot bin volume terbesar (bar + garis +
    label); `setup` menggambar range Shadow Fakeout dan penanda SWEEP / BOS / RETEST dari `signal.setup`.
    Chart hanya memvisualkan sinyal yang sudah dihitung; tidak mengubah entry, SL, TP, atau RR.
    """
    canvas = XBCanvas(width=960, height=540, color=(255, 255, 255))
    analysis_candles = list(candles)
    visible = analysis_candles[-int(bars):]
    if not visible:
        return canvas.png()
    structure = structure or xb_market_structure(analysis_candles)
    values = [item.high for item in visible] + [item.low for item in visible]
    levels = [
        float(structure["support"]),
        float(structure["resistance"]),
        float(structure["demand_low"]),
        float(structure["demand_high"]),
        float(structure["supply_low"]),
        float(structure["supply_high"]),
    ]
    # Target chart independen dari TP1: pakai TP3 bila ada.
    display_tp = float(getattr(signal, "tp3", 0.0) or 0.0) if signal else 0.0
    if signal and display_tp <= 0.0:
        display_tp = float(signal.take_profit)
    if signal:
        levels.extend([signal.entry, signal.stop_loss, display_tp])
    sx = (getattr(signal, "setup", None) or {}) if (signal is not None and setup) else {}
    for key in ("range_low", "range_high"):
        if sx.get(key) is not None:
            levels.append(float(sx[key]))
    minimum = min(values + levels)
    maximum = max(values + levels)
    padding = max((maximum - minimum) * 0.08, 0.01)
    minimum -= padding
    maximum += padding
    # Margin kiri menyesuaikan lebar label harga (XAU 7 karakter = 48 px seperti bot1.py; BTC lebih lebar).
    label_chars = max(len(f"{minimum:.2f}"), len(f"{maximum:.2f}"))
    left = max(48, label_chars * 7 - 1)
    outer_right, top = canvas.width - 26, 30
    profile_width = 150
    plot_right = outer_right - profile_width
    bottom = canvas.height - 170
    volume_top, volume_bottom = bottom + 22, bottom + 82
    trend_top, trend_bottom = bottom + 100, canvas.height - 24

    def x_at(index):
        return left + int(index * (plot_right - left) / max(1, len(visible) - 1))

    def y_at(price):
        return bottom - int((price - minimum) * (bottom - top) / max(maximum - minimum, 1e-9))

    latest = visible[-1]
    latest_dt = _xb_local(latest.timestamp, TZ)
    change = latest.close - latest.open
    change_pct = (change / latest.open * 100.0) if latest.open else 0.0
    header = f"{symbol} {timeframe.upper()} {latest_dt.strftime('%d-%m-%Y %H:%M')}"
    ohlc = f"O {latest.open:.2f} H {latest.high:.2f} L {latest.low:.2f} C {latest.close:.2f}"
    change_text = f"CHG {change:+.2f} {change_pct:+.2f}"
    canvas.label(left, 2, header, (15, 45, 90))
    canvas.label(left + 290, 2, ohlc, (20, 90, 45) if change >= 0 else (170, 35, 45))
    canvas.label(left + 650, 2, change_text, (20, 130, 65) if change >= 0 else (190, 35, 45))
    if demo:
        canvas.label(outer_right - 70, 2, "DEMO DATA", (200, 40, 40))

    # Grid dan zona SNR tetap tampil walau tanpa sinyal.
    for row in range(1, 6):
        y = top + (bottom - top) * row // 6
        canvas.line(left, y, plot_right, y, (224, 228, 234))
    for column in range(1, 8):
        x = left + (plot_right - left) * column // 8
        canvas.line(x, top, x, bottom, (238, 241, 245))
    supply_high = float(structure["supply_high"])
    supply_low = float(structure["supply_low"])
    demand_high = float(structure["demand_high"])
    demand_low = float(structure["demand_low"])
    canvas.rect(left, y_at(supply_high), plot_right, y_at(supply_low), (255, 226, 226))
    canvas.rect(left, y_at(demand_high), plot_right, y_at(demand_low), (224, 246, 232))
    for y_value, color in ((supply_high, (190, 55, 55)), (supply_low, (190, 55, 55)),
                           (demand_high, (40, 130, 80)), (demand_low, (40, 130, 80))):
        canvas.line(left, y_at(y_value), plot_right, y_at(y_value), color, 1)
    canvas.label(plot_right - 92, y_at((supply_high + supply_low) / 2) - 4, "SUPPLY", (170, 40, 45))
    canvas.label(plot_right - 92, y_at((demand_high + demand_low) / 2) - 4, "DEMAND", (25, 120, 65))

    # Trendline price-action: hubungkan pivot/fractal swing yang sudah terkonfirmasi.
    visible_start = len(analysis_candles) - len(visible)
    swings = xb_pivot_swings(analysis_candles, left=2, right=2)
    swing_lows = [(index - visible_start, price) for index, kind, price in swings
                  if kind == "L" and visible_start <= index < len(analysis_candles)]
    swing_highs = [(index - visible_start, price) for index, kind, price in swings
                   if kind == "H" and visible_start <= index < len(analysis_candles)]

    def choose_swing_pair(points):
        if len(points) < 2:
            return None
        candidates = []
        for first_index in range(len(points) - 1):
            for second_index in range(first_index + 1, len(points)):
                first, second = points[first_index], points[second_index]
                if second[0] - first[0] >= 5:
                    distance = second[0] - first[0]
                    candidates.append((distance, second[0], first, second))
        if not candidates:
            return points[-2], points[-1]
        # Pasangan terkonfirmasi terlebar & terbaru; tanpa regresi atau slope paksaan.
        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, first, second = candidates[-1]
        return first, second

    trend_color = (192, 171, 40)
    low_pair = choose_swing_pair(swing_lows)
    high_pair = choose_swing_pair(swing_highs)
    for pair in (low_pair, high_pair):
        if not pair:
            continue
        (x1_i, y1_p), (x2_i, y2_p) = pair
        canvas.line(x_at(x1_i), y_at(y1_p), x_at(x2_i), y_at(y2_p), trend_color, 2)
        if x2_i < len(visible) - 1:
            slope = (y2_p - y1_p) / max(1, x2_i - x1_i)
            projected = y2_p + slope * (len(visible) - 1 - x2_i)
            if minimum <= projected <= maximum:
                canvas.line(x_at(x2_i), y_at(y2_p), x_at(len(visible) - 1), y_at(projected), trend_color, 2)
    if low_pair or high_pair:
        canvas.label(left + 4, bottom - 18, "TRENDLINE", (155, 125, 20))

    # Kotak Sniper Entry (risk/reward) digambar di belakang candle.
    if signal:
        box_start = max(left + 24, x_at(max(0, len(visible) - 24)))
        box_end = plot_right - 8
        entry_y = y_at(signal.entry)
        sl_y = y_at(signal.stop_loss)
        tp_y = y_at(display_tp)
        canvas.rect(box_start, min(entry_y, tp_y), box_end, max(entry_y, tp_y), (222, 248, 229))
        canvas.rect(box_start, min(entry_y, sl_y), box_end, max(entry_y, sl_y), (255, 232, 232))
        canvas.line(box_start, entry_y, box_end, entry_y, (40, 40, 40), 2)

    closes = [item.close for item in visible]
    moving = xb_ema(closes, 21)
    for index in range(1, len(moving)):
        canvas.line(x_at(index - 1), y_at(moving[index - 1]), x_at(index), y_at(moving[index]), (64, 191, 255), 2)
    candle_width = max(2, int((plot_right - left) / max(len(visible), 1) * 0.55))
    for index, candle in enumerate(visible):
        x = x_at(index)
        bullish = candle.close >= candle.open
        color = (24, 150, 80) if bullish else (205, 46, 58)
        canvas.line(x, y_at(candle.high), x, y_at(candle.low), color, 1)
        canvas.rect(x - candle_width, y_at(max(candle.open, candle.close)),
                    x + candle_width, y_at(min(candle.open, candle.close)), color)

    for price, color in (
        (float(structure["support"]), (49, 161, 255)),
        (float(structure["resistance"]), (255, 177, 67)),
    ):
        canvas.dashed(left, y_at(price), plot_right, color)
    if signal:
        for price, color in (
            (signal.entry, (35, 35, 35)),
            (signal.stop_loss, (210, 40, 55)),
            (display_tp, (20, 145, 75)),
        ):
            canvas.dashed(left, y_at(price), plot_right, color)
        label_x = max(left + 8, plot_right - 112)
        canvas.label(label_x, y_at(signal.entry) - 7, "ENTRY", (35, 35, 35))
        canvas.label(label_x, y_at(signal.stop_loss) - 7, "SL", (210, 40, 55))
        canvas.label(label_x, y_at(display_tp) - 7, "TP", (20, 145, 75))

    # Setup Shadow Fakeout: range sebelum sweep + penanda SWEEP / BOS / RETEST.
    if sx:
        index_of = {item.timestamp: n for n, item in enumerate(visible)}
        range_start = index_of.get(sx.get("range_start_ts"), 0)
        for key, text, above in (("range_high", "RANGE H", True), ("range_low", "RANGE L", False)):
            if sx.get(key) is None:
                continue
            y = y_at(float(sx[key]))
            canvas.dotted(x_at(range_start), y, plot_right, (110, 118, 150))
            canvas.label(x_at(range_start) + 4, y - 12 if above else y + 5, text, (84, 92, 128))
        buy = str(getattr(signal, "side", "BUY")).upper() == "BUY"

        def mark(ts_key, text, color, use_high, above):
            idx = index_of.get(sx.get(ts_key))
            if idx is None:
                return
            candle = visible[idx]
            px, py = x_at(idx), y_at(candle.high if use_high else candle.low)
            canvas.circle(px, py, 4, color)
            ly = py - 16 if above else py + 8
            ly = max(top + 2, min(ly, bottom - 10))
            lx = max(left + 2, min(px - (7 * len(text)) // 2, plot_right - 7 * len(text) - 2))
            canvas.label(lx, ly, text, color)

        mark("sweep_ts", "SWEEP", (240, 120, 0), not buy, not buy)
        mark("breakout_ts", "BOS", (30, 90, 205), buy, buy)
        mark("retest_ts", "RETEST", (0, 150, 136), not buy, not buy)

    # Penanda pola di candle terakhir.
    pattern_text = " ".join(str(item) for item in structure.get("patterns") or []).lower()
    marker_color = (255, 196, 72)
    if any(name in pattern_text for name in ("bullish", "hammer", "higher low", "higher high", "double bottom")):
        marker_color = (38, 205, 143)
        canvas.line(x_at(len(visible) - 1), y_at(visible[-1].low) + 16, x_at(len(visible) - 1), y_at(visible[-1].low), marker_color, 2)
    elif any(name in pattern_text for name in ("bearish", "shooting star", "lower high", "lower low", "double top")):
        marker_color = (239, 83, 105)
        canvas.line(x_at(len(visible) - 1), y_at(visible[-1].high) - 16, x_at(len(visible) - 1), y_at(visible[-1].high), marker_color, 2)
    canvas.circle(x_at(len(visible) - 1), y_at(visible[-1].close), 5, marker_color)

    # Volume profile horizontal di kanan (24 bin) + POC (Point of Control = bin volume terbesar).
    profile_left, profile_right = plot_right + 10, outer_right
    bins = 24
    bin_size = max((maximum - minimum) / bins, 1e-9)
    profile = [0.0] * bins
    for candle in visible:
        typical = (candle.high + candle.low + candle.close) / 3
        bucket = max(0, min(bins - 1, int((typical - minimum) / bin_size)))
        profile[bucket] += max(float(candle.volume), 1.0)
    max_profile = max(profile) if profile else 1.0
    poc_bucket = profile.index(max_profile) if (poc and max_profile > 0) else -1
    canvas.line(plot_right + 4, top, plot_right + 4, bottom, (175, 180, 188), 1)
    for bucket, amount in enumerate(profile):
        low_price = minimum + bucket * bin_size
        high_price = minimum + (bucket + 1) * bin_size
        y1, y2 = y_at(high_price), y_at(low_price)
        width = int((amount / max_profile) * max(1, profile_right - profile_left - 6))
        color = (151, 226, 204) if low_price <= visible[-1].close else (247, 190, 198)
        if bucket == poc_bucket:
            color = (176, 84, 214)
        canvas.rect(profile_right - width, y1, profile_right, y2, color)
    if poc_bucket >= 0:
        poc_price = minimum + (poc_bucket + 0.5) * bin_size
        poc_y = y_at(poc_price)
        canvas.dashed(left, poc_y, plot_right, (176, 84, 214))
        canvas.label(left + 6, poc_y - 12, f"POC {poc_price:.2f}", (140, 40, 175))
    canvas.label(profile_left + 3, top + 4, "VOLUME", (20, 80, 90))
    # Label harga di dua sisi: skala kiri dan sisi profile.
    for scale_index in range(0, 6):
        scale_price = maximum - (maximum - minimum) * scale_index / 5
        text = f"{scale_price:.2f}"
        canvas.label(4, y_at(scale_price) - 4, text, (45, 45, 45))
        canvas.label(profile_right - (len(text) * 7 + 6), y_at(scale_price) - 4, text, (45, 45, 45))

    # Histogram volume dan panel trend-band.
    max_volume = max((float(candle.volume) for candle in visible), default=1.0) or 1.0
    for index, candle in enumerate(visible):
        x = x_at(index)
        height = int(float(candle.volume) / max_volume * max(1, volume_bottom - volume_top - 4))
        volume_color = (70, 185, 90) if candle.close >= candle.open else (235, 112, 112)
        half = max(1, candle_width // 2)
        canvas.rect(x - half, volume_bottom - height, x + half, volume_bottom, volume_color)
    canvas.line(left, volume_top, plot_right, volume_top, (185, 190, 198), 1)
    canvas.line(left, trend_top, plot_right, trend_top, (185, 190, 198), 1)
    trend_ema = xb_ema([c.close for c in visible], 21)
    for index in range(1, len(trend_ema)):
        up = trend_ema[index] >= trend_ema[index - 1]
        band_color = (30, 90, 205) if up else (210, 45, 55)
        canvas.line(x_at(index - 1), trend_bottom - 10, x_at(index), trend_bottom - (28 if up else 6), band_color, 3)
    status = "BULL" if trend_ema[-1] >= trend_ema[max(0, len(trend_ema) - 5)] else "BEAR"
    canvas.label(left + 4, top + 4, status, (20, 70, 190) if status == "BULL" else (190, 40, 50))
    # Tanggal-jam di bawah panel trend.
    date_step = max(1, len(visible) // 6)
    for index in range(0, len(visible), date_step):
        date_label = _xb_local(visible[index].timestamp, TZ).strftime("%d-%m %H:%M")
        canvas.label(max(left, x_at(index) - 20), trend_bottom + 2, date_label, (70, 70, 70))

    # Jalur harmonic X-A-B-C-D bila detektor rasio mengonfirmasi.
    harmonic_name, harmonic_points = xb_harmonic_trace(analysis_candles)
    trace_pixels = []
    for source_index, price in harmonic_points:
        relative_index = source_index - visible_start
        if 0 <= relative_index < len(visible):
            trace_pixels.append((x_at(relative_index), y_at(price)))
    if harmonic_name and len(trace_pixels) == 5:
        trace_color = (186, 126, 255)
        for first, second in zip(trace_pixels, trace_pixels[1:]):
            canvas.line(first[0], first[1], second[0], second[1], trace_color, 3)
        for text, point in zip(("X", "A", "B", "C", "D"), trace_pixels):
            canvas.circle(point[0], point[1], 6, trace_color)
            canvas.label(point[0] + 8, point[1] - 13, text, trace_color)

    canvas.rect(left, top - 12, left + 180, top - 5, (49, 161, 255))
    canvas.rect(left + 190, top - 12, left + 350, top - 5, (255, 177, 67))
    canvas.rect(left + 360, top - 12, left + 520, top - 5, (38, 205, 143))
    return canvas.png()


def xb_chart_caption(symbol, timeframe, structure, signal):
    """Caption detail (port chart_caption bot1.py). Jam bar = WIB; jam SWEEP/BREAKBODY = New York (diberi label)."""
    patterns = ", ".join(structure.get("patterns") or []) or "belum ada pola terkonfirmasi"
    lines = [f"CHART {symbol} {timeframe}"]
    if signal:
        lines.append(f"{signal.analysis.get('STRATEGY', 'SIGNAL')} | {signal.side}")
        lines.append(f"Entry  : {xb_fmt_price(signal.entry)}")
        lines.append(f"Target1: {xb_fmt_price(signal.tp1)}" if signal.tp1 else f"Target : {xb_fmt_price(signal.take_profit)}")
        if signal.tp2:
            lines.append(f"Target2: {xb_fmt_price(signal.tp2)}")
        if signal.tp3:
            lines.append(f"Target3: {xb_fmt_price(signal.tp3)}")
        lines.append(f"SL     : {xb_fmt_price(signal.stop_loss)}")
        lines.append(f"Dasar  : {signal.target_basis}")
        lines.append(f"Confluence: {signal.score}/13")
        lines.append(f"Session/Bar: {signal.session} / {xb_fmt_time(signal.candle_timestamp)} WIB")
        lines.append(f"Demand: {signal.demand_zone} | Supply: {signal.supply_zone}")
        lines.append(f"Pattern: {', '.join(signal.patterns) if signal.patterns else 'belum ada pola'}")
        lines.append(f"Alasan lengkap: {'; '.join(signal.reasons)}")
        lines.append("Konfirmasi: " + "; ".join(f"{key}={value}" for key, value in signal.analysis.items() if key != "STRATEGY"))
    else:
        lines.append("Belum ada sinyal dengan confluence minimum; chart tetap menampilkan struktur pasar.")
    lines.append(f"Support {xb_fmt_price(float(structure['support']))} | Resistance {xb_fmt_price(float(structure['resistance']))}")
    lines.append(f"Demand {float(structure['demand_low']):.2f}-{float(structure['demand_high']):.2f} | "
                 f"Supply {float(structure['supply_low']):.2f}-{float(structure['supply_high']):.2f}")
    lines.append(f"Pattern: {patterns}")
    lines.append("Analisis bukan jaminan profit; cek spread dan berita sebelum entry.")
    return "\n".join(lines)


# ---------------------------------------------------------------- jembatan ke tracker/Book/Telegram bot Bybit
def _xb_strict_plan(sym, m, candles, i, e, settings, tick):
    """Plan Bybit (tracker/Book) + XBSignal (caption & chart) dari satu setup strict xau-btc di bar `i`."""
    side = e["side"]
    plan = _sf_make_plan(sym, m, i, e["sweep_index"], e["breakout_index"], e["range_low"], e["range_high"],
                         side, e["sl"], e["tp"], True, tick)
    if plan is None:
        return None
    rl, rh = float(e["range_low"]), float(e["range_high"])
    sl_pips = float(settings["sl_pips"])
    pip_distance = sl_pips * float(settings["pip_size"])
    sweep_c, bos_c, retest_c = candles[e["sweep_index"]], candles[e["breakout_index"]], candles[i]
    range_start = candles[max(0, e["sweep_index"] - int(settings["range_lookback"]))]
    plan["invalidation"] = float(sweep_c.low if side == "BUY" else sweep_c.high)
    plan["xs"] = XBSignal(
        symbol=sym, timeframe="1m", side=side,
        entry=float(plan["entry"]), stop_loss=float(plan["sl"]), take_profit=float(plan["tp"]),
        candle_timestamp=int(retest_c.timestamp), session=xb_session_for(retest_c.timestamp), score=5,
        reasons=[
            "liquidity sweep dengan shadow panjang",
            "breakbody/BOS terkonfirmasi",
            "retest zona bertahan",
            f"SL di luar invalidasi + {pip_distance:g} ({sl_pips:g} pip)",
            "TP menuju zona lawan",
        ],
        analysis={
            "STRATEGY": "PDF Shadow Fakeout + Pattern/BOS + Retest",
            "ZONE": f"{rl:.8f} - {rh:.8f}",
            "SWEEP": xb_fmt_time(sweep_c.timestamp, XB_NY) + " NY",
            "BREAKBODY": xb_fmt_time(bos_c.timestamp, XB_NY) + " NY",
            "RR": f"{float(plan['rr_eff']):.2f}R",
            "SL_RULE": f"invalidasi + {sl_pips:g} pip",
            "TP_RULE": "zona lawan",
        },
        support=rl, resistance=rh,
        demand_zone=f"{rl:.8f}", supply_zone=f"{rh:.8f}",
        patterns=["Shadow Fakeout", "Liquidity Sweep", "Breakbody/BOS", "Retest"],
        offset_applied=float(OFFSETS.get(sym, 0.0)),
        target_basis="zona lawan setelah sweep + breakbody + retest",
        setup=dict(mode="STRICT", range_low=rl, range_high=rh, range_start_ts=int(range_start.timestamp),
                   sweep_ts=int(sweep_c.timestamp), breakout_ts=int(bos_c.timestamp), retest_ts=int(retest_c.timestamp)),
    )
    plan["xs_structure"] = xb_market_structure(candles[: i + 1])
    return plan


def _xb_fallback_plan(sym, m, candles, i, tick, settings):
    """Fallback non-strict (sweep + shadow + reclaim tanpa BOS/retest) = perilaku lama bot Bybit, diberi label jelas.
    Matikan dengan SF_FALLBACK=0 bila ingin murni setup PDF strict seperti xau-btc."""
    if i < SF_FALLBACK_RECENT:
        return None
    pip_p, pip_size = _sf_params(sym)
    pip_buffer = pip_p * pip_size
    prior = m.iloc[max(0, i - SF_FALLBACK_RECENT):i]
    recent_low = float(prior["low"].min())
    recent_high = float(prior["high"].max())
    oo, hh, ll, cc = (float(m[k].iloc[i]) for k in ("open", "high", "low", "close"))
    body, lower, upper, full = _sf_candle_stats(oo, hh, ll, cc)
    close_pos = (cc - ll) / full
    buy = ll < recent_low and cc > recent_low and lower >= 2 * max(body, tick) and close_pos >= 0.60
    sell = hh > recent_high and cc < recent_high and upper >= 2 * max(body, tick) and close_pos <= 0.40
    if not (buy or sell):
        return None
    side = "BUY" if buy else "SELL"
    atr = max(float((m["high"] - m["low"]).iloc[max(0, i - 13): i + 1].mean()), tick)   # hanya bar sampai i
    buffer = max(pip_buffer, 0.10 * atr)
    if side == "BUY":
        sl = ll - buffer
        tp = max(recent_high, cc + 2 * (cc - sl))
    else:
        sl = hh + buffer
        tp = min(recent_low, cc - 2 * (sl - cc))
    plan = _sf_make_plan(sym, m, i, i, None, recent_low, recent_high, side, sl, tp, False, tick)
    if plan is None:
        return None
    plan["fallback_reason"] = "sweep + shadow + reclaim (tanpa BOS/retest penuh)"
    bar = candles[i]
    range_start = candles[max(0, i - SF_FALLBACK_RECENT)]
    plan["xs"] = XBSignal(
        symbol=sym, timeframe="1m", side=side,
        entry=float(plan["entry"]), stop_loss=float(plan["sl"]), take_profit=float(plan["tp"]),
        candle_timestamp=int(bar.timestamp), session=xb_session_for(bar.timestamp), score=3,
        reasons=[
            f"sweep {'di bawah low' if side == 'BUY' else 'di atas high'} {SF_FALLBACK_RECENT} bar terakhir",
            "shadow panjang (>= 2x body) lalu close kembali ke dalam range (reclaim)",
            "BOS + retest penuh BELUM terkonfirmasi (mode fallback, bukan setup PDF strict)",
            f"SL di luar ujung shadow + buffer {buffer:g}",
            "TP = batas range atau 2R (yang lebih jauh)",
        ],
        analysis={
            "STRATEGY": "Shadow Fakeout 1M FALLBACK (sweep + shadow + reclaim)",
            "MODE": "FALLBACK tanpa BOS/retest penuh (bukan setup PDF strict)",
            "ZONE": f"{recent_low:.8f} - {recent_high:.8f}",
            "SWEEP": xb_fmt_time(bar.timestamp, XB_NY) + " NY",
            "RR": f"{float(plan['rr_eff']):.2f}R",
            "SL_RULE": "ujung shadow + buffer",
            "TP_RULE": "batas range / 2R",
        },
        support=recent_low, resistance=recent_high,
        demand_zone=f"{recent_low:.8f}", supply_zone=f"{recent_high:.8f}",
        patterns=["Shadow Fakeout", "Liquidity Sweep", "Reclaim"],
        offset_applied=float(OFFSETS.get(sym, 0.0)),
        target_basis="batas range sebelumnya atau 2R (fallback)",
        setup=dict(mode="FALLBACK", range_low=recent_low, range_high=recent_high,
                   range_start_ts=int(range_start.timestamp), sweep_ts=int(bar.timestamp),
                   breakout_ts=None, retest_ts=None),
    )
    plan["xs_structure"] = xb_market_structure(candles[: i + 1])
    return plan


def find_shadow_fakeout_1m(m, sym, tick, last_bars=None, include_all=False):
    """Shadow Fakeout 1M — port setia xau-btc: range -> shadow sweep -> body reclaim/BOS -> retest; BUY + SELL.

    Dicek di `last_bars` bar closed terakhir (sinyal telat tetap tertangkap; dedupe lewat key `sent`).
    Bila tidak ada setup strict di suatu bar dan SF_FALLBACK=1, dipakai fallback sweep+shadow+reclaim."""
    if not SF_ENABLED or m is None or m.empty:
        return []
    settings = xb_material_settings(sym)
    range_back, setup_back = settings["range_lookback"], settings["setup_lookback"]
    n = len(m)
    if n < range_back + setup_back + 3:                       # sama dengan syarat minimum make_material_signal
        return []
    candles = xb_candles_from_df(m)
    last_bars = max(1, int(last_bars or SF_LAST_BARS))
    out = []
    for i in range(max(range_back + 3, n - last_bars), n):
        plan = None
        entry = xb_material_entry_at(candles, i, sym, settings)
        if entry is not None:
            plan = _xb_strict_plan(sym, m, candles, i, entry, settings, tick)
        if plan is None and SF_FALLBACK:
            plan = _xb_fallback_plan(sym, m, candles, i, tick, settings)
        if plan is not None:
            out.append(plan)
    return out


def make_chart_xb(m, plan, ticker, path, demo=False):
    """Chart xau-btc (putih, volume profile + POC, zona, trendline, kotak Entry/SL/TP) untuk sinyal SF."""
    candles = xb_candles_from_df(m.iloc[: plan["i"] + 1])
    png = xb_render_chart(ticker, "1m", candles, plan["xs"], structure=plan.get("xs_structure"),
                          poc=SF_CHART_POC, setup=SF_CHART_SETUP, bars=SF_CHART_BARS, demo=demo)
    with open(path, "wb") as f:
        f.write(png)
    return path


def _last_sent_time(sent, sym, kind):
    """Waktu sinyal terakhir (kind) yang sudah terkirim untuk simbol ini, dari key `sent` (sym|kind|iso-time)."""
    times = [_key_time(k) for k in sent if k.startswith(f"{sym}|{kind}|")]
    return max(times) if times else None


# ============================================================ TICK
def round_tick(p, mode, tick):
    q = p / tick
    return round((math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 1e-9)) * tick, 8)


# ============================================================ SINYAL A
def find_momentum(m, tick):
    """Sinyal hanya valid jika terjadi di bar terakhir (entry = open bar berikutnya)."""
    i = len(m) - 1
    if i < 30 or not bool(m["trigger_a"].iloc[i]):
        return []
    r = m.iloc[i]
    entry = round_tick(float(r["close"]), "up", tick)          # estimasi open berikutnya
    atr = float(r["atr"])
    sl = round_tick(entry - ATR_SL_MULT * atr, "down", tick)
    risk = entry - sl
    if risk <= 0:
        return []
    tp = round_tick(entry + A_RR * risk, "down", tick)
    return [dict(kind="A", i=i, time=m.index[i], entry=entry, sl=sl, tp=tp, risk=risk,
                 risk_pct=risk / entry * 100, reward_pct=(tp - entry) / entry * 100,
                 rr_eff=(tp - entry) / risk, status="PENDING", tick=tick, dec=dec_of(tick),
                 rvol=float(r["rvol"]), adx=float(r.get("adx_5m", np.nan)),
                 bias=float(r.get("bias_score_5m", np.nan)),
                 vdist=float(r["close"] / r["vwap"] - 1) * 100)]


# ============================================================ SINYAL B
def find_fakeout(m, tick, last_bars=3, include_all=False):
    """Sweep low LOOKBACK bar + lower shadow + reclaim. Filter tren: EMA20 > EMA50."""
    o, h, l, c = (m[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(m)
    days = np.array(tday(m.index))
    out = []
    for i in range(max(LOOKBACK + 1, n - last_bars), n):
        if days[i - LOOKBACK] != days[i]:
            continue                                            # prior low harus di hari trading yang sama
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        pl = float(np.min(l[i - LOOKBACK:i]))
        ratio = (min(o[i], c[i]) - l[i]) / rng
        reclaim = (c[i] - pl) / rng
        if not (l[i] < pl and c[i] > pl and ratio >= SHADOW_MIN and reclaim >= RECLAIM_MIN
                and m["ema20"].iloc[i] > m["ema50"].iloc[i]):
            continue
        entry = round_tick(h[i] * (1 + BUFFER_PCT), "up", tick)
        sl = round_tick(l[i] * (1 - 0.0005), "down", tick)
        risk = entry - sl
        if risk <= 0:
            continue
        tp = round_tick(entry + B_RR * risk, "down", tick)
        status = "PENDING"
        for j in range(i + 1, n):
            if h[j] >= entry:
                status = "TRIGGERED"; break
            if c[j] < l[i]:
                status = "CANCELLED"; break
        if status == "PENDING" or include_all:
            out.append(dict(kind="B", i=i, time=m.index[i], entry=entry, sl=sl, tp=tp, risk=risk,
                            risk_pct=risk / entry * 100, reward_pct=(tp - entry) / entry * 100,
                            rr_eff=(tp - entry) / risk, status=status, tick=tick, dec=dec_of(tick),
                            sig_low=float(l[i]), prior_low=pl, ratio=ratio, reclaim=reclaim))
    return out


# ============================================================ CHART
def make_chart(m, plan, ticker, path, demo=False, before=70, after=14):
    # Sinyal SF: chart xau-btc (putih, volume profile + POC, zona SNR, trendline, kotak Entry/SL/TP).
    if plan.get("kind") == "SF" and plan.get("xs") is not None:
        return make_chart_xb(m, plan, ticker, path, demo=demo)
    # Chart helper juga aman dipanggil langsung dari unit-test/demo dengan OHLCV mentah.
    if "ema20" not in m.columns or "ema50" not in m.columns:
        m = add_indicators_1m(m)
    i, n = plan["i"], len(m)
    dec = plan.get("dec", 2)
    lo, hi = max(0, i - before), min(n - 1, i + after)
    sub = m.iloc[lo:hi + 1]
    xs = np.arange(lo, hi + 1)
    o, h, l, c = (sub[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    bg, fg, grid, up, dn = "#0e1117", "#e6e6e6", "#222a35", "#26a69a", "#ef5350"
    entry, sl, tp = plan["entry"], plan["sl"], plan["tp"]
    ymin, ymax = min(l.min(), sl), max(h.max(), tp)
    pad = (ymax - ymin) * 0.08
    ymin, ymax = ymin - pad, ymax + pad
    tiny = (ymax - ymin) * 0.0015

    fig, ax = plt.subplots(figsize=(10, 6.2), dpi=130)
    fig.patch.set_facecolor(bg); ax.set_facecolor(bg)
    fig.subplots_adjust(left=0.07, right=0.86, top=0.90, bottom=0.10)
    cols = np.where(c >= o, up, dn)
    ax.vlines(xs, l, h, colors=cols, linewidth=1.0, zorder=3)
    for x, oo, cc, col in zip(xs, o, c, cols):
        ax.add_patch(Rectangle((x - 0.35, min(oo, cc)), 0.7, max(abs(cc - oo), tiny),
                               facecolor=col, edgecolor=col, zorder=4))

    if plan["kind"] == "A":
        for col, lab, clr in (("ema9", "EMA9", "#f5c542"), ("ema21", "EMA21", "#4da3ff"),
                              ("vwap", "VWAP", "#ff9f43")):
            ax.plot(xs, sub[col], color=clr, lw=1.2, label=lab, zorder=2)
        title = "Momentum Long  \u2022  MARKET (open bar berikutnya)"
        ax.annotate("Breakout + RVOL", xy=(i, plan["entry"]), xytext=(i - 12, plan["entry"] + pad),
                    color=fg, fontsize=9, ha="center", arrowprops=dict(arrowstyle="->", color=fg, lw=1))
        foot = (f"RVOL {plan['rvol']:.1f}x  \u2022  bias 5m {plan['bias']:.0f}  \u2022  "
                f"jarak VWAP {plan['vdist']:.2f}%")
    elif plan["kind"] == "B":
        ax.plot(xs, sub["ema20"], color="#f5c542", lw=1.2, label="EMA20", zorder=2)
        ax.plot(xs, sub["ema50"], color="#4da3ff", lw=1.2, label="EMA50", zorder=2)
        ax.hlines(plan["prior_low"], i - LOOKBACK - 0.5, i + 0.5, colors="#b388ff", linestyles=":", linewidth=1.3, zorder=2)
        ax.annotate("Sweep + reclaim", xy=(i, plan["sig_low"]), xytext=(i - 12, plan["sig_low"] - pad * 0.9),
                    color=fg, fontsize=9, ha="center", arrowprops=dict(arrowstyle="->", color=fg, lw=1))
        title = "Legacy Shadow Fakeout  \u2022  BUY STOP"
        foot = f"lower shadow {plan['ratio']:.0%}  \u2022  reclaim {plan['reclaim']:.0%}"
    elif plan["kind"] == "C":
        ax.plot(xs, sub["ema20"], color="#f5c542", lw=1.2, label="EMA20", zorder=2)
        ax.plot(xs, sub["ema50"], color="#4da3ff", lw=1.2, label="EMA50", zorder=2)
        if plan.get("breakout_high") is not None:
            ax.hlines(plan["breakout_high"], lo, i + 0.5, colors="#c77dff", linestyles=":", linewidth=1.2, zorder=2)
        title = "Accumulation \u2192 Expansion  \u2022  BUY"
        foot = (f"score {plan.get('ae_score',0):.0f}/100  \u2022  RVOL {plan.get('ae_rvol',0):.1f}x  \u2022  "
                f"RSI {plan.get('ae_rsi',0):.0f}  \u2022  base {plan.get('ae_base_pct',0):.1f}%")
        ax.annotate("Expansion", xy=(i, plan["entry"]), xytext=(i - 12, plan["entry"] + pad),
                    color=fg, fontsize=9, ha="center", arrowprops=dict(arrowstyle="->", color=fg, lw=1))
    else:
        ax.plot(xs, sub["ema20"], color="#f5c542", lw=1.2, label="EMA20", zorder=2)
        ax.plot(xs, sub["ema50"], color="#4da3ff", lw=1.2, label="EMA50", zorder=2)
        if plan.get("range_low") is not None: ax.hlines(plan["range_low"], lo, i+0.5, colors="#b388ff", linestyles=":", linewidth=1.1, zorder=2)
        if plan.get("range_high") is not None: ax.hlines(plan["range_high"], lo, i+0.5, colors="#ff9f43", linestyles=":", linewidth=1.1, zorder=2)
        side = plan.get("side","BUY")
        sig_price = plan.get("sig_low") if side=="BUY" else plan.get("sig_high")
        title = f"Shadow Fakeout 1M  \u2022  {side}"
        foot = (f"{plan.get('sf_mode','MATERIAL')}  \u2022  range {plan.get('range_low',0):,.{dec}f}-{plan.get('range_high',0):,.{dec}f}"
                f"  \u2022  RR 1:{plan.get('rr_eff',0):.1f}")
        ax.annotate("Sweep / reclaim", xy=(i, sig_price), xytext=(i - 12, sig_price + (pad if side=="SELL" else -pad)),
                    color=fg, fontsize=9, ha="center", arrowprops=dict(arrowstyle="->", color=fg, lw=1))

    ax.axvspan(i - 0.5, i + 0.5, color="#ffffff", alpha=0.07, zorder=1)
    xr, x0 = i + after + 1, i + 0.5
    ax.fill_between([x0, xr], entry, tp, color=up, alpha=0.13, zorder=1)
    ax.fill_between([x0, xr], sl, entry, color=dn, alpha=0.13, zorder=1)
    for y, col, lab in ((entry, "#ffffff", "ENTRY"), (sl, dn, "SL"), (tp, up, "TP")):
        ax.hlines(y, x0, xr, colors=col, linestyles="--", linewidth=1.1, zorder=2)
        ax.text(xr + 0.3, y, f"{lab} {y:,.{dec}f}", color=col, fontsize=9, va="center",
                ha="left", clip_on=False, fontweight="bold")

    ax.set_xlim(lo - 1, xr); ax.set_ylim(ymin, ymax)
    tp_ = list(range(lo - (lo % 10) + 10, hi + 1, 10))
    ax.set_xticks(tp_)
    ax.set_xticklabels([m.index[k].strftime("%H:%M") for k in tp_])
    ax.tick_params(colors=fg, labelsize=8)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.{dec}f}"))
    ax.grid(color=grid, linewidth=0.6)
    for sp in ax.spines.values():
        sp.set_color(grid)
    leg = ax.legend(loc="upper left", fontsize=8, frameon=False)
    for t in leg.get_texts():
        t.set_color(fg)
    t = plan["time"]
    fig.text(0.07, 0.945, f"{ticker}  \u2022  {title}  \u2022  1M", color=fg, fontsize=13,
             fontweight="bold", ha="left")
    fig.text(0.07, 0.915, f"{t:%d %b %Y %H:%M} WIB   |   RR 1:{plan['rr_eff']:.1f}   |   "
             f"risk {plan['risk_pct']:.2f}%  reward {plan['reward_pct']:.2f}%",
             color="#9aa4b2", fontsize=9, ha="left")
    fig.text(0.07, 0.03, foot, color="#9aa4b2", fontsize=8, ha="left")
    if demo:
        ax.text(0.5, 0.5, "CONTOH / DEMO DATA", transform=ax.transAxes, fontsize=44,
                color="#ffffff", alpha=0.07, ha="center", va="center", rotation=20)
    fig.savefig(path, facecolor=bg)
    plt.close(fig)
    return path


# ============================================================ TELEGRAM
def build_caption(sym, p):
    # Sinyal SF: kartu detail xau-btc (plain text; di-escape karena parse_mode HTML).
    if p.get("kind") == "SF" and p.get("xs") is not None:
        return html.escape(xb_chart_caption(sym, "1m", p.get("xs_structure") or xb_market_structure([]), p["xs"]), quote=False)
    d = p["dec"]
    side = p.get("side", "BUY")
    sign = "BUY" if side == "BUY" else "SELL"
    body = [
        f"Entry {p['entry']:>10,.{d}f}  " + ("market / next open" if p.get("order_type") == "NEXT_OPEN" else ("buy stop" if side=="BUY" else "sell stop")),
        f"SL    {p['sl']:>10,.{d}f}  -{p['risk']:,.{d}f} ({p['risk_pct']:.2f}%)",
        f"TP    {p['tp']:>10,.{d}f}  +{p['risk']*p['rr_eff']:,.{d}f} ({p['reward_pct']:.2f}%)"
    ]
    # Offset tetap dipakai internal untuk penyesuaian harga, tetapi tidak ditampilkan.
    if p["kind"] == "A":
        head = [f"🔵 <b>MOMENTUM LONG — {sym}</b>",
                f"Bias 5m + breakout 1m • {p['time']:%d %b %Y %H:%M} WIB", "",
                "<pre>" + "\n".join(body) + "</pre>",
                f"RR 1:{p['rr_eff']:.1f} • RVOL {p['rvol']:.1f}x • bias {p['bias']:.0f} • ADX {p['adx']:.0f} • VWAP +{p['vdist']:.2f}%" ]
    elif p["kind"] == "B":
        head = [f"🟢 <b>LEGACY SHADOW FAKEOUT — {sym}</b>",
                f"BUY STOP • 1M • {p['time']:%d %b %Y %H:%M} WIB", "",
                "<pre>" + "\n".join(body) + "</pre>",
                f"RR 1:{p['rr_eff']:.1f} • lower shadow {p['ratio']:.0%} • reclaim {p['reclaim']:.0%}",
                f"Sweep di bawah low {LOOKBACK} bar ({p['prior_low']:,.{d}f})" ]
    elif p["kind"] == "C":
        head = [f"🧲 <b>ACCUMULATION → EXPANSION — {sym}</b>",
                f"BUY • 5M basis + 1M trigger • {p['time']:%d %b %Y %H:%M} WIB", "",
                "<pre>" + "\n".join(body) + "</pre>",
                f"Score {p['ae_score']}/100 • {p['ae_status']} • RVOL {p['ae_rvol']:.1f}x • RSI {p['ae_rsi']:.0f} • CMF {p['ae_cmf']:+.2f}",
                f"Base 60x5m {p['ae_base_pct']:.1f}% • ATR compression {p['ae_atr_ratio']:.2f}" ,
                "✅ Expansion dipicu oleh breakout high + volume/momentum confluence."]
    else:
        mode = p.get("sf_mode", "MATERIAL")
        why = "liquidity sweep → body reclaim/BOS → retest" if p.get("strict") else p.get("fallback_reason", "sweep + shadow + reclaim")
        head = [f"🟣 <b>SHADOW FAKEOUT 1M — {sym} {sign}</b>",
                f"{mode} • {p['time']:%d %b %Y %H:%M} WIB", "",
                "<pre>" + "\n".join(body) + "</pre>",
                f"RR 1:{p['rr_eff']:.1f} • {why}",
                f"Range {p['range_low']:,.{d}f} → {p['range_high']:,.{d}f} • invalidasi {p['invalidation']:,.{d}f}" ]
    head.append("<i>Bukan saran investasi.</i>")
    return "\n".join(head)


def tg_ready():
    return bool(TG_TOKEN and TG_CHAT)


def tg_post(method, data, files=None):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data, files=files, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:200]}")


def topic_id(kind=None):
    """Pilih message_thread_id otomatis berdasarkan jenis sinyal/event."""
    tid = TOPIC_IDS.get(kind or "", "")
    if not tid and kind in ("TP", "SL", "TIMEOUT"):
        tid = TOPIC_IDS.get("EVENT", "")
    try:
        return int(tid) if tid else None
    except (TypeError, ValueError):
        print(f"[TELEGRAM] topic ID tidak valid untuk {kind}: {tid!r}; fallback ke chat utama")
        return None


def tg_send_photo(path, caption, kind=None):
    head, rest = caption, ""
    if len(caption) > 1000:
        cut = caption.rfind("\n", 0, 1000)
        head, rest = caption[:cut], caption[cut + 1:]
    with open(path, "rb") as f:
        thread = topic_id(kind)
        data = {"chat_id": TG_CHAT, "caption": head, "parse_mode": "HTML"}
        if thread is not None:
            data["message_thread_id"] = thread
        tg_post("sendPhoto", data, files={"photo": f})
    if rest:
        data = {"chat_id": TG_CHAT, "text": rest, "parse_mode": "HTML"}
        if thread is not None:
            data["message_thread_id"] = thread
        tg_post("sendMessage", data)


def send_text(text, thread=None):
    if tg_ready():
        d = {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
        if thread:
            d["message_thread_id"] = thread               # balas di topik forum yang sama
        tg_post("sendMessage", d)
    else:
        print("[TELEGRAM belum diset]\n" + text)


# ============================================================ TRACKER TP / SL
ACTIVE = ("PENDING", "OPEN")
HIT = ("TP", "SL", "TIMEOUT")


def simulate(t, raw):
    """Replay bar 1m secara konservatif + BE/SL+ otomatis.

    BE/SL+ bersifat pre-emptive: stop digeser SEBELUM stop awal tersentuh.
    Tidak ada cara aman untuk membuat posisi profit setelah SL sudah terkena.
    Untuk menghindari look-ahead pada candle 1m, trigger BE/SL+ yang muncul
    di suatu candle baru berlaku mulai candle berikutnya.
    """
    bars = adj(raw, t.get("off", 0.0))
    sig_t = pd.Timestamp(t["sig_time"])
    nb = bars[bars.index > sig_t]
    entry = float(t["entry"])
    initial_sl = float(t.get("initial_sl", t["sl"]))
    tp = float(t["tp"])
    tick = float(t["tick"])
    side = t.get("side", "BUY")
    order_type = t.get("order_type", "NEXT_OPEN" if t["kind"] != "B" else "BUY_STOP")
    cap = A_MAX_HOLD_BARS if t["kind"] == "A" else (SF_MAX_HOLD_BARS if t["kind"] == "SF" else MAX_HOLD_BARS)
    out = dict(status="PENDING", fill=None, fill_time=None, exit=None, exit_time=None,
               initial_sl=initial_sl, active_sl=initial_sl, be_stage=0, stop_mode="INITIAL")
    held = waited = 0
    active_sl = initial_sl
    stage = 0

    for ts, (o,h,l,c) in zip(nb.index, nb[["open","high","low","close"]].to_numpy(float)):
        iso = ts.isoformat()
        if out["status"] == "PENDING":
            waited += 1
            if order_type == "BUY_STOP":
                if side != "BUY":
                    out.update(status="CANCELLED", exit_time=iso); break
                if h < entry:
                    if c < t.get("sig_low", -math.inf) or waited >= B_EXPIRE_BARS:
                        out.update(status="CANCELLED", exit_time=iso); break
                    continue
                fill = entry if o <= entry else round_tick(o, "up", tick)
            else:
                fill = round_tick(o, "up" if side == "BUY" else "down", tick)
            gap_bad = (side == "BUY" and (fill >= tp or fill <= initial_sl)) or (side == "SELL" and (fill <= tp or fill >= initial_sl))
            if gap_bad:
                out.update(status="CANCELLED", exit_time=iso); break
            out.update(status="OPEN", fill=float(fill), fill_time=iso)
            # R dihitung dari harga fill ke SL awal agar tetap konsisten dengan risiko nyata.
            risk0 = abs(float(fill) - initial_sl)
            if risk0 <= 0:
                out.update(status="CANCELLED", exit_time=iso); break
        held += 1

        # Konservatif: cek stop/TP aktif DULU. Jika candle yang sama juga menyentuh
        # trigger BE/SL+, pergeseran stop berlaku mulai candle berikutnya.
        if side == "BUY":
            if l <= active_sl:
                out.update(status="SL", exit=float(o if o < active_sl else active_sl), exit_time=iso)
                break
            if h >= tp:
                out.update(status="TP", exit=float(o if o > tp else tp), exit_time=iso)
                break
        else:
            if h >= active_sl:
                out.update(status="SL", exit=float(o if o > active_sl else active_sl), exit_time=iso)
                break
            if l <= tp:
                out.update(status="TP", exit=float(o if o < tp else tp), exit_time=iso)
                break

        # Geser stop setelah candle selesai agar tidak memakai urutan intrabar yang
        # tidak diketahui dari data OHLC 1m. Tahap 2 selalu mengungguli tahap 1.
        if BE_ENABLED:
            if side == "BUY":
                trigger2 = float(fill) + SLPLUS_TRIGGER_R * risk0
                trigger1 = float(fill) + BE_TRIGGER_R * risk0
                new_stage = stage
                new_sl = active_sl
                if h >= trigger2:
                    new_stage = 2
                    new_sl = max(new_sl, float(fill) + SLPLUS_LOCK_R * risk0)
                elif h >= trigger1:
                    new_stage = max(new_stage, 1)
                    new_sl = max(new_sl, float(fill) + BE_OFFSET_R * risk0)
            else:
                trigger2 = float(fill) - SLPLUS_TRIGGER_R * risk0
                trigger1 = float(fill) - BE_TRIGGER_R * risk0
                new_stage = stage
                new_sl = active_sl
                if l <= trigger2:
                    new_stage = 2
                    new_sl = min(new_sl, float(fill) - SLPLUS_LOCK_R * risk0)
                elif l <= trigger1:
                    new_stage = max(new_stage, 1)
                    new_sl = min(new_sl, float(fill) - BE_OFFSET_R * risk0)

            if new_sl != active_sl:
                active_sl = round_tick(new_sl, "down" if side == "BUY" else "up", tick)
                stage = new_stage

        out["active_sl"] = float(active_sl)
        out["be_stage"] = int(stage)
        out["stop_mode"] = ("SL+" if stage >= 2 else ("BE+" if stage == 1 else "INITIAL"))

        if cap and held >= cap:
            out.update(status="TIMEOUT", exit=float(c), exit_time=iso); break

    out["active_sl"] = float(active_sl)
    out["be_stage"] = int(stage)
    out["stop_mode"] = ("SL+" if stage >= 2 else ("BE+" if stage == 1 else "INITIAL"))
    return out


class Book:
    def __init__(self, path=None):
        self.path, self.data = path, dict(trades=[])
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except Exception:
                pass
        # Migrasi data lama: versi sebelumnya bisa menyimpan trade tanpa field `status`.
        # Jangan biarkan satu record lama membuat bot crash saat startup.
        trades = self.data.get("trades", []) if isinstance(self.data, dict) else []
        migrated = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            t.setdefault("status", "PENDING")
            t.setdefault("initial_sl", t.get("sl"))
            t.setdefault("active_sl", t.get("initial_sl", t.get("sl")))
            t.setdefault("be_stage", 0)
            t.setdefault("stop_mode", "INITIAL")
            t.setdefault("fill", None)
            t.setdefault("fill_time", None)
            t.setdefault("exit", None)
            t.setdefault("exit_time", None)
            try:
                sig_time = pd.Timestamp(t.get("sig_time"))
            except Exception:
                # Record rusak/tidak punya timestamp: jangan mengganggu startup.
                continue
            migrated.append(t)

        cut = now_ts() - pd.Timedelta(days=14)                  # file tidak membengkak
        self.data["trades"] = [t for t in migrated
                               if t.get("status", "PENDING") in ACTIVE or pd.Timestamp(t["sig_time"]) > cut]

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, sym, p):
        t = dict(id=f"{sym}|{p['kind']}|{p['time'].isoformat()}", sym=sym, kind=p["kind"],
                 side=p.get("side","BUY"), order_type=p.get("order_type", "NEXT_OPEN"),
                 sig_time=p["time"].isoformat(), entry=float(p["entry"]), sl=float(p["sl"]),
                 initial_sl=float(p["sl"]), active_sl=float(p["sl"]), be_stage=0, stop_mode="INITIAL", tp=float(p["tp"]),
                 tick=float(p["tick"]), off=float(p.get("off", 0.0)), lot=float(p.get("lot", TRADE_LOT)),
                 status="PENDING", fill=None, fill_time=None, exit=None, exit_time=None)
        for key in ("sig_low","sig_high","invalidation","strict"):
            if key in p:
                t[key] = bool(p[key]) if key == "strict" else float(p[key])
        self.data["trades"].append(t)
        return t

    def n_active(self, sym):
        return sum(1 for t in self.data["trades"] if t["sym"] == sym and t["status"] in ACTIVE)

    def update(self, sym, raw):
        """Perbarui trade aktif satu simbol. Return trade yang baru kena TP/SL/TIMEOUT."""
        events = []
        for t in self.data["trades"]:
            if t["sym"] != sym or t["status"] not in ACTIVE:
                continue
            t.update(simulate(t, raw))
            if t["status"] in HIT:
                events.append(dict(t))
        return events


class CapitalTracker:
    """Track realized paper P/L in IDR from TP/SL/TIMEOUT events."""
    def __init__(self, path=None):
        self.path = path
        self.data = {
            "balance": float(CAPITAL_START),
            "initial_capital": float(CAPITAL_START),
            "realized_pnl_idr": 0.0,
            "topups_idr": 0.0,
            "topup_count": 0,
            "entries": 0,
            "closed": 0,
            "tp": 0,
            "sl": 0,
            "timeout": 0,
            "events": [],
            "last_weekly_recap": "",
        }
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except Exception:
                pass

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def register_entry(self):
        self.data["entries"] = int(self.data.get("entries", 0)) + 1

    def _units_per_lot(self, sym):
        return XAU_UNITS_PER_LOT if sym == "XAUUSD" else BTC_UNITS_PER_LOT

    def settle(self, trade):
        status = trade.get("status")
        if status not in HIT or not trade.get("fill") or trade.get("exit") is None:
            return None
        trade_id = trade.get("id") or f"{trade.get('sym')}|{trade.get('kind')}|{trade.get('sig_time')}"
        if any(e.get("id") == trade_id for e in self.data.get("events", [])):
            return None

        side = trade.get("side", "BUY")
        fill = float(trade["fill"])
        exit_px = float(trade["exit"])
        lot = float(trade.get("lot", TRADE_LOT))
        price_diff = (exit_px - fill) if side == "BUY" else (fill - exit_px)
        units = lot * self._units_per_lot(trade.get("sym", "XAUUSD"))
        pnl_usd = price_diff * units
        pnl_idr = round(pnl_usd * USD_IDR, 2)
        before = float(self.data.get("balance", CAPITAL_START))
        after = before + pnl_idr

        topup = 0.0
        if after <= 0 and CAPITAL_TOPUP > 0:
            topup = float(CAPITAL_TOPUP)
            after += topup
            self.data["topups_idr"] = float(self.data.get("topups_idr", 0.0)) + topup
            self.data["topup_count"] = int(self.data.get("topup_count", 0)) + 1

        self.data["balance"] = round(after, 2)
        self.data["realized_pnl_idr"] = round(float(self.data.get("realized_pnl_idr", 0.0)) + pnl_idr, 2)
        self.data["closed"] = int(self.data.get("closed", 0)) + 1
        if status == "TP": self.data["tp"] = int(self.data.get("tp", 0)) + 1
        elif status == "SL": self.data["sl"] = int(self.data.get("sl", 0)) + 1
        elif status == "TIMEOUT": self.data["timeout"] = int(self.data.get("timeout", 0)) + 1

        event = {
            "id": trade_id,
            "ts": trade.get("exit_time") or now_ts().isoformat(),
            "sym": trade.get("sym"),
            "kind": trade.get("kind"),
            "status": status,
            "side": side,
            "lot": lot,
            "pnl_usd": pnl_usd,
            "pnl_idr": pnl_idr,
            "balance_before": before,
            "balance_after": after,
            "topup_idr": topup,
        }
        self.data.setdefault("events", []).append(event)
        self.data["events"] = self.data["events"][-2000:]
        self.save()
        return event

    def summary_text(self):
        d = self.data
        closed = int(d.get("closed", 0))
        winrate = (int(d.get("tp", 0)) / closed * 100.0) if closed else 0.0
        def rp(v): return f"Rp {int(round(v)):,.0f}".replace(",", ".")
        return ("<b>CAPITAL PAPER TRACKER</b>\n"
                f"Modal awal  : {rp(d.get('initial_capital', CAPITAL_START))}\n"
                f"Saldo kini  : {rp(d.get('balance', CAPITAL_START))}\n"
                f"Realized P/L: {rp(d.get('realized_pnl_idr', 0))}\n"
                f"Entry selesai: {closed}  | TP {int(d.get('tp',0))} | SL {int(d.get('sl',0))} | TO {int(d.get('timeout',0))}\n"
                f"Winrate TP  : {winrate:.1f}%\n"
                f"Top-up      : {rp(d.get('topups_idr',0))} ({int(d.get('topup_count',0))}x)\n"
                f"Lot/entry   : {TRADE_LOT:.2f}")

    def weekly_recap(self, now=None):
        now = now or now_ts()
        # Previous Saturday 05:00 WIB -> current Saturday 05:00 WIB.
        dow = now.weekday()
        days_since_sat = (dow - 5) % 7
        this_sat = (now - pd.Timedelta(days=days_since_sat)).normalize() + pd.Timedelta(hours=WEEKLY_RECAP_HOUR, minutes=WEEKLY_RECAP_MINUTE)
        if now < this_sat:
            this_sat -= pd.Timedelta(days=7)
        prev_sat = this_sat - pd.Timedelta(days=7)
        events = []
        for e in self.data.get("events", []):
            try:
                ts = pd.Timestamp(e.get("ts"))
                if prev_sat <= ts < this_sat:
                    events.append(e)
            except Exception:
                pass
        events.sort(key=lambda x: x.get("ts", ""))
        total_pnl = sum(float(e.get("pnl_idr", 0)) for e in events)
        tp = sum(1 for e in events if e.get("status") == "TP")
        sl = sum(1 for e in events if e.get("status") == "SL")
        to = sum(1 for e in events if e.get("status") == "TIMEOUT")
        tops = sum(float(e.get("topup_idr",0)) for e in events)
        start_bal = float(events[0].get("balance_before", self.data.get("balance", CAPITAL_START))) if events else float(self.data.get("balance", CAPITAL_START))
        end_bal = float(self.data.get("balance", CAPITAL_START))
        closed = len(events)
        winrate = (tp / closed * 100.0) if closed else 0.0
        def rp(v): return f"Rp {int(round(v)):,.0f}".replace(",", ".")
        lines = [
            f"<b>REKAP MINGGUAN — {this_sat:%d %b %Y %H:%M} WIB</b>",
            f"Periode: {prev_sat:%d %b %H:%M} → {this_sat:%d %b %H:%M} WIB",
            "",
            f"Modal awal minggu : {rp(start_bal)}",
            f"Saldo akhir       : {rp(end_bal)}",
            f"P/L realized      : {rp(total_pnl)}",
            f"Trade selesai     : {closed}",
            f"TP / SL / Timeout : {tp} / {sl} / {to}",
            f"Winrate TP        : {winrate:.1f}%",
            f"Auto top-up       : {rp(tops)}",
            f"Ukuran entry      : {TRADE_LOT:.2f} lot",
        ]
        if events:
            lines.append("")
            lines.append("<pre>")
            for e in events[-12:]:
                ts = pd.Timestamp(e["ts"]).tz_convert(TZ) if pd.Timestamp(e["ts"]).tzinfo else pd.Timestamp(e["ts"]).tz_localize(TZ)
                lines.append(f"{ts:%d/%m %H:%M} {e.get('sym'):7} {e.get('kind'):2} {e.get('status'):7} {rp(e.get('pnl_idr',0)):>14}")
            lines.append("</pre>")
        return "\n".join(lines)


def maybe_send_weekly_recap(capital, book):
    now = now_ts()
    if now.weekday() != 5:
        return False
    if (now.hour, now.minute) < (WEEKLY_RECAP_HOUR, WEEKLY_RECAP_MINUTE):
        return False
    key = now.strftime("%Y-%m-%d")
    if capital.data.get("last_weekly_recap") == key:
        return False
    capital.data["last_weekly_recap"] = key
    capital.save()
    if tg_ready():
        thread = topic_id("EVENT")
        send_text(capital.weekly_recap(now), thread)
    else:
        print(_plain(capital.weekly_recap(now)))
    return True

def build_event(t, capital_event=None):
    d = dec_of(t["tick"])
    title = {"TP": ("\u2705", "TP HIT"), "SL": ("\u274C", "SL HIT"),
             "TIMEOUT": ("\u23F1", "KELUAR MAKS BAR")}.get(t["status"], ("\u26A0", t["status"]))
    labels = {"A":"Momentum Long", "B":"Legacy Shadow Fakeout", "C":"Accumulation → Expansion", "SF":"Shadow Fakeout 1M"}
    kind = labels.get(t["kind"], t["kind"])
    side = t.get("side","BUY")
    risk = abs(t["entry"] - t.get("initial_sl", t["sl"]))
    diff = (t["exit"] - t["fill"]) if side == "BUY" else (t["fill"] - t["exit"])
    st, et = pd.Timestamp(t["sig_time"]), pd.Timestamp(t["exit_time"])
    stop_mode = t.get("stop_mode", "INITIAL")
    rows = [f"Side  {side}", f"Entry {t['fill']:>10,.{d}f}", f"Exit  {t['exit']:>10,.{d}f}",
            f"Hasil {diff:>+10,.{d}f}  ({diff / max(risk,1e-12):+.2f}R)",
            f"Stop  {stop_mode}  @ {t.get('active_sl', t.get('sl')):>10,.{d}f}"]
    lines = [f"{title[0]} <b>{title[1]} — {t['sym']}</b> ({kind})",
             "<pre>" + "\n".join(rows) + "</pre>",
             f"Sinyal {st:%H:%M} → keluar {et:%d %b %H:%M} WIB"]
    if capital_event:
        pnl = float(capital_event.get("pnl_idr", 0.0))
        bal = float(capital_event.get("balance_after", 0.0))
        topup = float(capital_event.get("topup_idr", 0.0))
        rp = lambda v: f"Rp {int(round(v)):,.0f}".replace(",", ".")
        lines.append(f"Modal: {rp(bal)}  |  P/L: {rp(pnl)}" + (f"  | Top-up: {rp(topup)}" if topup else ""))
    return "\n".join(lines)


# ============================================================ STATE / SCAN
def _key_time(k):
    try:
        return pd.Timestamp(k.split("|", 2)[2])
    except Exception:
        return now_ts() - pd.Timedelta(days=99)


def load_sent():
    try:
        with open(SENT_FILE) as f:
            s = set(json.load(f))
    except Exception:
        s = set()
    cut = now_ts() - pd.Timedelta(days=3)
    return {k for k in s if _key_time(k) > cut}


def save_sent(s):
    with open(SENT_FILE, "w") as f:
        json.dump(sorted(s), f)


def kind_count(sent, sym, kind, ptime):
    day = (ptime - pd.Timedelta(hours=DAY_RESET_H)).date()
    return sum(1 for k in sent if k.startswith(f"{sym}|{kind}|")
               and (_key_time(k) - pd.Timedelta(hours=DAY_RESET_H)).date() == day)


def a_count(sent, sym, ptime):
    return kind_count(sent, sym, "A", ptime)


def handle_plan(sym, p, m, sent, book, capital, include_all):
    key = f"{sym}|{p['kind']}|{p['time'].isoformat()}"
    if key in sent:
        return False
    if not include_all and (now_ts() - p["time"]).total_seconds() / 60 > SIGNAL_MAX_AGE_MIN:
        return False
    if p["kind"] == "A" and a_count(sent, sym, p["time"]) >= A_MAX_PER_DAY:
        return False
    if p["kind"] == "C" and kind_count(sent, sym, "C", p["time"]) >= AE_MAX_PER_DAY:
        return False
    if p["kind"] == "SF" and SF_COOLDOWN_MIN > 0 and not include_all:
        last_sf = _last_sent_time(sent, sym, "SF")        # cooldown per simbol seperti xau-btc
        if last_sf is not None and (p["time"] - last_sf) < pd.Timedelta(minutes=SF_COOLDOWN_MIN):
            return False
    p["off"] = OFFSETS[sym]
    p["lot"] = TRADE_LOT
    d = p["dec"]
    path = os.path.join(CHART_DIR, f"{sym}_{p['kind']}_{p['time']:%Y%m%d_%H%M}.png")
    make_chart(m, p, sym, path)
    print(f"{sym}: [{p['kind']}] {p['time']:%H:%M} entry {p['entry']:,.{d}f} SL {p['sl']:,.{d}f} "
          f"TP {p['tp']:,.{d}f} [{p['status']}]")
    if tg_ready():
        tg_send_photo(path, build_caption(sym, p), p.get("kind"))
    else:
        print("   TELEGRAM env belum diset; chart disimpan:", path)
    sent.add(key); save_sent(sent)
    if not include_all and p["status"] == "PENDING":
        book.add(sym, p); book.save()
        capital.register_entry(); capital.save()
    return True


def scan_once(bars_n, include_all, sent, book, capital):
    os.makedirs(CHART_DIR, exist_ok=True)
    now = now_ts()
    st = dict(sig=0, ev=0, err=0)
    for sym in PAIRS:
        try:
            raw = get_raw(sym)
            if raw.empty:
                print(f"{sym}: data kosong"); continue
            if book.n_active(sym):
                for e in book.update(sym, raw):
                    print(f"{sym}: {e['status']} {e['fill']} -> {e['exit']}")
                    ce = capital.settle(e)
                    msg = build_event(e, ce)
                    send_text(msg, topic_id("EVENT"))
                    st["ev"] += 1
                book.save(); capital.save()
            if XAU_PAUSE and sym == "XAUUSD" and xau_closed(now) and not include_all:
                continue                                         # CFD emas tutup
            df = adj(raw, OFFSETS[sym])
            if len(df) < LOOKBACK + 5:
                continue
            if not include_all and now - df.index[-1] > pd.Timedelta(minutes=SIGNAL_MAX_AGE_MIN):
                print(f"{sym}: bar terakhir {df.index[-1]:%H:%M} sudah basi, dilewati"); continue
            tick = get_tick(sym)
            m = prepare(df)
            plans = []
            if A_ENABLED:
                plans.extend(find_momentum(m, tick))
            if B_ENABLED:
                plans.extend(find_fakeout(m, tick, bars_n, include_all))
            plans.extend(find_accum_expansion(m, tick, AE_LAST_BARS, include_all))
            plans.extend(find_shadow_fakeout_1m(m, sym, tick, SF_LAST_BARS, include_all))
            # Urutkan berdasarkan waktu agar kartu Telegram tidak lompat-lompat.
            plans.sort(key=lambda z: z["time"])
            for p in plans:
                st["sig"] += int(handle_plan(sym, p, m, sent, book, capital, include_all))
        except Exception as e:
            st["err"] += 1
            print(f"{sym}: error -> {e}")
    maybe_send_weekly_recap(capital, book)
    print(f"[{now_ts():%H:%M:%S}] scan {len(PAIRS)} pair, {st['sig']} sinyal baru, "
          f"{st['ev']} TP/SL, error {st['err']} | modal Rp{int(capital.data.get('balance',0)):,}")
    return st["sig"]


# ============================================================ PERINTAH TELEGRAM
HELP_TXT = ("<b>Admin commands</b>\n"
            "/status — harga + trade aktif\n"
            "/capital — modal tracker\n"
            "/rekap — rekap mingguan terbaru\n"
            "/offset — lihat offset\n"
            "/offset XAUUSD -4 — ubah offset\n"
            "/engines — status engine\n"
            "/topics — routing topic\n"
            "/whereami — chat/topic ID\n"
            "/help — bantuan")


def status_text(book):
    out = []
    for sym in PAIRS:
        try:
            raw = RAW.get(sym)
            if raw is None or raw.empty:
                raw = get_raw(sym)
            d, off, last = dec_of(get_tick(sym)), OFFSETS[sym], raw.iloc[-1]
            out.append(f"{sym}  {raw.index[-1]:%H:%M} WIB\n Bybit  {last['close']:>11,.{d}f}\n"
                       f" Offset {off:>+11g}\n CFD    {last['close'] + off:>11,.{d}f}\n"
                       f" Trade aktif {book.n_active(sym)}")
        except Exception as e:
            out.append(f"{sym}: gagal ambil data ({e})")
    return "<pre>" + "\n\n".join(out) + "</pre>"


def handle_command(text, thread, book, capital):
    parts = text.split()
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd == "/status":
        send_text(status_text(book) + "\n\n" + capital.summary_text(), thread)
    elif cmd == "/capital":
        send_text(capital.summary_text(), thread)
    elif cmd == "/rekap":
        send_text(capital.weekly_recap(now_ts()), topic_id("EVENT") or thread)
    elif cmd == "/offset":
        if len(args) >= 2:
            sym = args[0].upper()
            try:
                val = float(args[1].replace(",", "."))
            except ValueError:
                send_text("Angka offset tidak valid. Contoh: /offset XAUUSD -4", thread); return
            if sym not in OFFSETS:
                send_text("Simbol tidak dikenal. Pilih XAUUSD atau BTCUSD.", thread); return
            OFFSETS[sym] = val
            save_offsets()
            send_text(f"Offset {sym} = {val:+g} (harga CFD = harga Bybit {val:+g})", thread)
        else:
            send_text("Offset saat ini:\n" + "\n".join(f"{k} {v:+g}" for k, v in OFFSETS.items()), thread)
    elif cmd == "/engines":
        send_text((
            f"Engine: A={'ON' if A_ENABLED else 'OFF'} | B={'ON' if B_ENABLED else 'OFF'} | C={'ON' if AE_ENABLED else 'OFF'} | SF={'ON' if SF_ENABLED else 'OFF'}\n"
            f"AE TF={AE_TF_MINUTES}m score>={AE_MIN_SCORE} last={AE_LAST_BARS}\n"
            f"SF xau-btc range={SF_RANGE_LOOKBACK} setup={SF_SETUP_LOOKBACK} fallback={'ON' if SF_FALLBACK else 'OFF'} cooldown={SF_COOLDOWN_MIN}m\n"
            f"SF chart bars={SF_CHART_BARS} POC={'ON' if SF_CHART_POC else 'OFF'} setup={'ON' if SF_CHART_SETUP else 'OFF'}\n"
            f"BE/SL+={'ON' if BE_ENABLED else 'OFF'} | BE +{BE_TRIGGER_R:.2f}R→+{BE_OFFSET_R:.2f}R | SL+ +{SLPLUS_TRIGGER_R:.2f}R→+{SLPLUS_LOCK_R:.2f}R"
        ), thread)
    elif cmd == "/topics":
        labels = {"A":"MOMENTUM", "B":"LEGACY FAKEOUT", "C":"ACCUM→EXPANSION", "SF":"SHADOW FAKEOUT 1M", "EVENT":"EVENTS"}
        lines = []
        for k, label in labels.items():
            lines.append(f"{label}: {TOPIC_IDS.get(k) or 'FALLBACK CHAT UTAMA'}")
        send_text("<b>Telegram Topic Routing</b>\n<pre>" + "\n".join(lines) + "</pre>", thread)
    elif cmd == "/whereami":
        # Kirim perintah ini di topic yang sedang dibuka untuk membaca chat_id + message_thread_id.
        send_text(
            "<b>Telegram ID saat ini</b>\n"
            f"Chat ID: <code>{TG_CHAT}</code>\n"
            f"Topic ID: <code>{thread if thread is not None else 'MAIN CHAT / bukan topic'}</code>\n\n"
            "Masukkan Topic ID ini ke .env sesuai kategori topic.",
            thread
        )
    elif cmd in ("/help", "/start"):
        send_text(HELP_TXT, thread)


def _delete_update_message(msg):
    try:
        tg_post("deleteMessage", {"chat_id": TG_CHAT, "message_id": msg.get("message_id")})
    except Exception:
        pass

def process_update(u, book, capital):
    msg = u.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != TG_CHAT:         # hanya chat yang diizinkan
        return

    # Hapus service message "joined/left" agar grup tetap bersih.
    if msg.get("new_chat_members") or msg.get("left_chat_member"):
        _delete_update_message(msg)
        return

    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return

    uid = (msg.get("from") or {}).get("id")
    if not is_admin(uid):
        # Semua command member dihapus otomatis; tidak dibalas di grup.
        _delete_update_message(msg)
        return

    handle_command(text, msg.get("message_thread_id"), book, capital)


def command_listener(book, capital):
    """Long polling getUpdates di thread terpisah; loop scan tidak terganggu."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    offset = None
    print("Perintah Telegram aktif (ADMIN ONLY): /status /capital /rekap /offset /engines /topics /whereami /help")
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            r = requests.get(url, params=params, timeout=45)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                try:
                    process_update(u, book, capital)
                except Exception as e:
                    print("perintah error:", e)
        except Exception as e:
            print("listener error:", e)
            time.sleep(10)


# ============================================================ DEMO
def _ohlc(rng, n, drift, S=0.25, vol_sigma=2.2, t0="2026-10-07 14:00"):
    idx = pd.date_range(t0, periods=n, freq="1min", tz=TZ)
    close = 4000 + np.cumsum(rng.normal(drift * S, 3.0 * S, n))
    op = np.r_[close[0], close[:-1]] + rng.normal(0, 1.0 * S, n)
    hi = np.maximum(op, close) + np.abs(rng.normal(0, vol_sigma * S, n))
    lo = np.minimum(op, close) - np.abs(rng.normal(0, vol_sigma * S, n))
    q = lambda x: np.round(x / 0.05) * 0.05
    op, hi, lo, close = q(op), q(hi), q(lo), q(close)
    hi, lo = np.maximum(hi, np.maximum(op, close)), np.minimum(lo, np.minimum(op, close))
    return idx, op, hi, lo, close


def demo_fakeout(S=0.25):
    for seed in range(1, 800):
        rng = np.random.default_rng(seed)
        n = 140
        idx, op, hi, lo, close = _ohlc(rng, n - 1, 0.5, S)
        pl = lo[-LOOKBACK:].min()
        df = pd.DataFrame(dict(open=np.r_[op, pl + 5 * S], high=np.r_[hi, pl + 25 * S], low=np.r_[lo, pl - 10 * S],
                               close=np.r_[close, pl + 20 * S], volume=1000.0),
                          index=pd.date_range("2026-10-07 14:00", periods=n, freq="1min", tz=TZ))
        m = prepare(df)
        plans = find_fakeout(m, 0.01, last_bars=1)
        if plans:
            return m, plans[0]
    raise RuntimeError("demo B gagal")


def demo_momentum(S=0.25):
    for drift in (0.15, 0.25, 0.35, 0.5):
        for seed in range(1, 400):
            rng = np.random.default_rng(seed)
            n = 140
            idx, op, hi, lo, close = _ohlc(rng, n - 1, drift, S)
            vol = rng.uniform(800, 1200, n)
            last_o = close[-1]
            top = hi[-5:].max()
            df = pd.DataFrame(dict(open=np.r_[op, last_o], high=np.r_[hi, top + 10 * S],
                                   low=np.r_[lo, last_o - 2 * S], close=np.r_[close, top + 5 * S], volume=vol),
                              index=pd.date_range("2026-10-07 14:00", periods=n, freq="1min", tz=TZ))
            df.iloc[-1, df.columns.get_loc("volume")] = 4000.0
            m = prepare(df)
            plans = find_momentum(m, 0.01)
            if plans:
                return m, plans[0]
    raise RuntimeError("demo A gagal")


def demo_shadow(S=0.25):
    """Data sintetis setup strict: range 24 bar -> sweep shadow panjang di bawah range -> breakbody/BOS -> retest (BUY)."""
    q = lambda x: round(float(x) / 0.05) * 0.05
    for seed in range(1, 600):
        rng = np.random.default_rng(seed)
        n0 = 143
        idx, op, hi, lo, close = _ohlc(rng, n0, -0.10, S)
        rl = float(lo[-24:].min())
        k = n0 - 12
        hi[k] = max(hi[k], rl + 7.5)                     # lebarkan range agar RR >= 1.5
        vol = rng.uniform(800, 1200, n0 + 7)
        vol[n0], vol[n0 + 1] = 3200.0, 2400.0
        tail = [                                          # (open, high, low, close) relatif terhadap batas bawah range
            (0.35, 0.70, -0.90, 0.50),                    # sweep: shadow bawah panjang, close kembali di atas range
            (-0.10, 1.30, -0.20, 1.20),                   # breakbody/BOS: body close di atas range
            (1.20, 1.90, 0.90, 1.70),
            (1.70, 2.20, 1.20, 1.50),
            (1.50, 1.80, 0.90, 1.10),
            (1.10, 1.40, 0.85, 1.30),
            (0.70, 1.10, 0.30, 1.00),                     # retest: low menyentuh zona, close bertahan di atas
        ]
        o2, h2, l2, c2 = ([q(rl + t[j]) for t in tail] for j in range(4))
        df = pd.DataFrame(dict(open=np.r_[op, o2], high=np.r_[hi, h2], low=np.r_[lo, l2], close=np.r_[close, c2], volume=vol),
                          index=pd.date_range("2026-10-07 14:00", periods=n0 + 7, freq="1min", tz=TZ))
        m = prepare(df)
        plans = find_shadow_fakeout_1m(m, "XAUUSD", 0.01, last_bars=1)
        if plans and plans[0].get("strict"):
            return m, plans[0]
    raise RuntimeError("demo SF gagal")


def _plain(s):
    for tag in ("<b>", "</b>", "<pre>", "</pre>", "<i>", "</i>"):
        s = s.replace(tag, "")
    return s


def run_demo(send):
    os.makedirs(CHART_DIR, exist_ok=True)
    msgs = []
    for name, fn in (("DEMO_A_momentum", demo_momentum), ("DEMO_B_fakeout", demo_fakeout),
                     ("DEMO_SF_shadow_xaubtc", demo_shadow)):
        m, p = fn()
        p["off"] = OFFSETS["XAUUSD"]
        path = os.path.join(CHART_DIR, f"{name}.png")
        make_chart(m, p, "XAUUSD", path, demo=True)
        cap = build_caption("XAUUSD", p)
        print(f"--- {name} ---\n{_plain(cap)}\nChart: {path}\n")
        if send:
            if not tg_ready():
                sys.exit("Set TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID dulu.")
            tg_send_photo(path, cap, p.get("kind"))
    t0 = pd.Timestamp("2026-10-07 14:05", tz=TZ)
    ex = [dict(sym="XAUUSD", kind="A", sig_time=t0.isoformat(), entry=3999.5, sl=3988.0, tp=4016.75, tick=0.01,
               status="TP", fill=3999.5, exit=4016.75, exit_time=(t0 + pd.Timedelta(minutes=17)).isoformat()),
          dict(sym="BTCUSD", kind="B", sig_time=t0.isoformat(), entry=95210.5, sl=95010.0, tp=95611.5, tick=0.1,
               status="SL", fill=95210.5, exit=95010.0, exit_time=(t0 + pd.Timedelta(minutes=9)).isoformat())]
    for t in ex:
        msgs.append(build_event(t))
    for mm in msgs:
        print(_plain(mm), "\n")
        if send:
            send_text(mm)
    if send:
        print("Contoh terkirim ke Telegram.")


# ============================================================ SELF-TEST

def _selftest_make_shadow():
    n=140
    idx=pd.date_range("2026-10-07 08:00", periods=n, freq="1min", tz=TZ)
    o=np.full(n,4000.0); c=np.full(n,4000.0); h=np.full(n,4010.0); l=np.full(n,3990.0); v=np.full(n,1000.0)
    for i in range(n):
        o[i]=3999.8 if i%2 else 4000.2; c[i]=4000.2 if i%2 else 3999.8
    s=125
    o[s],c[s],h[s],l[s],v[s]=4000.2,4000.4,4000.6,3989.0,2500.0
    o[s+1],c[s+1],h[s+1],l[s+1],v[s+1]=3989.8,4000.7,4001.0,3989.5,1800.0
    for i in range(s+2,s+5): o[i],c[i],h[i],l[i]=4000.4,4000.8,4001.2,3999.8
    i=s+5
    o[i],c[i],h[i],l[i],v[i]=3991.0,3992.0,3994.0,3990.5,1900.0
    for i in range(s+6,n): o[i],c[i],h[i],l[i]=3995,3996,4000,3994
    return pd.DataFrame({"open":o,"high":h,"low":l,"close":c,"volume":v},index=idx)


def _selftest_make_ae():
    n=500
    idx=pd.date_range("2026-10-07 08:00", periods=n, freq="1min", tz=TZ)
    d=pd.DataFrame(index=idx, data={"open":4000.0,"high":4000.3,"low":3999.7,"close":4000.0,"volume":1000.0})
    for i in range(0,300):
        d.iloc[i]=[4000+(i%5)*0.01,4000.25+(i%3)*0.01,3999.75-(i%2)*0.01,4000+(i%5)*0.01,900]
    for i in range(300,480):
        base=4000+(i-300)*0.001
        d.iloc[i]=[base,base+0.2,base-0.1,base+0.15,1200]
    for i in range(480,490): d.iloc[i]=[4005,4008,4004.8,4007.5,5000]
    for i in range(490,495): d.iloc[i]=[4007.5,4015,4007,4014,6000]
    for i in range(495,500): d.iloc[i]=[4014,4015,4013,4014.5,1000]
    return d


def run_self_test():
    print("=== BYBIT COMBO SELF-TEST ===")
    results=[]
    # Momentum existing demo
    m,p=demo_momentum(); results.append(("A MOMENTUM","PASS",p["kind"]))
    # Legacy fakeout existing demo
    m,p=demo_fakeout(); results.append(("B LEGACY FAKEOUT","PASS",p["kind"]))
    # AE
    ae=find_accum_expansion(_selftest_make_ae(),0.01,last_bars=10)
    results.append(("C ACCUM→EXPANSION","PASS" if ae else "FAIL", ae[0]["ae_score"] if ae else "no candidate"))
    # SF xau-btc: engine strict + kartu detail + chart (PNG)
    sdf=_selftest_make_shadow()
    sf=find_shadow_fakeout_1m(sdf,"XAUUSD",0.01,last_bars=12)
    strict=[x for x in sf if x.get("strict") and x.get("xs") is not None]
    results.append(("SF SHADOW 1M","PASS" if strict else "FAIL",
                    strict[-1]["sf_mode"] if strict else "no candidate"))
    ok_cap = ok_png = False
    if strict:
        p = strict[-1]
        cap = build_caption("XAUUSD", p)
        ok_cap = all(s in cap for s in ("PDF Shadow Fakeout + Pattern/BOS + Retest | ", "Alasan lengkap:",
                                        "Konfirmasi: ZONE=", "Confluence: 5/13", "Analisis bukan jaminan profit"))
        png = xb_render_chart("XAUUSD", "1m", xb_candles_from_df(sdf.iloc[: p["i"] + 1]), p["xs"],
                              structure=p["xs_structure"], poc=True, setup=True)
        ok_png = png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 4000
    results.append(("SF CAPTION XAUBTC","PASS" if ok_cap else "FAIL","kartu detail"))
    results.append(("SF CHART XAUBTC","PASS" if ok_png else "FAIL","PNG 960x540 + POC"))
    # Generic sell tracker
    raw=_selftest_make_shadow().tail(20).copy(); sig=raw.index[0]
    t=dict(kind="SF",side="SELL",order_type="NEXT_OPEN",sig_time=sig,entry=4000,sl=4010,tp=3980,tick=.01,off=0)
    raw.iloc[1]=[4000,4002,3975,3980,1000]
    sim=simulate(t,raw); results.append(("SELL TRACKER","PASS" if sim["status"]=="TP" else "FAIL",sim["status"]))
    for r in results: print(f"{r[0]:24} {r[1]:5} {r[2]}")
    if any(r[1]=="FAIL" for r in results):
        raise RuntimeError("Self-test failed")
    print("SELF-TEST PASSED")


# ============================================================ CHECK + MAIN
def run_check():
    print(f"Base URL : {BASE}   kategori: {CATEGORY}")
    try:
        j = bybit_get("/v5/market/time", {})
        print("Koneksi  : OK, waktu server", pd.to_datetime(int(j["result"]["timeSecond"]), unit="s", utc=True)
              .tz_convert(TZ).strftime("%d %b %Y %H:%M:%S WIB"))
    except Exception as e:
        print("Koneksi  : GAGAL ->", e)
        print("Cek internet/VPN, atau coba BYBIT_BASE=https://api.bybit.com"); return
    for sym, bs in PAIRS.items():
        print(f"\n{sym}  (Bybit {bs})")
        try:
            lst = bybit_get("/v5/market/instruments-info", dict(category=CATEGORY, symbol=bs))["result"]["list"]
            if not lst:
                print("  TIDAK DITEMUKAN di kategori", CATEGORY, "- pair belum tersedia di domain ini?"); continue
            print(f"  status {lst[0].get('status')} | tick {lst[0]['priceFilter']['tickSize']}")
            raw = get_raw(sym)
            last, off = raw.iloc[-1], OFFSETS[sym]
            d = dec_of(get_tick(sym))
            print(f"  bar terakhir {raw.index[-1]:%d %b %H:%M} WIB | {len(raw)} bar ter-cache")
            print(f"  harga Bybit {last['close']:,.{d}f}  offset {off:+g}  -> harga CFD {last['close'] + off:,.{d}f}")
        except Exception as e:
            print("  GAGAL ->", e)


def run_loop(a, sent, book, capital):
    print("Mode loop aktif. Ctrl+C untuk berhenti.")
    if tg_ready():
        threading.Thread(target=command_listener, args=(book, capital), daemon=True).start()
    while True:
        try:
            scan_once(a.bars, False, sent, book, capital)
        except Exception as e:
            print("loop error:", e)
        time.sleep(60 - time.time() % 60 + 3)                    # +3 dtk agar bar sudah final


def main():
    ap = argparse.ArgumentParser(description="Bybit Combo: Momentum + Legacy Fakeout + Accumulation→Expansion + Shadow Fakeout 1M -> Telegram")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--demo", action="store_true")
    g.add_argument("--check", action="store_true", help="tes koneksi Bybit, pair, tick, harga + offset")
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--self-test", action="store_true", help="uji semua engine secara lokal tanpa Telegram")
    ap.add_argument("--send", action="store_true", help="dengan --demo: kirim ke Telegram")
    ap.add_argument("--bars", type=int, default=3, help="cek fakeout di N bar terakhir")
    ap.add_argument("--all", action="store_true", help="sertakan fakeout yang sudah terpicu/batal (tes)")
    a = ap.parse_args()
    load_offsets()
    if a.demo:
        run_demo(a.send); return
    if a.self_test:
        run_self_test(); return
    if a.check:
        run_check(); return
    sent, book, capital = load_sent(), Book(BOOK_FILE), CapitalTracker(CAPITAL_FILE)
    if a.once:
        print(f"Selesai. {scan_once(a.bars, a.all, sent, book, capital)} sinyal baru."); return
    run_loop(a, sent, book, capital)


if __name__ == "__main__":
    main()
