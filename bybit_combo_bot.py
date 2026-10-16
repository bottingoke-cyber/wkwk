#!/usr/bin/env python3
"""
Bybit Ichimoku Signal Bot - XAUUSD & BTCUSD (Bybit v5) -> Telegram + chart
Satu sinyal: ICHIMOKU M15 + FAKEOUT/AMD (atau BREAK S/R CLOUD) dengan order LIMIT bertingkat di tepi cloud.
Data: Bybit v5 REST kline (M15 untuk sinyal; 1m hanya untuk melacak limit/TP/SL paper; default api.bybit.id).
Pair Bybit XAUUSDT / BTCUSDT ditampilkan sebagai XAUUSD / BTCUSD. Harga Bybit digeser dengan OFFSET supaya sama dengan
harga CFD (mis. CFD 4000, Bybit 4004 -> offset -4).

Sinyal ICH:
  FAKEOUT (AMD) : akumulasi di satu sisi cloud -> manipulasi (poke ke tepi cloud lalu close kembali + shadow) -> distribusi.
  BREAK         : close menembus cloud dengan body kuat -> tepi cloud jadi S/R baru (flip S/R).
  Order : LIMIT bertingkat (default 3, jarak 0.25 ATR M15) mulai dari tepi cloud; SL di luar cloud / ekstrem sweep
          (XAU ~100 pip, batas 60-150; 1 pip = $0.10); TP1 = 1.2R dan TP2 = 2R dari limit pertama.
  Batal : limit dibatalkan bila TP tercapai tanpa terisi atau lewat ICH_EXPIRE_MIN (240 menit).
  Kartu : detail (limit, SL, TP, confluence x/9, cloud, AMD, alasan) + chart putih gaya xau-btc dengan Kumo (+26 bar),
          Tenkan/Kijun, ladder L1-Ln, SL, TP1/TP2, kotak akumulasi AMD, volume profile + POC.
Plus: notifikasi TP HIT / SL+ / BE+ / SL HIT / keluar max bar (per ladder digabung) + catatan limit dibatalkan, paper tracker
modal (IDR) dengan statistik jujur (/status), rekap mingguan, dan perintah Telegram (/status, /offset, /engines, /topics).

Env wajib:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (API publik Bybit tidak butuh key)
Env opsional (default):
    OFFSET_XAUUSD=-5  OFFSET_BTCUSD=0   offset harga ke CFD (bisa diubah live lewat /offset)
    TELEGRAM_TOPIC_ICHIMOKU=<topic id sinyal>   TELEGRAM_TOPIC_EVENTS=<topic id event TP/SL>   (kosong = chat utama)
    TELEGRAM_ADMIN_IDS=<user id, pisah koma>    hanya admin yang boleh memakai perintah
    DATA_DIR=/data      folder state (Railway: pasang Volume di /data)
    BYBIT_BASE=https://api.bybit.id     domain API (global: https://api.bybit.com)
    DAY_RESET_HOUR=7    jam WIB pergantian "hari" untuk batas sinyal harian
    XAU_PAUSE_WEEKEND=1 tidak kirim sinyal XAUUSD Sabtu 05:00 - Senin 05:00 WIB (CFD tutup)
    HISTORY_BARS=1000   bar 1m yang di-cache untuk melacak limit/TP/SL aktif
  Sinyal ICH:
    ICH_ENABLED=1 (0 = matikan)  ICH_TF_MINUTES=15  ICH_MODES=FAKEOUT,BREAK
    ICH_MIN_SCORE=5 (dari 9; 4 = lebih banyak sinyal)  ICH_RUNGS=3  ICH_STEP_ATR=0.25
    ICH_SL_MIN_PIPS=60  ICH_SL_MAX_PIPS=150  ICH_PIP_XAU=0.10   (BTC memakai batas ATR M15: ICH_SL_MIN_ATR / ICH_SL_MAX_ATR)
    ICH_TP1_R=1.2  ICH_TP2_R=2.0  ICH_TRACK_TP=1   (tracker paper memakai TP1; 2 = TP2)
    ICH_MAX_PER_DAY=4  ICH_COOLDOWN_MIN=60  ICH_ONE_AT_A_TIME=1  ICH_EXPIRE_MIN=240  ICH_MAX_HOLD_BARS=480  ICH_RUNG_SPLIT=1
    ICH_CHART_BARS=100  CHART_POC=1
  Modal paper (IDR): CAPITAL_START_IDR  CAPITAL_TOPUP_IDR  TRADE_LOT  USD_IDR
    PAPER_COST_XAU=0  PAPER_COST_BTC=0   biaya round-trip (spread+fee) USD per oz / per BTC yang dikurangkan dari P/L paper

Pakai:
    python bybit_combo_bot.py --check            # tes koneksi, pair, tick, harga + offset, bar M15
    python bybit_combo_bot.py --demo [--send]    # contoh chart + pesan (data sintetis)
    python bybit_combo_bot.py --self-test        # uji lokal engine ICH + chart + tracker
    python bybit_combo_bot.py --once             # scan sekali
    python bybit_combo_bot.py --loop             # live
Perintah Telegram (saat --loop): /status  /capital  /rekap  /offset  /offset XAUUSD -4  /engines  /topics  /whereami  /help

Catatan: paper tracker memakai aturan konservatif (SL dihitung lebih dulu bila TP dan SL tersentuh di bar 1m yang sama) dan belum
memodelkan spread/slippage kecuali PAPER_COST_* diisi. Hasil paper bukan jaminan hasil live; belum divalidasi backtest data nyata.
Sinyal Momentum / Legacy Fakeout / Accumulation->Expansion / Shadow Fakeout 1M sudah dihapus dari versi ini.
"""
import os, sys, json, math, time, argparse, threading, html, struct, zlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

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
    "ICH": os.getenv("TELEGRAM_TOPIC_ICHIMOKU", "").strip(),
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
HISTORY_BARS = max(300, int(_f("HISTORY_BARS", 1000)))   # bar 1m yang di-cache untuk melacak limit/TP/SL aktif (1000 = ~16 jam; ladder ICH maks ~12 jam)
ATR_PERIOD = 14
MAX_HOLD_BARS = 30                            # batas bar default tracker (hanya untuk sisa trade lama sinyal B yang masih aktif saat upgrade)
B_EXPIRE_BARS = 30                            # idem: kedaluwarsa BUY STOP lama (hanya sisa trade lama)

# --- Manajemen posisi: BE / SL+ otomatis (paper tracker; tidak menempatkan order live)
BE_ENABLED = os.getenv("BE_ENABLED", "1") != "0"
BE_TRIGGER_R = max(0.0, _f("BE_TRIGGER_R", 0.80))       # saat profit mencapai +0.80R
BE_OFFSET_R = max(0.0, _f("BE_OFFSET_R", 0.05))         # kunci +0.05R (BE+)
SLPLUS_TRIGGER_R = max(BE_TRIGGER_R, _f("SLPLUS_TRIGGER_R", 1.20))  # naikkan SL lagi
SLPLUS_LOCK_R = max(0.0, _f("SLPLUS_LOCK_R", 0.30))    # kunci +0.30R

# --- Biaya paper + batas tracker ICH
PAPER_COST_XAU = max(0.0, _f("PAPER_COST_XAU", 0.0))        # biaya round-trip (spread+fee) USD per oz untuk P/L paper
PAPER_COST_BTC = max(0.0, _f("PAPER_COST_BTC", 0.0))        # biaya round-trip USD per BTC
ICH_EXPIRE_MIN = max(1, int(_f("ICH_EXPIRE_MIN", 240)))     # ICH: limit kedaluwarsa bila belum terisi (menit)
ICH_MAX_HOLD_BARS = max(0, int(_f("ICH_MAX_HOLD_BARS", 480)))  # ICH: batas bar 1m setelah terisi (480 = 8 jam)

# --- Sinyal ICH: Ichimoku M15 + Fakeout/AMD, order LIMIT bertingkat di cloud (S/R)
ICH_ENABLED = os.getenv("ICH_ENABLED", "1") != "0"
ICH_TF_MINUTES = int(_f("ICH_TF_MINUTES", 15)) if int(_f("ICH_TF_MINUTES", 15)) in (5, 15, 30, 60) else 15
ICH_HISTORY_BARS = max(200, int(_f("ICH_HISTORY_BARS", 400)))
ICH_LAST_BARS = max(1, int(_f("ICH_LAST_BARS", 2)))              # cek N bar M15 terakhir
ICH_MAX_AGE_MIN = max(5, int(_f("ICH_MAX_AGE_MIN", 14)))         # sinyal M15 boleh telat <= 14 menit setelah bar close
ICH_MODES = tuple(x_ for x_ in os.getenv("ICH_MODES", "FAKEOUT,BREAK").upper().replace(" ", "").split(",")
                  if x_ in ("FAKEOUT", "BREAK")) or ("FAKEOUT", "BREAK")
ICH_CTX_BARS = max(4, int(_f("ICH_CTX_BARS", 8)))               # konteks: harga di sisi awan yang sama
ICH_CTX_MIN = max(1, min(ICH_CTX_BARS, int(_f("ICH_CTX_MIN", 4))))
ICH_WICK_MIN = min(0.9, max(0.1, _f("ICH_WICK_MIN", 0.35)))      # shadow penolakan >= 35% range candle
ICH_WICK_ATR = max(0.0, _f("ICH_WICK_ATR", 0.40))                # dan >= 0.4 ATR
ICH_ACCUM_BARS = max(5, int(_f("ICH_ACCUM_BARS", 10)))           # range akumulasi sebelum manipulasi
ICH_ACCUM_ATR = max(1.0, _f("ICH_ACCUM_ATR", 4.0))
ICH_MIN_SCORE = max(1, min(9, int(_f("ICH_MIN_SCORE", 5))))      # minimal confluence (dari 9); 4 = lebih banyak sinyal
ICH_RUNGS = max(1, min(6, int(_f("ICH_RUNGS", 3))))              # jumlah limit bertingkat
ICH_STEP_ATR = max(0.05, _f("ICH_STEP_ATR", 0.25))               # jarak antar limit = 0.25 ATR M15
ICH_PIP_XAU = max(1e-6, _f("ICH_PIP_XAU", 0.10))                 # 1 pip emas = $0.10 (100 pip = $10)
ICH_SL_MIN_PIPS = max(1.0, _f("ICH_SL_MIN_PIPS", 60.0))          # SL dari limit 1: 60-150 pip (target ~100)
ICH_SL_MAX_PIPS = max(ICH_SL_MIN_PIPS, _f("ICH_SL_MAX_PIPS", 150.0))
ICH_SL_MIN_ATR = max(0.1, _f("ICH_SL_MIN_ATR", 0.8))             # BTC: batas SL dalam ATR M15
ICH_SL_MAX_ATR = max(ICH_SL_MIN_ATR, _f("ICH_SL_MAX_ATR", 2.5))
ICH_SL_BUFFER_ATR = max(0.0, _f("ICH_SL_BUFFER_ATR", 0.15))
ICH_TP1_R = max(0.5, _f("ICH_TP1_R", 1.2))
ICH_TP2_R = max(ICH_TP1_R, _f("ICH_TP2_R", 2.0))
ICH_TRACK_TP = 2 if int(_f("ICH_TRACK_TP", 1)) == 2 else 1       # TP yang dipakai tracker paper (1 atau 2)
ICH_MAX_PER_DAY = int(_f("ICH_MAX_PER_DAY", 4))
ICH_COOLDOWN_MIN = max(0, int(_f("ICH_COOLDOWN_MIN", 60)))
ICH_ONE_AT_A_TIME = os.getenv("ICH_ONE_AT_A_TIME", "1") != "0"   # tidak ada ladder baru selama ladder lama masih aktif
ICH_RUNG_SPLIT = os.getenv("ICH_RUNG_SPLIT", "1") != "0"         # lot total dibagi rata ke semua rung
ICH_CHART_BARS = max(40, int(_f("ICH_CHART_BARS", 100)))
CHART_POC = os.getenv("CHART_POC", os.getenv("SF_CHART_POC", "1")) != "0"          # sorot POC volume profile (bar + garis + label)


