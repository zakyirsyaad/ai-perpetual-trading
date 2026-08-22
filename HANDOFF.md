# Handoff — AI Trading Bot (2026-06-13)

Catatan untuk codex / sesi berikutnya. File ini boleh dihapus setelah dibaca.

## Yang dikerjakan sesi ini (commit `0794e06`)
1. **Confluence selektif** (`confluence.py` BARU + wiring di `volume_strategy.py`): ganti
   LONG/SHORT lempar-koin jadi arah dari konsensus 86 faktor, gate agreement+ATR, TP/SL skala ATR.
2. **Migrasi AI** dari xAI Grok (kredit habis) ke **freemodel.dev / gpt-5.4**
   (`brain.py` + `config.py`): `XAI_API_STYLE=chat` → OpenAI /chat/completions + json_object.
3. `realtime_backtest.py` (BARU): jalur `--strategy ai_volume` pakai confluence.

## Status: AI BERFUNGSI, profit BELUM terbukti
- gpt-5.4 jalan end-to-end di demo, reasoning cerdas, HOLD saat pasar jelek. Bagus.
- TAPI belum pernah lihat entry+close. Profit cuma bisa divalidasi lewat **demo berjalan** (bukan backtest — AI tak bisa di-backtest).
- Batas struktural: fee taker 0.05% terkunci → edge cuma nutup ~30-96% fee tergantung window. Net-positif konsisten di luar jangkauan strategi otomatis.

## TODO codex/VPS
1. Commit 31 file codex (terpisah dari `0794e06`).
2. Sync `0794e06` + kerjaan codex ke VPS.
3. **Update `.env` VPS manual** (gitignored — TIDAK ikut git):
   ```
   XAI_API_KEY=<set-on-vps-env>
   XAI_BASE_URL=https://api.freemodel.dev/v1
   XAI_MODEL=gpt-5.4
   XAI_API_STYLE=chat
   AI_KLINE_INTERVAL=3m
   POSITION_HOLD_MINUTES=60
   VOLUME_BREAKEVEN_EXIT_ENABLED=false
   VOLUME_EXECUTION_MODE=market
   CONFLUENCE_ENABLED=true
   CONFLUENCE_MIN_AGREEMENT=0.55
   CONFLUENCE_MIN_ATR_PCT=0.05
   CONFLUENCE_MIN_SCORE=0.05
   CONFLUENCE_ATR_TP_MULT=3.0
   CONFLUENCE_ATR_SL_MULT=1.0
   ```
4. **Migrasi DB**: jalankan 3 ALTER TABLE di `supabase/schema.sql:21-23` ke Supabase
   (tambah confidence/factor_metadata/trigger_metadata ke tabel `trades`).
   Tanpa ini, simpan trade tertutup GAGAL ("column trades.confidence does not exist").
5. Biarkan demo jalan di VPS, kumpulkan `ai_decisions` + `trades`, analisis setelah beberapa hari.

## Gotcha penting
- `VOLUME_BREAKEVEN_EXIT_ENABLED=false` WAJIB (kalau true, nutup winner di breakeven → gross/fee anjlok 37%→1%).
- Timeframe 3m (1m terlalu noisy, 5m nggak stabil).
- VPS = SATU-SATUNYA instance live (hindari dobel order/DB).
