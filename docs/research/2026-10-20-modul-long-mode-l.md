# Mode L dan pembanding S: latihan dan ujian sekali jalan

Tanggal: 20 Oktober 2026 · Spec: `docs/superpowers/specs/2026-10-18-long-mirror-live-exit-design.md`
· Pertanyaan tambahan: apakah SHORT hanya di kondisi TURUN (aturan entry bot live) punya keunggulan?

## Ringkasan

- **Mode L GAGAL di kedua versi.** Ujian sekali jalan (5 Agustus – 13 Oktober 2026):
  - **versi i:** 147 posisi, −212 per 1 juta, −243 tanpa 3 teratas;
  - **versi ii:** 152 posisi, −241 per 1 juta, −271 tanpa 3 teratas.

  Syarat lolos: lebih baik dari −150, dan tanpa 3 teratas lebih baik dari −250. Versi i memenuhi
  syarat kedua tapi tidak syarat pertama.
- **SHORT di kondisi TURUN (pembanding S) tidak punya keunggulan yang bertahan.**
  - Latihan: −328 per 1 juta (942 posisi). Ujian: **−515** (156 posisi).
  - Sebelum fee dan slippage hasilnya +53 per 1 juta di latihan dan −130 di ujian.
  - Hanya 1 dari 12 bulan latihan yang positif.
  - Jadi gate "SHORT hanya saat TURUN" dengan aturan entry dekat rata-rata tidak cukup untuk
    mendekati −150.
- **Fee tetap penghalang utama.** Fee 0,035% per kaki = 350 per 1 juta. Untuk mencapai −150,
  gross harus sekitar +200 per 1 juta. Tidak ada baris yang mencapainya secara konsisten.
  Gross L (i) di ujian +169, tapi di latihan −58, jadi angka ujian itu belum bisa dipercaya.
- **Potong rugi (versi ii) tidak konsisten.** Di latihan versi ii sedikit membantu L (−399 lawan
  −440), tapi di ujian lebih buruk di kedua baris (L −241 lawan −212, S −526 lawan −515). Win rate
  juga turun jauh (di ujian: L 54,6% → 31,9%, S 36,9% → 18,8%).

## Cara membaca

- **Mode L:** LONG saat kondisi NAIK, ketika harga dekat rata-rata (pemicu 3m bot live: jarak ke
  EMA21 paling jauh 0,75 × ATR14), dengan exit bot live.
  - Stop: `min(max(0,28%, 1,5 × ATR 3m), 0,40%)`.
  - TP: level net +56 USDT.
  - Batas waktu: 60 menit.
  - Versi ii: ditambah potong rugi 0,10% mulai menit ke-10.
- **Baris S:** aturan yang sama sebagai SHORT saat kondisi TURUN, tanpa status lolos.
- **Biaya:**
  - slippage entry 0,3 bps, exit pasar 0,5 bps, TP tepat di level;
  - fee 0,035% per kaki.
- **Per 1 juta** = 1.000.000 × jumlah PnL bersih / jumlah volume. **±** = satu standar error.
- **Kondisi pasar:** SMA20/SMA50 dan ATR14 di candle 1 jam, EMA50 di candle 4 jam (pemilih mode dokumen).

## Hasil

| Baris | Tahap | Posisi | Entry per hari kalender | Menang | Per 1 juta | ± | Gross per 1 juta | TP / SL / potong / waktu |
|---|---|---|---|---|---|---|---|---|
| L (i) | Latihan | 705 | 1,91 | 43,8% | −440 | 50 | −58 | 213 / 273 / 0 / 219 |
| L (ii) | Latihan | 719 | 1,95 | 26,2% | −399 | 39 | −15 | 172 / 58 / 427 / 62 |
| S (i) | Latihan | 942 | 2,55 | 49,8% | −328 | 46 | +53 | 353 / 356 / 0 / 233 |
| S (ii) | Latihan | 962 | 2,61 | 31,7% | −336 | 37 | +46 | 286 / 99 / 517 / 60 |
| L (i) | Ujian | 147 | 2,10 | 54,6% | −212 | 108 | +169 | 53 / 44 / 0 / 50 |
| L (ii) | Ujian | 152 | 2,17 | 31,9% | −241 | 85 | +142 | 43 / 5 / 87 / 17 |
| S (i) | Ujian | 156 | 2,23 | 36,9% | −515 | 97 | −130 | 31 / 53 / 0 / 72 |
| S (ii) | Ujian | 162 | 2,31 | 18,8% | −526 | 78 | −140 | 26 / 20 / 92 / 24 |
| Tidak trading | Keduanya | 0 | 0 | – | 0 | – | – | – |