# ============================================================ DATA (Bybit v5)
def now_ts():
    return pd.Timestamp.now(tz=TZ)


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


def parse_klines(rows, server_ms, bar_ms=60_000):
    """Bybit: [startMs, o, h, l, c, volume, turnover], urutan terbaru dulu."""
    if not rows:
        return EMPTY.copy(), None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    oldest = int(df["ts"].min())
    df = df[df["ts"] + bar_ms <= server_ms].dropna()               # buang bar yang masih berjalan
    if df.empty:
        return EMPTY.copy(), oldest
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(TZ)
    df = df.drop_duplicates("dt").sort_values("dt").set_index("dt")
    return df[["open", "high", "low", "close", "volume"]], oldest


def fetch_klines(sym, limit=200, end_ms=None, interval="1"):
    p = dict(category=CATEGORY, symbol=PAIRS[sym], interval=str(interval), limit=limit)
    if end_ms:
        p["end"] = end_ms
    j = bybit_get("/v5/market/kline", p)
    return parse_klines(j["result"]["list"], int(j.get("time") or time.time() * 1000), int(interval) * 60_000)


def fetch_history(sym, n=HISTORY_BARS, interval="1"):
    frames, end, got = [], None, 0
    for _ in range(10):
        df, oldest = fetch_klines(sym, 1000, end, interval)
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


RAW_TF = {}     # (sym, menit) -> bar TF tinggi (M15) mentah dari Bybit, tanpa offset


def get_raw_tf(sym, minutes=None):
    """Bar M15 (atau TF ICH) ter-cache. Fetch ulang hanya bila bar tertutup baru sudah mungkin ada (hemat rate limit)."""
    minutes = int(minutes or ICH_TF_MINUTES)
    key = (sym, minutes)
    c = RAW_TF.get(key)
    bar = pd.Timedelta(minutes=minutes)
    age = (now_ts() - c.index[-1]) if (c is not None and not c.empty) else None
    if age is None or age > bar * 40:
        df = fetch_history(sym, ICH_HISTORY_BARS, minutes)
    elif age >= bar * 2:
        new, _ = fetch_klines(sym, 60, None, minutes)
        df = pd.concat([c, new])
        df = df[~df.index.duplicated(keep="last")].sort_index().iloc[-(ICH_HISTORY_BARS + 60):]
    else:
        return c
    RAW_TF[key] = df
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


# ============================================================ RENDERER CHART GAYA XAU-BTC (port setia bot1.py: render_chart)
# Port dari bot1.py (xau-btc-nasdaq-termux-bot-final.zip):
#   chart  : render_chart   (putih 960x540: zona SNR, trendline pivot, EMA21, kotak Entry/SL/TP, volume profile + POC,
#            histogram volume, trend-band, harmonic XABCD) — murni stdlib (zlib/struct), tanpa Pillow/matplotlib.
# Semua helper berawalan xb_/XB agar mudah dibandingkan baris-per-baris dengan bot1.py.
# Beda yang disengaja:
#   * candle bot ini sudah CLOSED semua (parse_klines membuang bar berjalan) -> tidak ada lagi pemotongan candle
#     terakhir seperti di bot1.py (candles[:-1]); harga sudah ber-offset CFD.
#   * spasi pada label chart ikut tampil (di bot1.py spasi hilang), label harga menyesuaikan lebar untuk BTC.
#   * tambahan: POC volume profile (bar + garis + label) dan overlay Ichimoku (Kumo, Tenkan/Kijun, ladder limit, kotak AMD).

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
    score_max: int = 13                              # penyebut skor di caption (ICH memakai ICH_SCORE_MAX = 9)

    @property
    def risk(self):
        return abs(self.entry - self.stop_loss)


XB_NY = "America/New_York"


def _xb_local(ts, tz_name):
    return pd.Timestamp(int(ts), unit="s", tz="UTC").tz_convert(tz_name)


def xb_fmt_time(ts, tz_name=TZ):
    return _xb_local(ts, tz_name).strftime("%d-%m-%Y %H:%M")


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


def _xb_finite(v):
    return isinstance(v, (int, float)) and math.isfinite(v)


def _xb_spread(ys, gap, lo, hi):
    """Posisi y label (urutan asli) tanpa tumpang tindih: jarak >= gap, klaster dipusatkan pada y aslinya."""
    n = len(ys)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda k: ys[k])
    pos = [float(ys[k]) for k in order]
    for _ in range(60):
        moved = False
        for a in range(n - 1):
            d = pos[a + 1] - pos[a]
            if d < gap - 1e-9:
                push = (gap - d) / 2.0
                pos[a] -= push
                pos[a + 1] += push
                moved = True
        pos = [min(max(p, lo), hi) for p in pos]
        if not moved:
            break
    for a in range(1, n):                                        # sapuan akhir (clamp bisa memunculkan tumpukan lagi)
        pos[a] = max(pos[a], pos[a - 1] + gap)
    for a in range(n - 2, -1, -1):
        pos[a] = min(pos[a], pos[a + 1] - gap)
    out = [0.0] * n
    for rank, k in enumerate(order):
        out[k] = pos[rank]
    return out


