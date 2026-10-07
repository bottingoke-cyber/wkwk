# Bybit Combo XAU/BTC + Admin Capital Tracker

Bot ini adalah **signal/paper-accounting bot**: mengambil candle publik Bybit, mengirim sinyal Telegram, melacak TP/SL/TIMEOUT, lalu menghitung perubahan modal simulasi. Bot ini **tidak mengirim order live** ke Bybit.

## Fitur yang ditambahkan

- Modal awal default **Rp1.000.000**.
- Setiap entry dicatat **0.01 lot** (env `TRADE_LOT`).
- TP menambah saldo; SL mengurangi saldo; TIMEOUT dihitung sebagai realized P/L juga.
- Jika saldo setelah settlement `<= 0`, bot otomatis menambahkan **Rp500.000** (env `CAPITAL_TOPUP_IDR`) dan mencatat top-up.
- Rekap mingguan otomatis **Sabtu 05:00 WIB** (dapat diubah lewat `WEEKLY_RECAP_HOUR/MINUTE`) ke topic EVENTS.
- `/capital` dan `/rekap` hanya untuk admin.
- Semua command dari member biasa **dihapus otomatis**.
- Service message `new_chat_members` / `left_chat_member` juga dihapus otomatis.
- Data state disimpan di `DATA_DIR` agar bisa dipersistenkan di Railway.

## Penting soal P/L

Perhitungan modal adalah **paper bookkeeping**, bukan nilai broker live. Default kalkulasi memakai asumsi `USD_IDR=16000`, XAU `100 units/lot`, BTC `1 unit/lot`. Ubah env tersebut sesuai spesifikasi instrumen yang benar-benar kamu pakai.

## Jalankan lokal

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# isi token, chat ID, admin ID, dan variabel lain
set -a; source .env; set +a
python bybit_combo_bot.py --self-test
python bybit_combo_bot.py --loop
```

## Deploy ke Railway

1. Push folder ini ke GitHub repository.
2. Railway → **New Project** → **Deploy from GitHub repo** → pilih repository.
3. Tambahkan environment variables yang ada di `.env.example`.
4. Buat Volume dan mount ke `/data`.
5. Set `DATA_DIR=/data`.
6. Pastikan service start command:
   `python bybit_combo_bot.py --loop`
7. Deploy lalu cek Logs.

Tanpa volume, file state lokal container bisa hilang saat redeploy/restart. Volume Railway membuat direktori mount tetap persisten.

## Telegram setup

Bot harus menjadi admin di grup/supergroup dan diberi hak **Delete Messages**, karena bot perlu menghapus command member dan service message join/leave. `TELEGRAM_ADMIN_IDS` tetap menentukan siapa yang boleh memakai command bot; status admin Telegram saja tidak otomatis memberi akses command pada semua admin.

## Command admin

- `/status`
- `/capital`
- `/rekap`
- `/offset`
- `/offset XAUUSD -4`
- `/engines`
- `/topics`
- `/whereami`
- `/help`

## Topic routing

- `TELEGRAM_TOPIC_MOMENTUM` → A
- `TELEGRAM_TOPIC_LEGACY_FAKEOUT` → B
- `TELEGRAM_TOPIC_ACCUM_EXP` → C
- `TELEGRAM_TOPIC_SHADOW_FAKEOUT_1M` → SF
- `TELEGRAM_TOPIC_EVENTS` → TP/SL/TIMEOUT + rekap
