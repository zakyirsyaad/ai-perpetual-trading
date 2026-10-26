> **Catatan 26 Oktober 2026 (fee):** fee akun ternyata 0,05% per sisi sejak 20 Oktober malam; dokumen ini memakai 0,035%. Hasil nyata dan angka gross (bps) tidak berubah, tetapi hitungan ke depan: hasil per 1 juta = 50 × gross − 500, sehingga −150 butuh +7 bps (bukan +4) dan impas butuh +10 bps (bukan +7). Lihat `2026-10-26-pembaruan-bot-akun-1-v2.md`.

# Analisis trade sungguhan akun 1: penyebab rugi dan rencana perbaikan

Tanggal: 21 Oktober 2026 · Data: 519 trade sungguhan akun 1 (31 Agustus – 20 Oktober 2026), 6 trade akun 2, dan 1.084 keputusan AI. Semua dari hasil forward, bukan backtest.

## Ringkasan

- **Oktober jauh lebih baik dari September:** −282 per 1 juta, dibanding −838.
- **Target −150 masih butuh tambahan sekitar +2,5 bps per trade** di sisi gross (sebelum fee).
- **LONG pilihan AI rugi di setiap periode.** SHORT yang dekat rata-rata sedikit positif, dan SHORT saat kondisi TURUN paling baik.
- **Exit AI sedikit merugikan** (sekitar −100 per 1 juta), jadi sudah dimatikan sejak 21 Oktober 00:18 WIB.
- **Input AI bermasalah di beberapa tempat** (sudah dicek di kode). Perbaikannya disusun bertahap, dan setiap tahap diukur dari hasil forward.

## Gambaran besar

| Periode akun 1 | Trade | Per 1 juta | Gross per 1 juta | Fee per 1 juta |
|---|---|---|---|---|
| September (dua arah, fee 0,05%) | 413 | −838 | −344 | 495 |
| Oktober (hanya SHORT, gate stretch, fee 0,035%) | 106 | −282 | +75 | 357 |

Dengan fee 0,035% per kaki, hasil per 1 juta = 50 × gross per trade (bps) − 350. Target −150 butuh gross +4 bps per trade, dan impas butuh +7 bps.

## Entry: arah dan posisi harga

| BTC, akun 1 | Trade | Gross per trade (bps) | Per 1 juta |
|---|---|---|---|
| LONG, dekat rata-rata | 101 | −4,7 | −585 |
| LONG, mengejar harga | 82 | −7,0 | −700 |
| SHORT, mengejar harga | 93 | −8,0 | −750 |
| SHORT, dekat rata-rata | 175 | +2,0 | −250 |
| SHORT saat TURUN (Oktober) | 58 | +6,1 | −46 |

- **Mengejar harga adalah kesalahan terbesar.** Gate stretch yang aktif sejak Oktober sudah memblokirnya.
- **LONG pilihan AI selalu rugi.** LONG yang diblokir sejak 10 Oktober juga negatif kalau seandainya dijalankan.
- **Confidence AI tidak bermakna.** Trade dengan confidence ≥ 0,78 justru lebih buruk.

## Exit

- Stop loss rata-rata −46,7 bps (56 trade). Neutral exit rata-rata +34,1 bps (63 trade).
- Exit AI menutup 65% trade, rata-rata −7,2 bps.
- Pada entry yang sama, exit aturan (neutral +56, stop, 60 menit) lebih baik **+2,1 ± 1,0 bps** per trade, sekitar +100 per 1 juta. Kerugian terbesar dari exit AI ada di 10 menit pertama.
- Karena itu exit AI di akun 1 dimatikan sejak 21 Oktober 00:18 WIB.

## Penyebab dari sisi kode AI

1. **Target AI saling bertentangan.** Prompt membolehkan HOLD, tapi payload menyuruh "buka trade setiap tick, jangan diam".
2. **AI tidak tahu aturan mainnya.**
   - Ia ditawari LONG walaupun bot hanya boleh SHORT.
   - Ia tidak diberi tahu soal gate stretch.
   - Batas confidence 0,75 terlihat olehnya, sehingga jawabannya selalu 0,75–0,77.
3. **AI tidak menerima konteks 1 jam atau 4 jam.** Semua datanya dari candle 3 menit.
4. **Data faktor rusak.**
   - 81 dari 86 faktor sebenarnya rumus yang sama.
   - Faktor tren selalu mentok di ±1 dan berbalik bersamaan.
   - Faktor volatilitas selalu memberi suara LONG.
5. **"Pelajaran" hanya berupa teks di prompt, jadi tidak mengikat.** Setelah pelajaran itu muncul, entry yang mengejar harga justru naik dari 57% ke 72%.

## Kenapa skrip Pine bagus di TradingView tapi gagal di kita

- Sinyal Pine dengan angka aslinya sudah diuji pada 570 trade BTC: win rate 50,8%, gross −0,33 bps. Sinyalnya sendiri tidak punya keunggulan.
- Hasil bagus di TradingView kemungkinan besar datang dari dua hal:
  - **Trailing stop 0,08% dengan jarak 0,01%**, ditambah pengisian order dari high dan low candle yang tidak bisa didapat di eksekusi nyata;
  - **Fee 0,02% di skrip**, padahal fee kita 0,035% per kaki. Dengan fee kita, trade yang menang sekitar +0,07% habis dimakan fee.
- Untuk memastikannya, dibutuhkan skrip asli dan pengaturan *Properties* di Strategy Tester.

## Rencana perbaikan bertahap

| Tahap | Isi | Status |
|---|---|---|
| 0 | Matikan exit AI, pakai exit aturan | Berjalan sejak 21 Okt |
| 1 | Gate dua arah: SHORT hanya saat TURUN, LONG saat NAIK berjalan di mode shadow; ledger pembelajaran juga di mode shadow | Kode sedang dibuat |
| 2 | Input AI yang jujur: tanpa target "setiap tick", ada info gate dan kondisi 1 jam/4 jam, faktor rusak diganti | Kode sedang dibuat |
| 3 | Ledger memblokir kelompok setup yang terbukti rugi (60 sampel, di bawah +4 bps) | Kode sedang dibuat |

- Setiap tahap diukur dulu pada sekitar 60 trade, kira-kira 2–4 minggu, sebelum tahap berikutnya dimulai.
- **LONG baru ditradingkan** kalau hasil shadow-nya mencapai 60 sampel dengan gross ≥ +4 bps.
- **Konsekuensi:** jumlah trade akan turun sekitar setengah dari Oktober, sehingga volume makin jauh dari 1 juta per hari. Ini sengaja dipilih demi hasil per 1 juta yang lebih baik.

## Batasan

- Angka per kelompok berasal dari sampel kecil. Ketidakpastian sekitar ±2–3 bps.
- Perbandingan exit adalah hitungan "bagaimana kalau" pada entry dan harga yang sungguh terjadi.
- Harga VST di September sempat menyimpang dari harga produksi. Karena itu angka Oktober yang paling bisa dipercaya.