def xb_render_chart(symbol, timeframe, candles, signal=None, structure=None,
                    poc=True, bars=120, demo=False, overlay=None):
    """Chart putih 960x540 (port render_chart bot1.py): header status, candle, EMA21, zona supply/demand, trendline
    pivot, kotak Entry/SL/TP, volume profile + POC, histogram volume, trend-band, tanggal-jam, harmonic XABCD.

    `candles` = candle closed sampai (dan termasuk) candle sinyal. `poc` menyorot bin volume terbesar (bar + garis +
    label).
    Chart hanya memvisualkan sinyal yang sudah dihitung; tidak mengubah entry, SL, TP, atau RR.

    `overlay` (opsional, tambahan Bybit; None = keluaran identik xau-btc): dict dengan kunci
      future : jumlah bar kosong di kanan (untuk awan Ichimoku masa depan)
      bands  : [{a, b, up, down}]  isi antara dua seri (panjang = candle + future), warna `up` bila a >= b
      lines  : [{v, color, width}] garis indikator (panjang = candle)
      hlines : [{price, name, color, style}] level horizontal berlabel (ladder limit, TP1, dst.)
      marks  : [{ts, text, color, use_high, above}] penanda pada candle
      boxes  : [{ts_from, ts_to, low, high, fill, line, name}] kotak range (mis. akumulasi AMD)
      labels : {entry, sl, tp} nama label pengganti ENTRY / SL / TP
    """
    canvas = XBCanvas(width=960, height=540, color=(255, 255, 255))
    analysis_candles = list(candles)
    visible = analysis_candles[-int(bars):]
    if not visible:
        return canvas.png()
    structure = structure or xb_market_structure(analysis_candles)
    ov = overlay or {}                                           # lapisan tambahan (Kumo, ladder, penanda) — opsional
    future = max(0, int(ov.get("future", 0) or 0))               # bar kosong di kanan (awan masa depan)
    vis0 = len(analysis_candles) - len(visible)
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
    for hl in ov.get("hlines", []):
        levels.append(float(hl["price"]))
    for bx in ov.get("boxes", []):
        levels.extend([float(bx["low"]), float(bx["high"])])
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
        return left + int(index * (plot_right - left) / max(1, len(visible) - 1 + future))

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

    # --- overlay (opsional): Kumo, kotak range, helper garis terpotong ke area plot.
    ov_index = {item.timestamp: n for n, item in enumerate(visible)}

    def seg(x1, y1, x2, y2, color, width=1):
        """Garis yang dipotong ke area plot (top..bottom) agar tidak menimpa panel volume."""
        ox1, oy1, ox2, oy2 = x1, y1, x2, y2
        if (oy1 < top and oy2 < top) or (oy1 > bottom and oy2 > bottom):
            return
        if oy1 != oy2:
            def at(yc):
                return int(round(ox1 + (ox2 - ox1) * (yc - oy1) / (oy2 - oy1)))
            if oy1 < top:
                x1, y1 = at(top), top
            elif oy1 > bottom:
                x1, y1 = at(bottom), bottom
            if oy2 < top:
                x2, y2 = at(top), top
            elif oy2 > bottom:
                x2, y2 = at(bottom), bottom
        canvas.line(x1, y1, x2, y2, color, width)

    def vis_series(arr, extra=0):
        return list(arr)[vis0: vis0 + len(visible) + extra]

    for band in ov.get("bands", []):                             # Kumo: isi antara dua seri; warna menurut sisi atas
        sa, sb = vis_series(band["a"], future), vis_series(band["b"], future)
        for k in range(min(len(sa), len(sb)) - 1):
            a0, a1, b0, b1 = sa[k], sa[k + 1], sb[k], sb[k + 1]
            if not all(_xb_finite(z) for z in (a0, a1, b0, b1)):
                continue
            xa, xb2 = x_at(k), x_at(k + 1)
            for xp in range(xa, xb2 + 1):
                frac = (xp - xa) / max(1, xb2 - xa)
                av, bv = a0 + (a1 - a0) * frac, b0 + (b1 - b0) * frac
                y1, y2 = sorted((y_at(av), y_at(bv)))
                y1, y2 = max(top, y1), min(bottom, y2)
                if y1 <= y2:
                    canvas.rect(xp, y1, xp, y2, band["up"] if av >= bv else band["down"])
    box_labels = []
    for bx in ov.get("boxes", []):                               # kotak range (mis. akumulasi AMD)
        i0 = ov_index.get(bx.get("ts_from"), 0)
        i1 = ov_index.get(bx.get("ts_to"), len(visible) - 1)
        bx1, bx2 = x_at(i0), x_at(i1)
        by1, by2 = sorted((y_at(float(bx["high"])), y_at(float(bx["low"]))))
        by1, by2 = max(top, by1), min(bottom, by2)
        if by1 <= by2:
            canvas.rect(bx1, by1, bx2, by2, bx.get("fill", (255, 246, 214)))
            box_labels.append((bx1, bx2, by1, by2, bx.get("line", (200, 170, 60)), bx.get("name", "")))

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
    for ln in ov.get("lines", []):                               # Tenkan / Kijun / Span A / Span B / VWAP / EMA, dst.
        vals = vis_series(ln["v"])
        for k in range(1, len(vals)):
            if _xb_finite(vals[k - 1]) and _xb_finite(vals[k]):
                seg(x_at(k - 1), y_at(vals[k - 1]), x_at(k), y_at(vals[k]), ln["color"], ln.get("width", 1))
    candle_width = max(2, int((plot_right - left) / max(len(visible) + future, 1) * 0.55))
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
        if ov.get("hlines") or ov.get("labels"):
            lab = ov.get("labels") or {}
            items = [(y_at(signal.entry), f"{lab.get('entry', 'ENTRY')} {signal.entry:.2f}", (35, 35, 35)),
                     (y_at(signal.stop_loss), f"{lab.get('sl', 'SL')} {signal.stop_loss:.2f}", (210, 40, 55)),
                     (y_at(display_tp), f"{lab.get('tp', 'TP')} {display_tp:.2f}", (20, 145, 75))]
            for hl in ov.get("hlines", []):
                yy = y_at(float(hl["price"]))
                if top <= yy <= bottom:
                    {"dotted": canvas.dotted}.get(hl.get("style", "dashed"), canvas.dashed)(left, yy, plot_right, hl["color"])
                    items.append((yy, f"{hl.get('name', '')} {float(hl['price']):.2f}", hl["color"]))
            label_ys = _xb_spread([it[0] - 7 for it in items], 12, top + 2, bottom - 10)   # jarak antar label >= 12 px
            for (yy, text, color), ly in zip(items, label_ys):
                width_px = 7 * len(text.replace(",", ""))
                canvas.label(max(left + 8, plot_right - 12 - width_px), int(round(ly)), text, color)
        else:
            canvas.label(label_x, y_at(signal.entry) - 7, "ENTRY", (35, 35, 35))
            canvas.label(label_x, y_at(signal.stop_loss) - 7, "SL", (210, 40, 55))
            canvas.label(label_x, y_at(display_tp) - 7, "TP", (20, 145, 75))

    for mk in ov.get("marks", []):
        idx = ov_index.get(mk.get("ts"))
        if idx is None:
            continue
        mcandle = visible[idx]
        mpx, mpy = x_at(idx), y_at(mcandle.high if mk.get("use_high", True) else mcandle.low)
        canvas.circle(mpx, mpy, 4, mk["color"])
        mtext = mk["text"]
        mly = mpy - 16 if mk.get("above", True) else mpy + 8
        mly = max(top + 2, min(mly, bottom - 10))
        mlx = max(left + 2, min(mpx - (7 * len(mtext)) // 2, plot_right - 7 * len(mtext) - 2))
        canvas.label(mlx, mly, mtext, mk["color"])
    for bx1, bx2, by1, by2, line_c, name in box_labels:          # garis tepi kotak di atas zona Entry/SL/TP agar tetap terlihat
        canvas.dotted(bx1, by1, bx2, line_c)
        canvas.dotted(bx1, by2, bx2, line_c)
        canvas.line(bx1, by1, bx1, by2, line_c)
        canvas.line(bx2, by1, bx2, by2, line_c)
        if name:
            canvas.label(bx1 + 4, by1 + 4, name, line_c)

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
    legend = [(ln.get("name"), ln["color"]) for ln in ov.get("lines", []) if ln.get("name")]
    legend += [(bd.get("name"), bd.get("legend", (150, 150, 150))) for bd in ov.get("bands", []) if bd.get("name")]
    lgx = left + 4 + 7 * len(status) + 14
    for name, color in legend:                                   # legenda overlay: garis kecil + nama
        canvas.line(lgx, top + 8, lgx + 10, top + 8, color, 2)
        canvas.label(lgx + 16, top + 4, name, color)
        lgx += 16 + 7 * len(name) + 12
    # Tanggal-jam di bawah panel trend.
    date_step = max(1, len(visible) // 6)
    for index in range((date_step // 2) if ov else 0, len(visible), date_step):
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


def make_chart_xb(plan, ticker, path, demo=False):
    """Chart xau-btc (putih, volume profile + POC, zona, trendline, kotak Entry/SL/TP) + Kumo/ladder/AMD untuk sinyal ICH."""
    png = xb_render_chart(ticker, plan.get("xs_tf", "M15"), plan["xs_candles"], plan["xs"], structure=plan.get("xs_structure"),
                          poc=CHART_POC, bars=plan.get("xs_bars", ICH_CHART_BARS), demo=demo,
                          overlay=plan.get("xs_overlay"))
    with open(path, "wb") as f:
        f.write(png)
    return path


def _last_sent_time(sent, sym, kind):
    """Waktu sinyal terakhir (kind) yang sudah terkirim untuk simbol ini, dari key `sent` (sym|kind|iso-time)."""
    times = [_key_time(k) for k in sent if k.startswith(f"{sym}|{kind}|")]
    return max(times) if times else None


# ============================================================ SINYAL ICH: ICHIMOKU M15 + FAKEOUT/AMD (LIMIT DI CLOUD)
# Fokus M15. Awan Ichimoku (Kumo) = S/R dinamis. Order = LIMIT bertingkat (ladder) di tepi awan sampai ke dalam awan,
# SL di luar awan / ekstrem sweep (~100 pip emas), TP1/TP2 = kelipatan R dari limit pertama.
#   FAKEOUT (AMD) : akumulasi di satu sisi awan -> manipulasi (high/low menembus tepi awan lalu CLOSE kembali + shadow)
#                   -> distribusi berlawanan arah. Limit di tepi awan = retest area manipulasi.
#   BREAK         : close menembus awan (S/R break) dengan body kuat -> limit di tepi awan yang ditembus (flip S/R).
# Semua kondisi hanya memakai data bar <= t (awan di bar t dihitung dari bar t-26), jadi sinyal tidak berubah saat
# data baru datang. Konvensi pip emas: 1 pip = $0.10 (100 pip = $10); bisa diubah lewat ICH_PIP_XAU.
ICH_SCORE_MAX = 9


def ich_frame(d):
    """Ichimoku 9/26/52 pada frame OHLC closed. Awan di bar t memakai data bar t-26 (tanpa look-ahead);
    a_raw/b_raw disimpan untuk menggambar awan 26 bar ke depan."""
    h, l, c = d["high"].astype(float), d["low"].astype(float), d["close"].astype(float)
    x = pd.DataFrame(index=d.index)
    x["tenkan"] = (h.rolling(9).max() + l.rolling(9).min()) / 2.0
    x["kijun"] = (h.rolling(26).max() + l.rolling(26).min()) / 2.0
    x["a_raw"] = (x["tenkan"] + x["kijun"]) / 2.0
    x["b_raw"] = (h.rolling(52).max() + l.rolling(52).min()) / 2.0
    x["span_a"] = x["a_raw"].shift(26)
    x["span_b"] = x["b_raw"].shift(26)
    x["cloud_top"] = x[["span_a", "span_b"]].max(axis=1, skipna=False)
    x["cloud_bot"] = x[["span_a", "span_b"]].min(axis=1, skipna=False)
    x["atr"] = rma(true_range(d, c.shift(1)), ATR_PERIOD)
    return x


def _ich_arrays(d, x):
    A = {k: d[k].to_numpy(float) for k in ("open", "high", "low", "close")}
    A["vol"] = np.nan_to_num(d["volume"].to_numpy(float)) if "volume" in d.columns else np.zeros(len(d))
    for k in ("tenkan", "kijun", "span_a", "span_b", "cloud_top", "cloud_bot", "atr"):
        A[k] = x[k].to_numpy(float)
    top, bot, c = A["cloud_top"], A["cloud_bot"], A["close"]
    pos = np.zeros(len(d), dtype=int)
    ok = ~(np.isnan(top) | np.isnan(bot))
    pos[ok & (c > top)] = 1
    pos[ok & (c < bot)] = -1
    A["pos"] = pos
    return A


def _ich_sl_bounds(sym, atr):
    """Batas jarak SL dari limit pertama: emas dalam pip (default 60-150 pip = $6-$15), BTC dalam kelipatan ATR M15."""
    if sym == "XAUUSD":
        return ICH_SL_MIN_PIPS * ICH_PIP_XAU, ICH_SL_MAX_PIPS * ICH_PIP_XAU
    return ICH_SL_MIN_ATR * atr, ICH_SL_MAX_ATR * atr


def _ich_core(A, t, sell, mode):
    """Kondisi inti. Return (edge, far, ext) atau None. edge = tepi awan terdekat (limit 1), far = tepi jauh,
    ext = ekstrem manipulasi (high untuk SELL / low untuk BUY)."""
    o, h, l, c = (A[k][t] for k in ("open", "high", "low", "close"))
    atr, top, bot = A["atr"][t], A["cloud_top"][t], A["cloud_bot"][t]
    if not (atr > 0) or np.isnan(top) or np.isnan(bot) or t < ICH_CTX_BARS + 1:
        return None
    pos = A["pos"]
    rng = h - l
    if rng <= 0:
        return None
    body = abs(c - o)
    if mode == "FAKEOUT":
        side_pos = -1 if sell else 1
        if int((pos[t - ICH_CTX_BARS:t] == side_pos).sum()) < ICH_CTX_MIN:       # harga sebelumnya di sisi awan yang sama
            return None
        if sell:
            if pos[t - 1] > 0 or not (h > bot and c < bot):                      # menembus tepi bawah awan lalu close kembali di bawah
                return None
            wick = h - max(o, c)
            edge, far, ext = bot, top, h
        else:
            if pos[t - 1] < 0 or not (l < top and c > top):
                return None
            wick = min(o, c) - l
            edge, far, ext = top, bot, l
        if wick < ICH_WICK_MIN * rng or wick < ICH_WICK_ATR * atr:               # shadow penolakan nyata
            return None
        return float(edge), float(far), float(ext)
    if sell:                                                                       # BREAK
        if not (pos[t] == -1 and pos[t - 1] >= 0 and c < o and (bot - c) >= 0.15 * atr and body >= 0.5 * rng):
            return None
        return float(bot), float(top), float(top)
    if not (pos[t] == 1 and pos[t - 1] <= 0 and c > o and (c - top) >= 0.15 * atr and body >= 0.5 * rng):
        return None
    return float(top), float(bot), float(bot)


def _ich_conf(A, t, sell, mode, far):
    """Daftar confluence yang terpenuhi di bar t (maks ICH_SCORE_MAX = 9)."""
    o, h, l, c, v = (A[k][t] for k in ("open", "high", "low", "close", "vol"))
    atr = A["atr"][t]
    rng = max(h - l, 1e-12)
    out = []
    n = ICH_ACCUM_BARS
    ph, pl = A["high"][t - n:t], A["low"][t - n:t]
    if mode == "FAKEOUT":
        if (h > ph.max()) if sell else (l < pl.min()):
            out.append("sweep liquidity " + ("di atas" if sell else "di bawah") + " range akumulasi")
        if (ph.max() - pl.min()) <= ICH_ACCUM_ATR * atr:
            out.append(f"akumulasi rapat ({n} bar)")
        if (h > far) if sell else (l < far):
            out.append("manipulasi menembus seluruh awan")
        if ((c - l) / rng <= 0.35) if sell else ((c - l) / rng >= 0.65):
            out.append("close dekat ujung berlawanan sweep")
    else:
        came = A["pos"][max(0, t - 6):t]
        if (came == (1 if sell else -1)).any():
            out.append("tembus awan penuh dari sisi seberang")
        if abs(c - o) >= 0.7 * rng:
            out.append("body dominan (>=70%)")
        if abs(c - o) >= 1.0 * atr:
            out.append("displacement >= 1 ATR")
        if ((c - l) / rng <= 0.25) if sell else ((c - l) / rng >= 0.75):
            out.append("close di ujung candle")
    tk, kj = A["tenkan"], A["kijun"]
    if not (np.isnan(tk[t]) or np.isnan(kj[t])):
        if (tk[t] < kj[t]) if sell else (tk[t] > kj[t]):
            out.append("Tenkan/Kijun searah")
        if t >= 3 and not np.isnan(kj[t - 3]) and ((kj[t] < kj[t - 3]) if sell else (kj[t] > kj[t - 3])):
            out.append("Kijun miring searah")
    if t >= 26 and ((c < A["close"][t - 26]) if sell else (c > A["close"][t - 26])):
        out.append("Chikou searah")
    sa, sb = A["span_a"][t], A["span_b"][t]
    if not (np.isnan(sa) or np.isnan(sb)) and ((sa < sb) if sell else (sa > sb)):
        out.append("awan searah")
    vm = float(A["vol"][t - 20:t].mean()) if t >= 20 else 0.0
    if vm > 0 and v >= 1.2 * vm:
        out.append("volume lonjakan")
    return out


def _ich_ladder(sym, sell, mode, edge, far, ext, atr, tick):
    """Ladder limit di tepi awan ke dalam awan + SL + TP. Return (dict, None) atau (None, alasan)."""
    step = max(ICH_STEP_ATR * atr, tick)
    if sell:
        lim = min(far + 0.5 * step, ext) if mode == "FAKEOUT" else far + 0.5 * step
    else:
        lim = max(far - 0.5 * step, ext) if mode == "FAKEOUT" else far - 0.5 * step
    levels = []
    for k in range(max(1, ICH_RUNGS)):
        px = edge + k * step if sell else edge - k * step
        if k and ((px > lim) if sell else (px < lim)):                           # rung tidak boleh melewati sisi jauh awan / ekstrem sweep
            break
        levels.append(round_tick(px, "up" if sell else "down", tick))
    levels = sorted(set(levels), reverse=not sell)                              # SELL naik, BUY turun; limit 1 = tepi awan
    l1, last = levels[0], levels[-1]
    buf = ICH_SL_BUFFER_ATR * atr
    base = max(far, ext, last) if sell else min(far, ext, last)
    sl_raw = base + buf if sell else base - buf
    lo, hi = _ich_sl_bounds(sym, atr)
    dist = abs(sl_raw - l1)
    if dist > hi:
        return None, f"SL {dist:.2f} melebihi batas {hi:.2f}"
    if dist < lo:
        sl_raw = l1 + lo if sell else l1 - lo
    sl_raw = max(sl_raw, last + buf) if sell else min(sl_raw, last - buf)
    sl = round_tick(sl_raw, "up" if sell else "down", tick)
    risk1 = abs(sl - l1)
    if risk1 <= 0:
        return None, "risk nol"
    sgn = -1.0 if sell else 1.0
    tp1 = round_tick(l1 + sgn * ICH_TP1_R * risk1, "up" if sell else "down", tick)
    tp2 = round_tick(l1 + sgn * ICH_TP2_R * risk1, "up" if sell else "down", tick)
    return dict(levels=levels, sl=sl, tp1=tp1, tp2=tp2, risk1=risk1, step=step), None


def _ich_overlay(x, candles, t, plan):
    """Seri/level untuk chart xau-btc: Kumo (+26 bar ke depan), Tenkan, Kijun, ladder, SL/TP, penanda AMD."""
    n, fut = t + 1, 26
    sa = np.r_[x["span_a"].to_numpy(float)[:n], x["a_raw"].to_numpy(float)[n - fut:n]]
    sb = np.r_[x["span_b"].to_numpy(float)[:n], x["b_raw"].to_numpy(float)[n - fut:n]]
    sell = plan["side"] == "SELL"
    col_sl, col_tp, col_l = (210, 40, 55), (20, 145, 75), (120, 60, 180)
    hl = []
    for k, px in enumerate(plan["rungs_px"][1:], 2):
        hl.append(dict(price=px, name=f"L{k}", color=col_l, style="dashed"))
    hl.append(dict(price=plan["tp1"], name="TP1", color=col_tp, style="dashed"))
    marks = [dict(ts=int(candles[t].timestamp), text="MANIP" if plan["mode"] == "FAKEOUT" else "BREAK",
                  color=(240, 120, 0) if plan["mode"] == "FAKEOUT" else (30, 90, 205), use_high=sell, above=sell)]
    boxes = []
    if plan["mode"] == "FAKEOUT" and t - ICH_ACCUM_BARS >= 0:
        boxes.append(dict(ts_from=int(candles[t - ICH_ACCUM_BARS].timestamp), ts_to=int(candles[t - 1].timestamp),
                          low=plan["accum_low"], high=plan["accum_high"], name="ACCUM", fill=(255, 246, 214), line=(200, 170, 60)))
    return dict(
        future=fut,
        bands=[dict(a=sa.tolist(), b=sb.tolist(), up=(214, 238, 222), down=(246, 214, 218), name="KUMO", legend=(190, 100, 115))],
        lines=[dict(v=x["tenkan"].to_numpy(float)[:n].tolist(), color=(235, 110, 40), width=1, name="TENKAN"),
               dict(v=x["kijun"].to_numpy(float)[:n].tolist(), color=(40, 90, 200), width=2, name="KIJUN"),
               dict(v=sa[:n].tolist(), color=(60, 170, 100), width=1, name="SPAN A"),
               dict(v=sb[:n].tolist(), color=(210, 80, 90), width=1, name="SPAN B")],
        hlines=hl, marks=marks, boxes=boxes,
        labels=dict(entry="L1", sl="SL", tp="TP2"))


def find_ichimoku(d, sym, tick, last_bars=None, include_all=False):
    """Sinyal ICH pada frame M15 (offset CFD sudah diterapkan, bar closed saja). Dicek di `last_bars` bar terakhir."""
    if not ICH_ENABLED or d is None or len(d) < 52 + 26 + 3:                 # syarat hanya bergantung pada indeks bar (bukan panjang data)
        return []
    last_bars = max(1, int(last_bars or ICH_LAST_BARS))
    x = ich_frame(d)
    A = _ich_arrays(d, x)
    n = len(d)
    candles = None
    out = []
    for t in range(max(52 + 26 + 2, n - last_bars), n):
        found = None
        for mode in ICH_MODES:
            for sell in (True, False):
                core = _ich_core(A, t, sell, mode)
                if core is None:
                    continue
                edge, far, ext = core
                conf = _ich_conf(A, t, sell, mode, far)
                if len(conf) < ICH_MIN_SCORE:
                    continue
                lad, why = _ich_ladder(sym, sell, mode, edge, far, ext, float(A["atr"][t]), tick)
                if lad is None:
                    continue
                found = (mode, sell, edge, far, ext, conf, lad)
                break
            if found:
                break
        if not found:
            continue
        mode, sell, edge, far, ext, conf, lad = found
        side = "SELL" if sell else "BUY"
        levels = lad["levels"]
        l1 = levels[0]
        track_tp = lad["tp2"] if ICH_TRACK_TP == 2 else lad["tp1"]
        atr = float(A["atr"][t])
        N = ICH_ACCUM_BARS
        pip = ICH_PIP_XAU if sym == "XAUUSD" else None
        plan = dict(
            kind="ICH", side=side, order_type="LIMIT", i=int(t), time=d.index[t] + pd.Timedelta(minutes=ICH_TF_MINUTES - 1),
            entry=l1, sl=lad["sl"], tp=track_tp, tp1=lad["tp1"], tp2=lad["tp2"], risk=lad["risk1"],
            risk_pct=lad["risk1"] / l1 * 100, reward_pct=abs(track_tp - l1) / l1 * 100,
            rr_eff=abs(track_tp - l1) / lad["risk1"], status="PENDING", tick=tick, dec=dec_of(tick),
            rungs_px=[float(v) for v in levels], rung_rr=[abs(lad["tp1"] - v) / max(abs(lad["sl"] - v), 1e-12) for v in levels],
            mode=mode, score=len(conf), conf=conf, cloud_top=float(A["cloud_top"][t]), cloud_bot=float(A["cloud_bot"][t]),
            edge=edge, far=far, ext=ext, tenkan=float(A["tenkan"][t]), kijun=float(A["kijun"][t]), atr15=atr,
            accum_high=float(A["high"][t - N:t].max()), accum_low=float(A["low"][t - N:t].min()),
            sl_pips=(lad["risk1"] / pip) if pip else None,
            sl_pips_last=(abs(lad["sl"] - levels[-1]) / pip) if pip else None,
            expire_bars=ICH_EXPIRE_MIN, bar_ts=int(d.index[t].timestamp()))
        if candles is None:
            candles = xb_candles_from_df(d)
        sub = candles[: t + 1]
        structure = xb_market_structure(sub)
        bar = sub[-1]
        cloud_txt = f"{plan['cloud_bot']:,.2f} - {plan['cloud_top']:,.2f}"
        why_mode = ("manipulasi (sweep) menembus tepi awan lalu close kembali = fakeout AMD" if mode == "FAKEOUT"
                    else "close menembus awan = S/R break; limit di tepi awan yang ditembus (flip S/R)")
        plan["xs"] = XBSignal(
            symbol=sym, timeframe=f"M{ICH_TF_MINUTES}", side=side, entry=float(l1), stop_loss=float(lad["sl"]),
            take_profit=float(lad["tp2"]), candle_timestamp=int(bar.timestamp), session=xb_session_for(bar.timestamp),
            score=len(conf), score_max=ICH_SCORE_MAX,
            reasons=[why_mode, f"harga {'di bawah' if sell else 'di atas'} awan (S/R dinamis M{ICH_TF_MINUTES})"] + conf[:6]
                    + [f"limit bertingkat {len(levels)} level dari tepi awan", "SL di luar awan/ekstrem sweep"],
            analysis={"STRATEGY": "Ichimoku M15 + Pattern Fakeout/AMD (limit di cloud)", "MODE": mode, "CLOUD": cloud_txt,
                      "TENKAN/KIJUN": f"{plan['tenkan']:,.2f} / {plan['kijun']:,.2f}", "ATR": f"{atr:.2f}",
                      "RR TP1": f"{ICH_TP1_R:g}R", "RR TP2": f"{ICH_TP2_R:g}R",
                      "LIMIT": ", ".join(f"{v:,.2f}" for v in levels)},
            support=float(structure["support"]), resistance=float(structure["resistance"]),
            demand_zone=f"{structure['demand_low']:.2f}-{structure['demand_high']:.2f}",
            supply_zone=f"{structure['supply_low']:.2f}-{structure['supply_high']:.2f}",
            patterns=(["Fakeout/AMD", "Liquidity Sweep"] if mode == "FAKEOUT" else ["Cloud Break", "S/R Flip"]) + ["Ichimoku"],
            offset_applied=float(OFFSETS.get(sym, 0.0)),
            target_basis=f"TP1 {ICH_TP1_R:g}R / TP2 {ICH_TP2_R:g}R dari limit 1",
            tp1=float(lad["tp1"]), tp2=float(lad["tp2"]))
        plan["xs_structure"] = structure
        plan["xs_candles"] = sub
        plan["xs_tf"] = f"M{ICH_TF_MINUTES}"
        plan["xs_bars"] = ICH_CHART_BARS
        plan["xs_overlay"] = _ich_overlay(x, sub, t, plan)
        out.append(plan)
    return out


def find_ichimoku_live(sym, tick, include_all=False):
    """Ambil M15 Bybit (cache; hanya fetch bila bar M15 baru sudah mungkin ada) lalu cari sinyal ICH."""
    if not ICH_ENABLED:
        return []
    raw15 = get_raw_tf(sym, ICH_TF_MINUTES)
    if raw15 is None or raw15.empty:
        return []
    return find_ichimoku(adj(raw15, OFFSETS[sym]), sym, tick, ICH_LAST_BARS, include_all)


def ich_caption(sym, p):
    """Kartu detail ICH (teks polos, di-escape karena parse_mode HTML)."""
    xs, st = p["xs"], p.get("xs_structure") or {}
    d, side, sell = p["dec"], p["side"], p["side"] == "SELL"
    f = lambda v: f"{v:,.{d}f}"
    n = len(p["rungs_px"])
    lines = [f"CHART {sym} M{ICH_TF_MINUTES} | ICHIMOKU + " + ("FAKEOUT/AMD" if p["mode"] == "FAKEOUT" else "BREAK S/R CLOUD"),
             f"{side} LIMIT x{n} di cloud (order pending, BUKAN market)"]
    for k, px in enumerate(p["rungs_px"], 1):
        lines.append(f"Limit {k}: {f(px)}" + ("  (tepi cloud)" if k == 1 else ""))
    if p.get("sl_pips") is not None:
        sl_note = f"{p['sl_pips']:.0f} pip dari limit 1 / {p['sl_pips_last']:.0f} pip dari limit {n} (1 pip = ${ICH_PIP_XAU:g})"
    else:
        sl_note = f"{p['risk'] / max(p['atr15'], 1e-12):.1f}x ATR M{ICH_TF_MINUTES} dari limit 1"
    lines.append(f"SL     : {f(p['sl'])}  ({sl_note})")
    lines.append(f"TP1    : {f(p['tp1'])}  ({ICH_TP1_R:g}R dari limit 1)" + ("  <- dipakai tracker" if ICH_TRACK_TP != 2 else ""))
    lines.append(f"TP2    : {f(p['tp2'])}  ({ICH_TP2_R:g}R)" + ("  <- dipakai tracker" if ICH_TRACK_TP == 2 else ""))
    lines.append("RR/limit: " + " | ".join(f"L{k} {r:.1f}R" for k, r in enumerate(p["rung_rr"], 1)) + " (ke TP1)")
    lines.append(f"Dasar  : {xs.target_basis}")
    lines.append(f"Confluence: {p['score']}/{ICH_SCORE_MAX}")
    lines.append(f"Cloud  : {f(p['cloud_bot'])} - {f(p['cloud_top'])} | Tenkan {f(p['tenkan'])} | Kijun {f(p['kijun'])}")
    if p["mode"] == "FAKEOUT":
        lines.append(f"AMD    : Accumulation {ICH_ACCUM_BARS} bar ({f(p['accum_low'])}-{f(p['accum_high'])}) -> "
                     f"Manipulation (sweep {f(p['ext'])}, close kembali {'di bawah' if sell else 'di atas'} tepi cloud) -> "
                     f"Distribution ({'turun' if sell else 'naik'})")
    else:
        lines.append(f"Break  : close menembus cloud {'ke bawah' if sell else 'ke atas'}; tepi {f(p['edge'])} jadi "
                     f"{'resistance' if sell else 'support'} baru (flip S/R)")
    lines.append(f"Session/Bar: {xs.session} / {xb_fmt_time(xs.candle_timestamp)} WIB")
    lines.append(f"Demand: {xs.demand_zone} | Supply: {xs.supply_zone}")
    lines.append(f"Pattern: {', '.join(xs.patterns)}")
    lines.append(f"Alasan lengkap: {'; '.join(xs.reasons)}")
    hrs, mins = divmod(ICH_EXPIRE_MIN, 60)
    lines.append("Berlaku: limit batal bila TP tercapai tanpa terisi atau lewat " + (f"{hrs} jam" if hrs else "") + (f" {mins} mnt" if mins else ""))
    lines.append("Analisis bukan jaminan profit; cek spread dan berita sebelum pasang limit.")
    return html.escape("\n".join(lines), quote=False)


def _trade_plans(p):
    """Satu sinyal -> daftar order paper. ICH = satu trade per rung limit (lot dibagi rata bila ICH_RUNG_SPLIT)."""
    if p.get("kind") != "ICH" or not p.get("rungs_px"):
        return [p]
    k = len(p["rungs_px"])
    out = []
    for n, px in enumerate(p["rungs_px"], 1):
        q = dict(p)
        q.update(entry=float(px), rung=n, rungs=k, group=p["time"].isoformat(), expire_bars=ICH_EXPIRE_MIN)
        q["lot"] = float(p.get("lot", TRADE_LOT)) / (k if ICH_RUNG_SPLIT else 1)
        out.append(q)
    return out


# ============================================================ TICK
def round_tick(p, mode, tick):
    q = p / tick
    return round((math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 1e-9)) * tick, 8)


# ============================================================ CHART
def make_chart(plan, ticker, path, demo=False):
    """Chart gaya xau-btc (putih) untuk sinyal ICH: Kumo, Tenkan/Kijun, ladder limit, SL/TP, kotak AMD, volume profile + POC."""
    return make_chart_xb(plan, ticker, path, demo=demo)


# ============================================================ TELEGRAM
def build_caption(sym, p):
    """Kartu sinyal (plain text ter-escape; parse_mode HTML). Hanya sinyal ICH."""
    return ich_caption(sym, p)


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


def _hold_cap(kind):
    caps = {"ICH": ICH_MAX_HOLD_BARS, "A": 180, "SF": 120, "C": 240}        # A/SF/C: hanya untuk sisa trade lama saat upgrade
    return caps.get(kind, MAX_HOLD_BARS)


def exit_tag(t):
    """Label keluar yang jujur: SL dengan stop yang sudah digeser = SL+/BE+ (profit terkunci), bukan 'SL HIT'."""
    st = t.get("status")
    if st == "SL":
        stage = int(t.get("be_stage", 0) or 0)
        return "SL+" if stage >= 2 else ("BE+" if stage == 1 else "SL")
    return st


def simulate(t, raw):
    """Replay bar 1m secara konservatif + BE/SL+ otomatis.

    Jenis order: NEXT_OPEN (market di open bar berikutnya), BUY_STOP (terisi saat harga menembus level; batal bila
    close < low sinyal / kedaluwarsa), LIMIT (BUY/SELL limit: terisi saat harga menyentuh level, di open bila open sudah
    melewati level; batal bila target tercapai lebih dulu atau kedaluwarsa `expire_bars`).

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
    cap = _hold_cap(t["kind"])
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
            elif order_type == "LIMIT":
                expire = int(t.get("expire_bars") or ICH_EXPIRE_MIN)
                buy = side == "BUY"
                touched = (l <= entry) if buy else (h >= entry)
                missed = (h >= tp) if buy else (l <= tp)           # target tercapai tanpa kita terisi
                if not touched:
                    if missed or waited >= expire:
                        out.update(status="CANCELLED", exit_time=iso); break
                    continue
                better_open = (o <= entry) if buy else (o >= entry)    # open sudah melewati level limit -> terisi di open
                if missed and not better_open:                     # urutan intrabar tak diketahui -> konservatif: target dulu
                    out.update(status="CANCELLED", exit_time=iso); break
                fill = round_tick(o, "up" if buy else "down", tick) if better_open else entry
                fill = min(fill, entry) if buy else max(fill, entry)   # tidak pernah lebih buruk dari harga limit
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
    out["exit_tag"] = exit_tag(out)
    return out


class Book:
    def __init__(self, path=None):
        self.path, self.data = path, dict(trades=[])
        self.cancelled = []                                     # trade yang baru dibatalkan pada update() terakhir
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
        t = dict(id=f"{sym}|{p['kind']}|{p['time'].isoformat()}" + (f"|L{int(p['rung'])}" if p.get("rung") else ""),
                 sym=sym, kind=p["kind"],
                 side=p.get("side","BUY"), order_type=p.get("order_type", "NEXT_OPEN"),
                 sig_time=p["time"].isoformat(), entry=float(p["entry"]), sl=float(p["sl"]),
                 initial_sl=float(p["sl"]), active_sl=float(p["sl"]), be_stage=0, stop_mode="INITIAL", tp=float(p["tp"]),
                 tick=float(p["tick"]), off=float(p.get("off", 0.0)), lot=float(p.get("lot", TRADE_LOT)),
                 status="PENDING", fill=None, fill_time=None, exit=None, exit_time=None)
        for key in ("sig_low","sig_high","invalidation","strict","tp2","expire_bars","rung","rungs","mode","group"):
            if key in p:
                if key == "strict": t[key] = bool(p[key])
                elif key in ("rung", "rungs", "expire_bars"): t[key] = int(p[key])
                elif key in ("mode", "group"): t[key] = str(p[key])
                else: t[key] = float(p[key])
        self.data["trades"].append(t)
        return t

    def n_active(self, sym):
        return sum(1 for t in self.data["trades"] if t["sym"] == sym and t["status"] in ACTIVE)

    def n_active_kind(self, sym, kind):
        return sum(1 for t in self.data["trades"] if t["sym"] == sym and t.get("kind") == kind and t["status"] in ACTIVE)

    def update(self, sym, raw):
        """Perbarui trade aktif satu simbol. Return trade yang baru kena TP/SL/TIMEOUT; yang baru batal ada di self.cancelled."""
        events = []
        self.cancelled = []
        for t in self.data["trades"]:
            if t["sym"] != sym or t["status"] not in ACTIVE:
                continue
            t.update(simulate(t, raw))
            if t["status"] in HIT:
                events.append(dict(t))
            elif t["status"] == "CANCELLED":
                self.cancelled.append(dict(t))
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
        gross_diff = (exit_px - fill) if side == "BUY" else (fill - exit_px)
        cost = PAPER_COST_XAU if trade.get("sym", "XAUUSD") == "XAUUSD" else PAPER_COST_BTC
        price_diff = gross_diff - cost                                  # biaya round-trip opsional (default 0)
        risk0 = abs(fill - float(trade.get("initial_sl", trade.get("sl", fill))))
        r_mult = price_diff / risk0 if risk0 > 0 else 0.0
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
        tag = exit_tag(trade)
        if tag == "TP": self.data["tp"] = int(self.data.get("tp", 0)) + 1
        elif tag in ("SL+", "BE+"): self.data["slp"] = int(self.data.get("slp", 0)) + 1     # stop profit terkunci
        elif tag == "SL": self.data["sl"] = int(self.data.get("sl", 0)) + 1                 # rugi penuh
        elif status == "TIMEOUT": self.data["timeout"] = int(self.data.get("timeout", 0)) + 1
        if pnl_idr > 0: self.data["win"] = int(self.data.get("win", 0)) + 1
        elif pnl_idr < 0: self.data["loss"] = int(self.data.get("loss", 0)) + 1

        event = {
            "id": trade_id,
            "ts": trade.get("exit_time") or now_ts().isoformat(),
            "sym": trade.get("sym"),
            "kind": trade.get("kind"),
            "status": status,
            "tag": tag,
            "be_stage": int(trade.get("be_stage", 0) or 0),
            "r": round(r_mult, 3),
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

    def stats(self, events=None):
        """Ringkasan hasil dari daftar event. Jujur: SL dengan stop yang sudah digeser (SL+/BE+) = profit terkunci."""
        ev = self.data.get("events", []) if events is None else events
        s = dict(n=0, tp=0, slp=0, sl=0, to=0, win=0, loss=0, pnl=0.0, by={})
        for e in ev:
            st, pnl = e.get("status"), float(e.get("pnl_idr", 0) or 0)
            tag = e.get("tag") or ("SL+/BE+" if (st == "SL" and pnl > 0) else st)       # event lama tanpa tag
            s["n"] += 1
            s["pnl"] += pnl
            if tag == "TP": s["tp"] += 1
            elif tag in ("SL+", "BE+", "SL+/BE+"): s["slp"] += 1
            elif tag == "SL": s["sl"] += 1
            else: s["to"] += 1
            if pnl > 0: s["win"] += 1
            elif pnl < 0: s["loss"] += 1
            b = s["by"].setdefault(e.get("kind") or "?", dict(n=0, win=0, pnl=0.0))
            b["n"] += 1; b["win"] += int(pnl > 0); b["pnl"] += pnl
        return s

    def summary_text(self):
        d = self.data
        s = self.stats()
        n = s["n"]
        def rp(v): return f"Rp {int(round(v)):,.0f}".replace(",", ".")
        wr = (s["win"] / n * 100.0) if n else 0.0
        tpr = (s["tp"] / n * 100.0) if n else 0.0
        lines = ["<b>CAPITAL PAPER TRACKER</b>",
                 f"Modal awal  : {rp(d.get('initial_capital', CAPITAL_START))}",
                 f"Saldo kini  : {rp(d.get('balance', CAPITAL_START))}",
                 f"Realized P/L: {rp(d.get('realized_pnl_idr', 0))}",
                 f"Selesai {n} | TP {s['tp']} | SL+/BE+ {s['slp']} | SL {s['sl']} | TO {s['to']}",
                 f"Winrate P/L>0: {wr:.1f}% ({s['win']} untung / {s['loss']} rugi) | hit TP {tpr:.1f}%"]
        if s["by"]:
            rows = [f"{k:<3} {b['n']:>4}x  win {b['win'] / b['n'] * 100:>3.0f}%  {rp(b['pnl']):>15}"
                    for k, b in sorted(s["by"].items())]
            lines.append("Per sinyal:\n<pre>" + "\n".join(rows) + "</pre>")
        lines += [f"Top-up      : {rp(d.get('topups_idr',0))} ({int(d.get('topup_count',0))}x)",
                  f"Lot/entry   : {TRADE_LOT:.2f}"]
        return "\n".join(lines)

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
        s = self.stats(events)
        total_pnl = s["pnl"]
        tops = sum(float(e.get("topup_idr",0)) for e in events)
        start_bal = float(events[0].get("balance_before", self.data.get("balance", CAPITAL_START))) if events else float(self.data.get("balance", CAPITAL_START))
        end_bal = float(self.data.get("balance", CAPITAL_START))
        closed = s["n"]
        wr = (s["win"] / closed * 100.0) if closed else 0.0
        tpr = (s["tp"] / closed * 100.0) if closed else 0.0
        def rp(v): return f"Rp {int(round(v)):,.0f}".replace(",", ".")
        lines = [
            f"<b>REKAP MINGGUAN — {this_sat:%d %b %Y %H:%M} WIB</b>",
            f"Periode: {prev_sat:%d %b %H:%M} → {this_sat:%d %b %H:%M} WIB",
            "",
            f"Modal awal minggu : {rp(start_bal)}",
            f"Saldo akhir       : {rp(end_bal)}",
            f"P/L realized      : {rp(total_pnl)}",
            f"Trade selesai     : {closed}",
            f"TP / SL+BE+ / SL / TO : {s['tp']} / {s['slp']} / {s['sl']} / {s['to']}",
            f"Winrate P/L>0     : {wr:.1f}% ({s['win']} untung / {s['loss']} rugi) | hit TP {tpr:.1f}%",
            f"Auto top-up       : {rp(tops)}",
            f"Ukuran entry      : {TRADE_LOT:.2f} lot",
        ]
        if events:
            lines.append("")
            lines.append("<pre>")
            for e in events[-12:]:
                ts = pd.Timestamp(e["ts"]).tz_convert(TZ) if pd.Timestamp(e["ts"]).tzinfo else pd.Timestamp(e["ts"]).tz_localize(TZ)
                tg = e.get("tag") or e.get("status")
                lines.append(f"{ts:%d/%m %H:%M} {str(e.get('sym')):7} {str(e.get('kind')):3} {str(tg):7} {rp(e.get('pnl_idr',0)):>14}")
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
    tag = exit_tag(t)
    side = t.get("side", "BUY")
    fill, exit_px = float(t["fill"]), float(t["exit"])
    risk = abs(fill - float(t.get("initial_sl", t["sl"])))             # R dari harga FILL ke SL awal (bukan harga sinyal)
    diff = (exit_px - fill) if side == "BUY" else (fill - exit_px)
    r_mult = diff / max(risk, 1e-12)
    if t["status"] == "TIMEOUT":
        title = ("⏱", "KELUAR MAKS BAR" + (" • profit" if diff > 0 else (" • rugi" if diff < 0 else "")))
    else:
        title = {"TP": ("✅", "TP HIT"),
                 "SL+": ("\U0001F7E2", "SL+ • PROFIT TERKUNCI"),
                 "BE+": ("\U0001F7E1", "BE+ • PROFIT KECIL TERKUNCI"),
                 "SL": ("❌", "SL HIT")}.get(tag, ("⚠", str(t["status"])))
    labels = {"A":"Momentum Long", "B":"Legacy Shadow Fakeout", "C":"Accumulation → Expansion",
              "SF":"Shadow Fakeout 1M", "ICH":"Ichimoku M15 Fakeout/AMD"}
    kind = labels.get(t["kind"], t["kind"])
    st, et = pd.Timestamp(t["sig_time"]), pd.Timestamp(t["exit_time"])
    stop_mode = t.get("stop_mode", "INITIAL")
    rung = f"  (limit L{int(t['rung'])}/{int(t.get('rungs', t['rung']))})" if t.get("rung") else ""
    rows = [f"Side  {side}{rung}", f"Entry {fill:>10,.{d}f}", f"Exit  {exit_px:>10,.{d}f}",
            f"Hasil {diff:>+10,.{d}f}  ({r_mult:+.2f}R)",
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


def build_event_group(items):
    """Satu pesan untuk beberapa rung ladder (ICH) yang selesai pada scan yang sama. items = [(trade, capital_event)]."""
    t0 = items[0][0]
    d = dec_of(t0["tick"])
    rows, tags, pnl_total, bal, topup = [], [], 0.0, None, 0.0
    for t, ce in sorted(items, key=lambda z: int(z[0].get("rung", 0))):
        tag = exit_tag(t)
        tags.append(tag)
        side = t.get("side", "BUY")
        fill, ex = float(t["fill"]), float(t["exit"])
        risk = abs(fill - float(t.get("initial_sl", t["sl"])))
        diff = (ex - fill) if side == "BUY" else (fill - ex)
        rows.append(f"L{int(t.get('rung', 0))} {fill:>10,.{d}f} -> {ex:>10,.{d}f}  {diff / max(risk, 1e-12):+.2f}R  {tag}")
        if ce:
            pnl_total += float(ce.get("pnl_idr", 0.0))
            bal = float(ce.get("balance_after", 0.0))
            topup += float(ce.get("topup_idr", 0.0))
    uniq = set(tags)
    if uniq == {"TP"}:
        title = ("✅", "TP HIT")
    elif uniq == {"SL"}:
        title = ("❌", "SL HIT")
    elif uniq <= {"SL+", "BE+"}:
        title = ("\U0001F7E2", "SL+ • PROFIT TERKUNCI")
    else:
        title = ("\U0001F4CA", "LADDER SELESAI")
    st = pd.Timestamp(t0["sig_time"])
    et = max(pd.Timestamp(t["exit_time"]) for t, _ in items)
    lines = [f"{title[0]} <b>{title[1]} — {t0['sym']}</b> (Ichimoku M15, {len(items)} limit)",
             f"<pre>Side {t0.get('side', 'BUY')}\n" + "\n".join(rows) + "</pre>",
             f"Sinyal {st:%H:%M} → keluar {et:%d %b %H:%M} WIB"]
    if bal is not None:
        rp = lambda v: f"Rp {int(round(v)):,.0f}".replace(",", ".")
        lines.append(f"Modal: {rp(bal)}  |  P/L: {rp(pnl_total)}" + (f"  | Top-up: {rp(topup)}" if topup else ""))
    return "\n".join(lines)


def _ich_cancel_notes(book, sym):
    """Satu pesan per ladder ICH yang batal TOTAL (tak ada rung terisi): pengguna perlu tahu bila limit sudah dipasang manual."""
    groups = {}
    for t in book.cancelled:
        if t.get("sym") == sym and t.get("kind") == "ICH":
            groups.setdefault(t.get("group") or t["sig_time"], []).append(t)
    notes = []
    for g in groups:
        allr = [x for x in book.data["trades"]
                if x["sym"] == sym and x.get("kind") == "ICH" and (x.get("group") or x["sig_time"]) == g]
        if any(x.get("fill") for x in allr) or any(x["status"] in ACTIVE for x in allr):
            continue                                            # ada rung terisi/aktif: dilaporkan lewat event TP/SL
        t0 = allr[0]
        d = dec_of(t0["tick"])
        lv = " / ".join(f"{float(x['entry']):,.{d}f}" for x in sorted(allr, key=lambda z: int(z.get("rung", 0))))
        notes.append(f"❎ <b>LIMIT DIBATALKAN — {sym}</b> (Ichimoku M15)\n"
                     f"{t0.get('side', 'BUY')} limit {lv} tidak terisi (target tercapai lebih dulu atau kedaluwarsa).\n"
                     f"Batalkan order limit bila sudah dipasang.")
    return notes


def handle_plan(sym, p, sent, book, capital, include_all):
    key = f"{sym}|{p['kind']}|{p['time'].isoformat()}"
    if key in sent:
        return False
    if not include_all:
        if (now_ts() - p["time"]).total_seconds() / 60 > ICH_MAX_AGE_MIN:
            return False
        if ICH_MAX_PER_DAY > 0 and kind_count(sent, sym, "ICH", p["time"]) >= ICH_MAX_PER_DAY:
            return False
        last_ich = _last_sent_time(sent, sym, "ICH")
        if ICH_COOLDOWN_MIN > 0 and last_ich is not None and (p["time"] - last_ich) < pd.Timedelta(minutes=ICH_COOLDOWN_MIN):
            return False
        if ICH_ONE_AT_A_TIME and book.n_active_kind(sym, "ICH"):
            return False
    p["off"] = OFFSETS[sym]
    p["lot"] = TRADE_LOT
    d = p["dec"]
    path = os.path.join(CHART_DIR, f"{sym}_{p['kind']}_{p['time']:%Y%m%d_%H%M}.png")
    try:
        path = make_chart(p, sym, path)
    except Exception as e:                                    # chart gagal tidak boleh menahan sinyal
        print(f"{sym}: [{p['kind']}] chart gagal ({e}); kirim teks saja")
        path = None
    print(f"{sym}: [{p['kind']}] {p['time']:%H:%M} entry {p['entry']:,.{d}f} SL {p['sl']:,.{d}f} "
          f"TP {p['tp']:,.{d}f} [{p['status']}]")
    if tg_ready():
        cap = build_caption(sym, p)
        if path:
            tg_send_photo(path, cap, p.get("kind"))
        else:
            send_text(cap, topic_id(p.get("kind")))
    else:
        print("   TELEGRAM env belum diset; chart disimpan:", path)
    sent.add(key); save_sent(sent)
    if not include_all and p["status"] == "PENDING":
        for q in _trade_plans(p):                             # ICH: satu trade paper per rung limit
            book.add(sym, q)
            capital.register_entry()
        book.save(); capital.save()
    return True


def scan_once(include_all, sent, book, capital):
    os.makedirs(CHART_DIR, exist_ok=True)
    now = now_ts()
    st = dict(sig=0, ev=0, err=0)
    for sym in PAIRS:
        try:                                                     # (1) lacak limit/TP/SL aktif (bar 1m hanya diambil bila ada trade aktif)
            if book.n_active(sym):
                raw = get_raw(sym)
                if raw.empty:
                    print(f"{sym}: data 1m kosong")
                else:
                    groups = {}
                    for e in book.update(sym, raw):
                        print(f"{sym}: {e['status']} {e['fill']} -> {e['exit']}")
                        ce = capital.settle(e)
                        gkey = (e.get("kind"), e.get("group")) if (e.get("rungs") or 0) > 1 else (id(e),)
                        groups.setdefault(gkey, []).append((e, ce))
                    for items in groups.values():
                        send_text(build_event(*items[0]) if len(items) == 1 else build_event_group(items), topic_id("EVENT"))
                        st["ev"] += len(items)
                    for note in _ich_cancel_notes(book, sym):
                        send_text(note, topic_id("ICH") or topic_id("EVENT"))
                    book.save(); capital.save()
        except Exception as e:                                   # error tracker tidak boleh mematikan pencarian sinyal
            st["err"] += 1
            print(f"{sym}: error tracker -> {e}")
        try:                                                     # (2) sinyal ICH baru (M15)
            if not ICH_ENABLED or (XAU_PAUSE and sym == "XAUUSD" and xau_closed(now) and not include_all):
                continue                                         # CFD emas tutup / sinyal dimatikan
            tick = get_tick(sym)
            plans = find_ichimoku_live(sym, tick, include_all)
            plans.sort(key=lambda z: z["time"])
            for p in plans:
                st["sig"] += int(handle_plan(sym, p, sent, book, capital, include_all))
        except Exception as e:
            st["err"] += 1
            print(f"{sym}: ICH error -> {e}")
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
            raw, _ = fetch_klines(sym, 5)                          # harga terbaru (bar 1m terakhir yang sudah close)
            if raw.empty:
                raise RuntimeError("data kosong")
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
            f"Engine: ICH={'ON' if ICH_ENABLED else 'OFF'}\n"
            f"ICH M{ICH_TF_MINUTES} mode={'+'.join(ICH_MODES)} limit x{ICH_RUNGS} | SL {ICH_SL_MIN_PIPS:g}-{ICH_SL_MAX_PIPS:g} pip | TP {ICH_TP1_R:g}R/{ICH_TP2_R:g}R | min {ICH_MIN_SCORE}/{ICH_SCORE_MAX}\n"
            f"Batas: {ICH_MAX_PER_DAY}/hari | jeda {ICH_COOLDOWN_MIN}m | limit kedaluwarsa {ICH_EXPIRE_MIN}m | satu ladder aktif={'ON' if ICH_ONE_AT_A_TIME else 'OFF'}\n"
            f"Chart bars={ICH_CHART_BARS} POC={'ON' if CHART_POC else 'OFF'}\n"
            f"BE/SL+={'ON' if BE_ENABLED else 'OFF'} | BE +{BE_TRIGGER_R:.2f}R→+{BE_OFFSET_R:.2f}R | SL+ +{SLPLUS_TRIGGER_R:.2f}R→+{SLPLUS_LOCK_R:.2f}R"
        ), thread)
    elif cmd == "/topics":
        labels = {"ICH": "ICHIMOKU M15", "EVENT": "EVENTS"}
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
def _ich_demo_frame(seed, n0=150, tail=16):
    """M15 sintetis: tren turun -> pantulan ke tepi bawah awan -> bar manipulasi (fakeout) di akhir (hanya demo/uji)."""
    rng = np.random.default_rng(seed)
    close = 4200.0 + np.cumsum(rng.normal(-0.55, 1.6, n0))
    op = np.r_[close[0] + 0.55, close[:-1]] + rng.normal(0, 0.4, n0)
    hi = np.maximum(op, close) + np.abs(rng.normal(0, 1.1, n0))
    lo = np.minimum(op, close) - np.abs(rng.normal(0, 1.1, n0))
    vol = 1000.0 * np.exp(rng.normal(0, 0.25, n0))
    idx = pd.date_range("2026-10-06 08:00", periods=n0 + tail, freq="15min", tz=TZ)
    base = pd.DataFrame(dict(open=op, high=hi, low=lo, close=close, volume=vol), index=idx[:n0])
    h, l = base["high"], base["low"]
    a_raw = ((h.rolling(9).max() + l.rolling(9).min()) / 2 + (h.rolling(26).max() + l.rolling(26).min()) / 2) / 2
    b_raw = (h.rolling(52).max() + l.rolling(52).min()) / 2
    edge = min(a_raw.iloc[n0 + tail - 1 - 26], b_raw.iloc[n0 + tail - 1 - 26])      # tepi bawah awan di bar manipulasi
    last = float(close[-1])
    target = edge - 2.2
    path = np.linspace(last, target, tail)
    o2, h2, l2, c2, v2, prev = [], [], [], [], [], last
    for k in range(tail - 1):
        cc = path[k + 1] if k < tail - 2 else target
        o2.append(prev); c2.append(cc)
        h2.append(max(prev, cc) + abs(rng.normal(0.5, 0.2))); l2.append(min(prev, cc) - abs(rng.normal(0.5, 0.2)))
        v2.append(1000.0 * float(np.exp(rng.normal(0, 0.2)))); prev = cc
    cc = edge - 1.2
    hh = edge + 0.55 * 6.0 + 1.5
    ll = cc - 0.6
    hh = max(hh, max(prev, cc) + 0.4 * (hh - ll))
    o2.append(prev); h2.append(hh); l2.append(ll); c2.append(cc); v2.append(2400.0)
    tailf = pd.DataFrame(dict(open=o2, high=h2, low=l2, close=c2, volume=v2), index=idx[n0:])
    df = pd.concat([base, tailf])
    for k in ("open", "high", "low", "close"):
        df[k] = np.round(df[k] / 0.01) * 0.01
    df["high"] = np.maximum(df["high"], np.maximum(df["open"], df["close"]))
    df["low"] = np.minimum(df["low"], np.minimum(df["open"], df["close"]))
    return df


def demo_ichimoku():
    """Data sintetis M15: fakeout SELL di tepi bawah awan -> ladder limit + SL ~100 pip + TP1/TP2."""
    for seed in range(1, 300):
        df = _ich_demo_frame(seed)
        plans = find_ichimoku(df, "XAUUSD", 0.01, last_bars=1, include_all=True)
        if plans:
            return df, plans[0]
    raise RuntimeError("demo ICH gagal")


def _ich_random_frame(seed, n=1500):
    """M15 sintetis (random walk bergerombol, jalur tick konsisten) untuk contoh/uji mode BREAK. Bukan data pasar."""
    rng = np.random.default_rng(seed)
    reg = n // 120 + 1
    drift = np.repeat(rng.choice([-0.12, 0.0, 0.0, 0.12], reg), 120)[:n]
    vol = np.repeat(rng.choice([0.55, 0.8, 1.2], reg), 120)[:n]
    path = (4100.0 + np.cumsum(rng.normal(drift[:, None], vol[:, None], (n, 15)).reshape(-1))).reshape(n, 15)
    op = np.r_[4100.0, path[:-1, -1]]
    cl = path[:, -1]
    hi, lo = np.maximum(path.max(axis=1), op), np.minimum(path.min(axis=1), op)
    rb = hi - lo
    volu = 1000.0 * np.exp(rng.normal(0, 0.3, n)) * (1.0 + rb / max(float(np.median(rb)), 1e-9))
    df = pd.DataFrame(dict(open=op, high=hi, low=lo, close=cl, volume=volu),
                      index=pd.date_range("2026-09-14 00:00", periods=n, freq="15min", tz=TZ))
    for k in ("open", "high", "low", "close"):
        df[k] = np.round(df[k] / 0.01) * 0.01
    df["high"] = np.maximum(df["high"], np.maximum(df["open"], df["close"]))
    df["low"] = np.minimum(df["low"], np.minimum(df["open"], df["close"]))
    return df


def demo_ichimoku_break():
    """Data sintetis M15: sinyal mode BREAK S/R cloud pertama yang ditemukan (hanya untuk contoh tampilan/uji)."""
    for seed in range(1, 80):
        df = _ich_random_frame(seed)
        for p in find_ichimoku(df, "XAUUSD", 0.01, last_bars=len(df) - 90, include_all=True):
            if p["mode"] == "BREAK" and len(p["rungs_px"]) >= 2 and p["i"] >= 200:
                return df.iloc[: p["i"] + 1], p
    raise RuntimeError("demo ICH BREAK gagal")


def _plain(s):
    for tag in ("<b>", "</b>", "<pre>", "</pre>", "<i>", "</i>"):
        s = s.replace(tag, "")
    return s


def run_demo(send):
    os.makedirs(CHART_DIR, exist_ok=True)
    msgs = []
    for name, fn in (("DEMO_ICH_fakeout_amd_m15", demo_ichimoku), ("DEMO_ICH_break_cloud_m15", demo_ichimoku_break)):
        m, p = fn()
        p["off"] = OFFSETS["XAUUSD"]
        path = os.path.join(CHART_DIR, f"{name}.png")
        make_chart(p, "XAUUSD", path, demo=True)
        cap = build_caption("XAUUSD", p)
        print(f"--- {name} ---\n{_plain(cap)}\nChart: {path}\n")
        if send:
            if not tg_ready():
                sys.exit("Set TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID dulu.")
            tg_send_photo(path, cap, p.get("kind"))
    # contoh pesan hasil: ladder 3 limit kena TP, dan satu limit kena SL
    t0 = pd.Timestamp("2026-10-07 14:14", tz=TZ)
    grp = t0.isoformat()
    ladder = [dict(sym="XAUUSD", kind="ICH", side="SELL", order_type="LIMIT", sig_time=t0.isoformat(), rung=k, rungs=3, group=grp,
                   entry=e, sl=4123.9, initial_sl=4123.9, active_sl=4123.9, tp=4100.63, tick=0.01, status="TP", fill=e, exit=4100.63,
                   exit_time=(t0 + pd.Timedelta(minutes=95)).isoformat(), stop_mode="INITIAL", be_stage=0)
              for k, e in enumerate((4113.32, 4113.97, 4114.62), 1)]
    single = dict(ladder[0], rung=1, rungs=1, status="SL", exit=4123.9, exit_time=(t0 + pd.Timedelta(minutes=40)).isoformat())
    msgs += [build_event_group([(t, None) for t in ladder]), build_event(single)]
    for mm in msgs:
        print(_plain(mm), "\n")
        if send:
            send_text(mm)
    if send:
        print("Contoh terkirim ke Telegram.")


# ============================================================ SELF-TEST
def _selftest_path(start, last_close, waypoints):
    """Bar 1m sintetis: mulai di `start` (harga last_close), lalu mengikuti waypoint [(menit, harga)] secara linear."""
    rows = [(start, last_close, last_close, last_close, last_close, 800.0)]
    prev, t = last_close, start
    for mins, tgt in waypoints:
        for k in range(1, int(mins) + 1):
            px = round((prev + (tgt - prev) * k / mins) / 0.01) * 0.01
            o = rows[-1][4]
            rows.append((t + pd.Timedelta(minutes=k), o, max(o, px), min(o, px), px, 800.0))
        t, prev = t + pd.Timedelta(minutes=int(mins)), tgt
    return pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"]).set_index("t")


def run_self_test():
    print("=== BYBIT ICHIMOKU SELF-TEST ===")
    results = []

    def add(name, ok, info=""):
        results.append((name, "PASS" if ok else "FAIL", info))

    os.makedirs(CHART_DIR, exist_ok=True)
    plans = {}
    for label, fn in (("FAKEOUT", demo_ichimoku), ("BREAK", demo_ichimoku_break)):
        try:
            _, p = fn()
            plans[label] = p
            cap = build_caption("XAUUSD", p)
            sell = p["side"] == "SELL"
            lv, sl, t1, t2 = p["rungs_px"], p["sl"], p["tp1"], p["tp2"]
            order_ok = (all(a < b for a, b in zip(lv, lv[1:])) and sl > lv[-1] and t1 < lv[0] and t2 < t1) if sell else \
                       (all(a > b for a, b in zip(lv, lv[1:])) and sl < lv[-1] and t1 > lv[0] and t2 > t1)
            pips_ok = p["sl_pips"] is not None and ICH_SL_MIN_PIPS - 1 <= p["sl_pips"] <= ICH_SL_MAX_PIPS + 1
            ok = (p["kind"] == "ICH" and p["order_type"] == "LIMIT" and p["mode"] == label and len(lv) >= 2 and order_ok and pips_ok
                  and all(s in cap for s in ("Limit 1:", "SL     :", "TP1    :", "TP2    :", "Confluence: ", "/9", "Analisis bukan jaminan profit")))
            add(f"ICH {label} ENGINE+KARTU", ok, f"{p['side']} x{len(lv)} skor {p['score']}/{ICH_SCORE_MAX} SL {p['sl_pips']:.0f} pip")
            png_path = os.path.join(CHART_DIR, f"_selftest_{label}.png")
            make_chart(p, "XAUUSD", png_path, demo=True)
            with open(png_path, "rb") as fh:
                hdr = fh.read(8)
            add(f"ICH {label} CHART", hdr == b"\x89PNG\r\n\x1a\n" and os.path.getsize(png_path) > 6000, "PNG Kumo + ladder")
            os.remove(png_path)
        except Exception as e:                                   # pragma: no cover
            add(f"ICH {label}", False, repr(e)[:80])
    t0 = pd.Timestamp("2026-10-07 14:00", tz=TZ)
    base = dict(kind="ICH", order_type="LIMIT", sig_time=t0, tick=0.01, off=0, expire_bars=240)
    mk = lambda rows: pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                                   index=pd.date_range(t0, periods=len(rows), freq="1min", tz=TZ))
    s1 = simulate(dict(base, side="BUY", entry=100.0, sl=95.0, tp=110.0),
                  mk([(100, 100, 100, 100, 1), (102, 103, 101.5, 102, 1), (101, 101.5, 99.5, 100.5, 1), (100.5, 110.5, 100, 110, 1)]))
    add("LIMIT BUY FILL+TP", s1["status"] == "TP" and s1["fill"] == 100.0, s1["status"])
    s2 = simulate(dict(base, side="SELL", entry=100.0, sl=105.0, tp=90.0),
                  mk([(100, 100, 100, 100, 1), (98, 98.5, 97, 97.5, 1), (99, 100.5, 98.5, 99.5, 1), (99.5, 99.8, 89.5, 90, 1)]))
    add("LIMIT SELL FILL+TP", s2["status"] == "TP" and s2["fill"] == 100.0, s2["status"])
    s3 = simulate(dict(base, side="SELL", entry=100.0, sl=105.0, tp=90.0), mk([(98, 98, 98, 98, 1), (97, 97, 89, 90, 1)]))
    add("LIMIT BATAL (TP SEBELUM FILL)", s3["status"] == "CANCELLED", s3["status"])
    s4 = simulate(dict(base, side="BUY", entry=100.0, sl=95.0, tp=110.0),
                  mk([(100, 100, 100, 100, 1), (100, 106.5, 99.9, 106, 1), (106, 106, 101.0, 101.2, 1), (101.2, 101.4, 101.0, 101.1, 1)]))
    tag4 = exit_tag(dict(s4, status=s4["status"]))
    add("SL+ BUKAN 'SL HIT'", s4["status"] == "SL" and tag4 == "SL+", f"{s4['status']}/{tag4}")
    # ladder end-to-end: 3 limit -> semua terisi -> TP1 -> satu pesan grup + modal
    try:
        p = plans["FAKEOUT"]
        p["off"], p["lot"] = 0.0, TRADE_LOT
        book, cap_t = Book(None), CapitalTracker(None)
        for q in _trade_plans(p):
            book.add("XAUUSD", q)
            cap_t.register_entry()
        L, tp1 = p["rungs_px"], p["tp1"]
        sig = p["time"]
        up = L[-1] + 0.4                                         # harga naik melewati limit tertinggi -> ketiganya terisi (sinyal SELL)
        raw = _selftest_path(sig, round(float(L[0]) - 1.0, 2), [(6, round(up, 2)), (25, round(tp1 - 0.6, 2))])
        evs = book.update("XAUUSD", raw)
        groups = [(e, cap_t.settle(e)) for e in evs]
        msg = build_event_group(groups) if len(groups) > 1 else ""
        st = cap_t.stats()
        add("LADDER 3 RUNG -> TP (PESAN GRUP)", len(evs) == len(L) and all(e["status"] == "TP" for e in evs) and "TP HIT" in msg
            and msg.count("->") == len(L) and st["tp"] == len(L), f"{len(evs)} event")
    except Exception as e:                                       # pragma: no cover
        add("LADDER 3 RUNG -> TP", False, repr(e)[:80])
    # statistik jujur: SL dengan stop digeser = profit terkunci (bukan SL)
    cap_s = CapitalTracker(None)
    cap_s.data["events"] = [dict(status="TP", pnl_idr=100000.0, kind="ICH"), dict(status="SL", pnl_idr=30000.0, kind="ICH"),
                            dict(status="SL", pnl_idr=-80000.0, kind="ICH", tag="SL"), dict(status="TIMEOUT", pnl_idr=-5000.0, kind="ICH")]
    cs = cap_s.stats()
    add("STATISTIK SL+/BE+", (cs["tp"], cs["slp"], cs["sl"], cs["to"], cs["win"], cs["loss"]) == (1, 1, 1, 1, 2, 2),
        f"TP{cs['tp']} SL+{cs['slp']} SL{cs['sl']} TO{cs['to']}")
    for r in results:
        print(f"{r[0]:34} {r[1]:5} {r[2]}")
    if any(r[1] == "FAIL" for r in results):
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
            if ICH_ENABLED:
                r15 = get_raw_tf(sym, ICH_TF_MINUTES)
                print(f"  M{ICH_TF_MINUTES}: {len(r15)} bar | terakhir {r15.index[-1]:%d %b %H:%M} WIB (butuh >= 90 bar untuk Ichimoku)")
        except Exception as e:
            print("  GAGAL ->", e)


def run_loop(a, sent, book, capital):
    print("Mode loop aktif. Ctrl+C untuk berhenti.")
    if tg_ready():
        threading.Thread(target=command_listener, args=(book, capital), daemon=True).start()
    while True:
        try:
            scan_once(False, sent, book, capital)
        except Exception as e:
            print("loop error:", e)
        time.sleep(60 - time.time() % 60 + 3)                    # +3 dtk agar bar sudah final


def main():
    ap = argparse.ArgumentParser(description="Bybit Ichimoku M15 (Fakeout/AMD + Break S/R cloud, limit bertingkat) -> Telegram")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--demo", action="store_true")
    g.add_argument("--check", action="store_true", help="tes koneksi Bybit, pair, tick, harga + offset")
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--self-test", action="store_true", help="uji engine ICH + chart + tracker secara lokal tanpa Telegram")
    ap.add_argument("--send", action="store_true", help="dengan --demo: kirim ke Telegram")
    ap.add_argument("--bars", type=int, default=3, help="(tidak dipakai lagi; dipertahankan agar start command lama tetap jalan)")
    ap.add_argument("--all", action="store_true", help="sertakan sinyal ICH lama/yang di luar batas (tes; tidak dicatat sebagai trade)")
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
        print(f"Selesai. {scan_once(a.all, sent, book, capital)} sinyal baru."); return
    run_loop(a, sent, book, capital)


if __name__ == "__main__":
    main()