Latihan: 1 Agustus 2025 – 4 Agustus 2026 (369 hari kalender, 127 hari NAIK, 172 hari TURUN).
Ujian: 5 Agustus – 13 Oktober 2026 (70 hari kalender, 32 hari NAIK, 32 hari TURUN).

### S (i) per bulan, latihan dan ujian (per 1 juta)

Latihan:

| 2025-08 | 2025-09 | 2025-10 | 2025-11 | 2025-12 | 2026-01 |
|---|---|---|---|---|---|
| −388 | −387 | +21 | −160 | −542 | −473 |

| 2026-02 | 2026-03 | 2026-04 | 2026-05 | 2026-06 | 2026-07 |
|---|---|---|---|---|---|
| −271 | −421 | −509 | −174 | −316 | −369 |

Ujian:

| 2026-08 | 2026-09 | 2026-10 |
|---|---|---|
| −604 | −478 | −517 |

## Kaitan dengan bot live

- **Analisis kerugian bot live** (`docs/research/2026-10-20-analisis-kerugian-trade.md`)
  menemukan 17 trade SHORT di kondisi TURUN hampir impas (−97 per 1 juta, gross +253). Entry di
  sana dipilih AI, dan sampelnya kecil (± 297). Backtest aturan entry yang sama tanpa AI, dengan
  ratusan posisi, tidak menunjukkan keunggulan. Gate TURUN saja tidak cukup.
- **Kecepatan entry.** Bot live sekitar 5,7 trade per hari. Mode L dan S masing-masing sekitar
  2 per hari kalender, karena hanya aktif di satu kondisi pasar.

## Sensitivitas biaya Mode A (rescore)

Log Mode A dihitung ulang dengan biaya di atas, dengan kejadian exit yang sama. Ini sensitivitas
biaya, bukan uji.

| Log | Posisi | Per 1 juta lama | Per 1 juta baru |
|---|---|---|---|
| Mode A ujian (5m) | 94 | −546 | −481 |
| Pembanding D ujian | 150 | −483 | −414 |
| Mode A latihan (15m) | 333 | −567 | −501 |
| Mode A latihan (5m) | 479 | −472 | −405 |
| Pembanding D latihan | 718 | −427 | −357 |

Biaya yang lebih kecil memperbaiki sekitar 65–70 per 1 juta, tetapi tidak ada log yang mendekati −150.

## Batasan

- Exit AI tidak bisa di-backtest. Mode L memakai exit aturan saja (stop, TP, waktu, dan potong rugi
  di versi ii). Bot live menutup sebagian besar posisinya lewat AI.
- Candle produksi, bukan VST. Harga VST hampir sama dengan produksi.
- TP di-backtest tepat di level. Di bot live, poll 5 detik kadang memberi harga lebih baik.
- Ujian ini sekali jalan dan sudah terpakai: lock, start record dan marker menolak ujian ulang.

## Rekaman

- Lock `mode_L_BTC-USDT_lock.json`, sha256
  `174def0480ad85524b4e157f9d2d0b88fe2195533d5f0ffc952547fe0457bb2a`.
- Marker `holdout_runs/mode_L_BTC-USDT_174def0480ad85524b4e157f9d2d0b88fe2195533d5f0ffc952547fe0457bb2a.json`.
- Data ujian dibaca 441 berkas, manifest
  `f790d888179f0e8973eaee406eaa43cd9cdfac85c401313e758c660df9763027`. Hari ujian saja 70 berkas,
  manifest `8ecd002850c49afe6af819afb61ec68e1ad7e1af3ee91cf63f0808a396855377`.
- Kode di commit `95e61b7`, Python 3.10.
- `verify-copy` dijalankan sebelum latihan dan sebelum ujian: 0 masalah.
