# Laporan evaluasi gerbang Pine (Fase 1)

Evaluasi offline atas data riwayat; tidak ada order yang ditempatkan dan bot tidak diubah.

- sha256 spec: `fd77d36af66167f071ec597efb9989d397c12b087202f7eab96b6f85aed0898c`
- sha256 data: `f26d7dda1b26d1794aa5990a1fb575f3fddba62248933ef08b2b67b37ee54534`
- sha256 data_holdout: `d426d975544d90513ca3e97490869ef624ba461b00b46b636931fe297adfd820`
- sha256 selection: `3687376145f2f111c976ed78b50e25b7e58be07308615387a7d3cdcee7a9e310`
- sha256 holdout: `3459341410e5694a455594304baecf5966a7d362c298a4845069116f29c05702`
- sha256 code: `e915262310aeea70e183db9b518d5a2bd5d2d33ca6eff0951b3cc559d272d0c5`

## Ringkasan

Tidak ada kontender yang lulus gerbang; bot tidak diubah dan tabel WR terhadap pasokan per kontender dan simbol ada di bawah.

Rumus: net per 1M = 50 × (gross bps − 7) pada fee 0.035% per sisi. Acuan: bot live saat ini -190 per 1M volume.

| Kontender | Simbol | WR | net/1M | transaksi/hari | gerbang |
|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 48.1% | -399 | 9.0 | gagal |
| Kontender 1 (struktur Pine) | ETH-USDT | 52.17% | -337 | 3.8 | gagal |
| Kontender 2 (logika bot) | BTC-USDT | 47.61% | -429 | 13.0 | gagal |
| Kontender 2 (logika bot) | ETH-USDT | 50.71% | -366 | 6.2 | gagal |
| Kontender 3 (meta-labeling) | BTC-USDT | 47.57% | -420 | 8.1 | gagal |
| Kontender 3 (meta-labeling) | ETH-USDT | 56.84% | -247 | 3.9 | gagal |

Lulus gerbang berarti "edge ada dan konsisten dengan target", bukan bukti WR 60%: dengan sekitar 250–500 transaksi per simbol, WR sebenarnya 60% punya interval 95% sekitar 54–66%. Keputusan 60% dibuat per simbol setelah sekitar 600 transaksi.

## Integritas data dan sha256

| Berkas | sha256 |
|---|---|
| spec | `fd77d36af66167f071ec597efb9989d397c12b087202f7eab96b6f85aed0898c` |
| data | `f26d7dda1b26d1794aa5990a1fb575f3fddba62248933ef08b2b67b37ee54534` |
| data_holdout | `d426d975544d90513ca3e97490869ef624ba461b00b46b636931fe297adfd820` |
| selection | `3687376145f2f111c976ed78b50e25b7e58be07308615387a7d3cdcee7a9e310` |
| holdout | `3459341410e5694a455594304baecf5966a7d362c298a4845069116f29c05702` |
| code | `e915262310aeea70e183db9b518d5a2bd5d2d33ca6eff0951b3cc559d272d0c5` |
| kode di file seleksi | `e915262310aeea70e183db9b518d5a2bd5d2d33ca6eff0951b3cc559d272d0c5` |
| kode di file uji | `e915262310aeea70e183db9b518d5a2bd5d2d33ca6eff0951b3cc559d272d0c5` |

| Pemeriksaan | Hasil |
|---|---|
| spec di file seleksi = spec saat ini | cocok |
| spec di file uji = spec saat ini | cocok |
| file seleksi yang dipakai uji = file seleksi | cocok |
| kode file seleksi = kode file uji | cocok |
| kode saat ini = kode file uji | cocok |
| evaluasi holdout dimulai (holdout_runs) | ya |

| Parameter | Nilai |
|---|---|
| s* (overshoot stop, bps) | 1.00 |
| jumlah stop untuk s* | 13 |
| jumlah sampel latensi | 1903 |
| seed | 0 |
| data dimuat sampai (ms UTC) | 1785866400000 |
| periode uji (ms UTC) | 1785862800000 sampai 1791910800000 |

## Langkah 0 (sebaran rezim)

Sebaran naik/turun/netral per sumber tren dan bulan WIB, dicuplik di tiap penutupan bar 5m. Sel dengan n kurang dari 60 ditandai "tidak cukup", bukan "gagal".

| Simbol | Tren | Bulan | naik | turun | netral | n | Status |
|---|---|---|---|---|---|---|---|
| BTC-USDT | E200 | 2025-08 | 44.4% | 55.6% | 0.0% | 7328 | cukup |
| BTC-USDT | E200 | 2025-09 | 54.0% | 46.0% | 0.0% | 8640 | cukup |
| BTC-USDT | E200 | 2025-10 | 55.4% | 44.6% | 0.0% | 8928 | cukup |
| BTC-USDT | E200 | 2025-11 | 44.6% | 55.4% | 0.0% | 8640 | cukup |
| BTC-USDT | E200 | 2025-12 | 49.5% | 50.5% | 0.0% | 8928 | cukup |
| BTC-USDT | E200 | 2026-01 | 48.1% | 51.9% | 0.0% | 8928 | cukup |
| BTC-USDT | E200 | 2026-02 | 43.3% | 56.7% | 0.0% | 8064 | cukup |
| BTC-USDT | E200 | 2026-03 | 47.8% | 52.2% | 0.0% | 8928 | cukup |
| BTC-USDT | E200 | 2026-04 | 54.2% | 45.8% | 0.0% | 8640 | cukup |
| BTC-USDT | E200 | 2026-05 | 51.8% | 48.2% | 0.0% | 8928 | cukup |
| BTC-USDT | E200 | 2026-06 | 42.9% | 57.1% | 0.0% | 8640 | cukup |
| BTC-USDT | E200 | 2026-07 | 57.7% | 42.3% | 0.0% | 8928 | cukup |
| BTC-USDT | E200 | 2026-08 | 43.4% | 56.6% | 0.0% | 1152 | cukup |
| BTC-USDT | H1 | 2025-08 | 34.9% | 53.8% | 11.3% | 8928 | cukup |
| BTC-USDT | H1 | 2025-09 | 45.1% | 36.8% | 18.1% | 8640 | cukup |
| BTC-USDT | H1 | 2025-10 | 48.5% | 44.1% | 7.4% | 8928 | cukup |
| BTC-USDT | H1 | 2025-11 | 25.3% | 56.1% | 18.6% | 8640 | cukup |
| BTC-USDT | H1 | 2025-12 | 36.7% | 44.9% | 18.4% | 8928 | cukup |
| BTC-USDT | H1 | 2026-01 | 34.0% | 48.9% | 17.1% | 8928 | cukup |
| BTC-USDT | H1 | 2026-02 | 24.9% | 59.7% | 15.5% | 8064 | cukup |
| BTC-USDT | H1 | 2026-03 | 46.9% | 38.3% | 14.8% | 8928 | cukup |
| BTC-USDT | H1 | 2026-04 | 58.3% | 27.6% | 14.0% | 8640 | cukup |
| BTC-USDT | H1 | 2026-05 | 40.2% | 44.4% | 15.5% | 8928 | cukup |
| BTC-USDT | H1 | 2026-06 | 30.8% | 53.2% | 16.0% | 8640 | cukup |
| BTC-USDT | H1 | 2026-07 | 54.4% | 29.7% | 15.9% | 8928 | cukup |
| BTC-USDT | H1 | 2026-08 | 21.9% | 63.5% | 14.6% | 1152 | cukup |
| BTC-USDT | H4 | 2025-08 | 23.1% | 64.7% | 12.2% | 8928 | cukup |
| BTC-USDT | H4 | 2025-09 | 45.0% | 34.9% | 20.1% | 8640 | cukup |
| BTC-USDT | H4 | 2025-10 | 45.2% | 44.8% | 10.1% | 8928 | cukup |
| BTC-USDT | H4 | 2025-11 | 7.4% | 87.1% | 5.6% | 8640 | cukup |
| BTC-USDT | H4 | 2025-12 | 16.5% | 32.8% | 50.7% | 8928 | cukup |
| BTC-USDT | H4 | 2026-01 | 44.6% | 36.2% | 19.2% | 8928 | cukup |
| BTC-USDT | H4 | 2026-02 | 0.0% | 93.5% | 6.5% | 8064 | cukup |
| BTC-USDT | H4 | 2026-03 | 41.4% | 31.2% | 27.4% | 8928 | cukup |
| BTC-USDT | H4 | 2026-04 | 73.9% | 18.2% | 7.9% | 8640 | cukup |
| BTC-USDT | H4 | 2026-05 | 37.6% | 49.6% | 12.8% | 8928 | cukup |
| BTC-USDT | H4 | 2026-06 | 11.1% | 79.4% | 9.4% | 8640 | cukup |
| BTC-USDT | H4 | 2026-07 | 65.6% | 17.2% | 17.2% | 8928 | cukup |
| BTC-USDT | H4 | 2026-08 | 0.0% | 100.0% | 0.0% | 1152 | cukup |
| ETH-USDT | E200 | 2025-08 | 60.0% | 40.0% | 0.0% | 7328 | cukup |
| ETH-USDT | E200 | 2025-09 | 46.2% | 53.8% | 0.0% | 8640 | cukup |
| ETH-USDT | E200 | 2025-10 | 52.4% | 47.6% | 0.0% | 8928 | cukup |
| ETH-USDT | E200 | 2025-11 | 47.2% | 52.8% | 0.0% | 8640 | cukup |
| ETH-USDT | E200 | 2025-12 | 51.0% | 49.0% | 0.0% | 8928 | cukup |
| ETH-USDT | E200 | 2026-01 | 50.2% | 49.8% | 0.0% | 8928 | cukup |
| ETH-USDT | E200 | 2026-02 | 44.6% | 55.4% | 0.0% | 8064 | cukup |
| ETH-USDT | E200 | 2026-03 | 47.1% | 52.9% | 0.0% | 8928 | cukup |
| ETH-USDT | E200 | 2026-04 | 49.2% | 50.8% | 0.0% | 8640 | cukup |
| ETH-USDT | E200 | 2026-05 | 47.6% | 52.4% | 0.0% | 8928 | cukup |
| ETH-USDT | E200 | 2026-06 | 42.0% | 58.0% | 0.0% | 8640 | cukup |
| ETH-USDT | E200 | 2026-07 | 59.4% | 40.6% | 0.0% | 8928 | cukup |
| ETH-USDT | E200 | 2026-08 | 41.6% | 58.4% | 0.0% | 1152 | cukup |
| ETH-USDT | H1 | 2025-08 | 44.1% | 37.2% | 18.7% | 8928 | cukup |
| ETH-USDT | H1 | 2025-09 | 30.3% | 50.0% | 19.7% | 8640 | cukup |
| ETH-USDT | H1 | 2025-10 | 48.1% | 41.4% | 10.5% | 8928 | cukup |
| ETH-USDT | H1 | 2025-11 | 28.1% | 53.9% | 18.1% | 8640 | cukup |
| ETH-USDT | H1 | 2025-12 | 38.3% | 45.4% | 16.3% | 8928 | cukup |
| ETH-USDT | H1 | 2026-01 | 44.8% | 44.0% | 11.3% | 8928 | cukup |
| ETH-USDT | H1 | 2026-02 | 24.3% | 61.5% | 14.3% | 8064 | cukup |
| ETH-USDT | H1 | 2026-03 | 48.5% | 38.0% | 13.4% | 8928 | cukup |
| ETH-USDT | H1 | 2026-04 | 45.6% | 37.8% | 16.7% | 8640 | cukup |
| ETH-USDT | H1 | 2026-05 | 28.0% | 50.8% | 21.2% | 8928 | cukup |
| ETH-USDT | H1 | 2026-06 | 31.5% | 53.3% | 15.1% | 8640 | cukup |
| ETH-USDT | H1 | 2026-07 | 56.3% | 27.2% | 16.5% | 8928 | cukup |
| ETH-USDT | H1 | 2026-08 | 0.0% | 67.7% | 32.3% | 1152 | cukup |
| ETH-USDT | H4 | 2025-08 | 58.5% | 28.6% | 12.9% | 8928 | cukup |
| ETH-USDT | H4 | 2025-09 | 25.6% | 62.8% | 11.7% | 8640 | cukup |
| ETH-USDT | H4 | 2025-10 | 34.4% | 54.8% | 10.8% | 8928 | cukup |
| ETH-USDT | H4 | 2025-11 | 7.4% | 86.5% | 6.1% | 8640 | cukup |
| ETH-USDT | H4 | 2025-12 | 32.7% | 41.9% | 25.4% | 8928 | cukup |
| ETH-USDT | H4 | 2026-01 | 45.7% | 35.6% | 18.7% | 8928 | cukup |
| ETH-USDT | H4 | 2026-02 | 3.6% | 90.3% | 6.1% | 8064 | cukup |
| ETH-USDT | H4 | 2026-03 | 39.2% | 24.3% | 36.4% | 8928 | cukup |
| ETH-USDT | H4 | 2026-04 | 58.9% | 8.9% | 32.2% | 8640 | cukup |
| ETH-USDT | H4 | 2026-05 | 11.8% | 67.2% | 21.0% | 8928 | cukup |
| ETH-USDT | H4 | 2026-06 | 11.1% | 72.2% | 16.7% | 8640 | cukup |
| ETH-USDT | H4 | 2026-07 | 83.3% | 6.3% | 10.3% | 8928 | cukup |
| ETH-USDT | H4 | 2026-08 | 0.0% | 80.2% | 19.8% | 1152 | cukup |

## Tahap latih (grid, 5 teratas, PBO, SPA)

- Kontender 1 terpilih: `H4|P40|ADX|OFF|OFF`; aturan primer kontender 3: `H4|P40|OFF|OFF|OFF`.
- Aturan dinilai: 61; stabil: ya; pasokan membatasi: tidak.
- PBO (CSCV, aturan dengan ≥ 1.000 TP+SL): 0.01 (lulus jika < 0.5: ya).
- SPA p: 0.000 (2000 replikasi; nilai arah vs sisi acak pada entri yang sama).
- Purge waktu tick sebelum tiap batas blok: 75 menit (time stop 60 menit + latensi terburuk kalibrasi, jeda order dan satu bar 1m).

Lima aturan teratas pada data latih (WR, net/1M, pasokan):

| Aturan | n TP+SL | WR | batas bawah | gross bps | net/1M | transaksi/hari | memenuhi syarat |
|---|---|---|---|---|---|---|---|
| H4/P40/ADX/OFF/OFF | 3685 | 50.50% | 48.9% | -0.44 | -372 | 14.6 | ya |
| H1/P40/ADX/OFF/OFF | 3640 | 50.36% | 48.9% | -0.47 | -374 | 14.5 | ya |
| H1/P35/OFF/OFF/OFF | 2824 | 50.60% | 48.8% | -0.34 | -367 | 11.2 | ya |
| H4/B12/ADX/OFF/OFF | 2990 | 49.83% | 48.0% | -0.70 | -385 | 11.8 | ya |
| H4/P35/ADX/OFF/OFF | 2725 | 49.58% | 47.76% | -0.75 | -387 | 10.7 | ya |

C0 pada data latih wajar (50 ± 3% WR, |gross| ≤ 2 bps).

| C0 pada data latih | WR | gross bps | n |
|---|---|---|---|
| BTC-USDT long | 48.9% | -1.00 | 5174 |
| BTC-USDT short | 50.19% | -0.48 | 5180 |
| ETH-USDT long | 48.9% | -1.06 | 5120 |
| ETH-USDT short | 49.06% | -0.99 | 5156 |

## Model meta-labeling (log-loss, kalibrasi)

- C terpilih: 10.000; ambang probabilitas: 0.4945; kandidat: 22258; prediksi di luar sampel: 16488.
- pasokan pada ambang (trade/hari, B2–B4): luar sampel 10.0; model akhir 12.3.

Log-loss validasi per lipatan (jendela memanjang B1–B4); rata-rata dibobot ukuran lipatan, sama dengan pemilihan C:

| C | log-loss per lipatan | rata-rata |
|---|---|---|
| 0.1 | 0.6939, 0.6929, 0.6930 | 0.6932 |
| 1.0 | 0.6941, 0.6924, 0.6927 | 0.6930 |
| 10.0 | 0.6955, 0.6912, 0.6920 | 0.6928 |
| 100.0 | 0.6965, 0.6907, 0.6918 | 0.6929 |
| 1000.0 | 0.6967, 0.6906, 0.6918 | 0.6929 |

Kalibrasi (prediksi vs frekuensi teramati):

| rata-rata prediksi | frekuensi teramati | n |
|---|---|---|
| 0.483 | 0.483 | 11735 |
| 0.510 | 0.525 | 4753 |

## Periode uji per kontender dan simbol

WR = TP / (TP + SL); time stop dilaporkan terpisah. Gross dihitung atas semua transaksi. Acuan bot live: -190 per 1M. Entri diisi pada harga penutupan bar 1m yang memuat waktu order (tick + latensi AI + 5 detik, spec 5.10), bukan pada harga saat order dikirim.

| Kontender | Simbol | TP | SL | time stop | WR | batas bawah | gross bps | net/1M | net/1M (fee 0.05%) | WR separuh 1 / 2 | transaksi/hari | transaksi/hari 20 hari | gerbang |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 192 | 207 | 50 | 48.1% | 42.9% | -0.98 | -399 | -549 | 50.28% / 46.36% | 9.0 | 9.7 | gagal |
| Kontender 1 (struktur Pine) | ETH-USDT | 96 | 88 | 7 | 52.17% | 44.2% | 0.26 | -337 | -487 | 57.53% / 48.6% | 3.8 | 3.4 | gagal |
| Kontender 2 (logika bot) | BTC-USDT | 259 | 285 | 104 | 47.61% | 44.2% | -1.58 | -429 | -579 | 45.7% / 49.31% | 13.0 | 12.3 | gagal |
| Kontender 2 (logika bot) | ETH-USDT | 142 | 138 | 32 | 50.71% | 45.6% | -0.33 | -366 | -516 | 46.38% / 54.93% | 6.2 | 6.4 | gagal |
| Kontender 3 (meta-labeling) | BTC-USDT | 176 | 194 | 34 | 47.57% | 42.3% | -1.40 | -420 | -570 | 47.09% / 47.98% | 8.1 | 9.2 | gagal |
| Kontender 3 (meta-labeling) | ETH-USDT | 108 | 82 | 6 | 56.84% | 47.42% | 2.05 | -247 | -397 | 57.47% / 56.31% | 3.9 | 3.3 | gagal |

Referensi (skrip Pine apa adanya, hanya dilaporkan):

| Simbol | n | WR | gross bps | net/1M |
|---|---|---|---|---|
| BTC-USDT | 570 | 50.79% | -0.33 | -366 |
| ETH-USDT | 324 | 48.4% | -1.22 | -411 |

## Gerbang (kriteria 1–9)

Kriteria (spec 5.11), per kontender dan simbol:

1. Minimal 60 transaksi TP+SL pada simbol itu.
2. p Holm < 0.05, satu sisi: WR di atas C0 (edge ada).
3. WR ≥ 57%.
4. Gross > 0 bps (setelah slippage, sebelum fee).
5. WR > 50% di tiap setengah periode uji.
6. Ketahanan: WR ≥ 55% dengan overshoot stop 2 bps dan WR > 50% di minimal 15 dari 20 offset tick.
7. Sumber edge: permutasi tren p < 0.05 (kontender 1 dan 2); kontender 3 lebih tinggi dari aturan primernya sendiri, p < 0.05.
8. Pasokan gabungan simbol yang lulus ≥ 10 transaksi per hari (periode uji dan 20 hari terakhir).
9. Kontrol wajar (C0 50 ± 3% dengan |gross| ≤ 2 bps; PBO latih < 0.5 untuk kontender 1 dan 3).

Kolom "p Holm (blok 5 hari)" adalah sensitivitas, hanya dilaporkan dan tidak masuk gerbang: p kriteria 2 dengan bootstrap blok 5 hari berturut-turut, karena rezim tren bertahan berminggu-minggu.

| Kontender | Simbol | p Holm | p Holm (blok 5 hari) | WR | Hasil |
|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 1.000 | 1.000 | 48.1% | gagal: 2, 3, 4, 5, 6, 7, 8 |
| Kontender 1 (struktur Pine) | ETH-USDT | 0.327 | 0.175 | 52.17% | gagal: 2, 3, 5, 6, 7, 8 |
| Kontender 2 (logika bot) | BTC-USDT | 1.000 | 1.000 | 47.61% | gagal: 2, 3, 4, 5, 6, 7 |
| Kontender 2 (logika bot) | ETH-USDT | 0.327 | 0.212 | 50.71% | gagal: 2, 3, 4, 5, 6, 7, 8 |
| Kontender 3 (meta-labeling) | BTC-USDT | 1.000 | 1.000 | 47.57% | gagal: 2, 3, 4, 5, 6, 7, 8 |
| Kontender 3 (meta-labeling) | ETH-USDT | 0.081 | 0.086 | 56.84% | gagal: 2, 3, 8 |

Kontender terpilih: tidak ada (bot tidak diubah). Simbol diperdagangkan: –.

## Kontrol dan permutasi

C0: selalu-long dan selalu-short pada jam tick yang sama; harus 50 ± 3% WR dan gross dalam ± 2 bps.

| Simbol | WR long | gross long | WR short | gross short | Hasil |
|---|---|---|---|---|---|
| BTC-USDT | 49.32% | -0.97 | 50.54% | -0.52 | wajar |
| ETH-USDT | 47.65% | -1.51 | 48.8% | -1.01 | wajar |

C1: waktu entri kontender dengan sisi acak (nilai arah):

| Kontender | Simbol | WR kontender | C1 rata-rata | p5 | p95 | n entri |
|---|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 48.1% | 49.85% | 45.4% | 54.24% | 449 |
| Kontender 1 (struktur Pine) | ETH-USDT | 52.17% | 48.9% | 42.4% | 54.89% | 191 |
| Kontender 2 (logika bot) | BTC-USDT | 47.61% | 49.26% | 45.9% | 53.04% | 648 |
| Kontender 2 (logika bot) | ETH-USDT | 50.71% | 49.52% | 44.6% | 54.29% | 312 |
| Kontender 3 (meta-labeling) | BTC-USDT | 47.57% | 49.73% | 45.1% | 53.37% | 404 |
| Kontender 3 (meta-labeling) | ETH-USDT | 56.84% | 50.09% | 44.7% | 55.80% | 196 |

Sumber edge (kriteria 7) dan ketahanan (kriteria 6):

| Kontender | Simbol | p | Sumber | Ketahanan |
|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 0.464 | permutasi tren, 1000 pergeseran; WR rata-rata tergeser 47.95% | WR dengan overshoot 2.0 bps 48.1%; offset dengan WR > 50%: 1 dari 20 |
| Kontender 1 (struktur Pine) | ETH-USDT | 0.623 | permutasi tren, 1000 pergeseran; WR rata-rata tergeser 53.59% | WR dengan overshoot 2.0 bps 52.17%; offset dengan WR > 50%: 19 dari 20 |
| Kontender 2 (logika bot) | BTC-USDT | 0.805 | permutasi tren, 1000 pergeseran; WR rata-rata tergeser 49.35% | WR dengan overshoot 2.0 bps 47.61%; offset dengan WR > 50%: 0 dari 20 |
| Kontender 2 (logika bot) | ETH-USDT | 0.303 | permutasi tren, 1000 pergeseran; WR rata-rata tergeser 49.66% | WR dengan overshoot 2.0 bps 50.71%; offset dengan WR > 50%: 5 dari 20 |
| Kontender 3 (meta-labeling) | BTC-USDT | 0.647 | dibanding aturan primer; WR aturan primer 48.3% (n 560) | WR dengan overshoot 2.0 bps 47.57%; offset dengan WR > 50%: 0 dari 20 |
| Kontender 3 (meta-labeling) | ETH-USDT | 0.039 | dibanding aturan primer; WR aturan primer 51.5% (n 206) | WR dengan overshoot 2.0 bps 56.84%; offset dengan WR > 50%: 19 dari 20 |

## VST vs produksi

Bagian ini belum tersedia.

## Keputusan AI (bucket A/B/E/F)

Bagian ini belum tersedia.

## Biaya kadens dan guard

Kadens: jam satu menit yang hanya bertindak pada penutupan 5m, tanpa latensi dan tanpa jeda order. Dibandingkan dengan jalannya utama:

| Kontender | Simbol | WR utama | WR kadens | gross bps | net/1M | transaksi/hari |
|---|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 48.1% | 46.38% | -1.66 | -433 | 9.2 |
| Kontender 1 (struktur Pine) | ETH-USDT | 52.17% | 51.9% | 0.30 | -335 | 3.8 |
| Kontender 2 (logika bot) | BTC-USDT | 47.61% | 47.30% | -1.68 | -434 | 13.1 |
| Kontender 2 (logika bot) | ETH-USDT | 50.71% | 47.58% | -1.49 | -424 | 6.0 |
| Kontender 3 (meta-labeling) | BTC-USDT | 47.57% | 47.24% | -1.41 | -420 | 8.0 |
| Kontender 3 (meta-labeling) | ETH-USDT | 56.84% | 58.6% | 2.78 | -211 | 4.0 |

Guard dimatikan (pasokan tanpa batas harian) dan urutan ETH lebih dulu (bagian tick dengan sinyal di kedua simbol):

| Kontender | pasokan tanpa guard | sinyal kedua simbol | tick sinyal ganda |
|---|---|---|---|
| Kontender 1 (struktur Pine) | 14.6 | 1.6% | 253 |
| Kontender 2 (logika bot) | 34.7 | 4.0% | 246 |
| Kontender 3 (meta-labeling) | 13.2 | 1.0% | 173 |

Per simbol dengan guard dimatikan:

| Kontender | Simbol | n | WR | gross bps | transaksi/hari |
|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | BTC-USDT | 507 | 49.02% | -0.63 | 10.1 |
| Kontender 1 (struktur Pine) | ETH-USDT | 223 | 53.24% | 0.69 | 4.5 |
| Kontender 2 (logika bot) | BTC-USDT | 1117 | 49.20% | -1.06 | 22.3 |
| Kontender 2 (logika bot) | ETH-USDT | 619 | 48.9% | -1.05 | 12.4 |
| Kontender 3 (meta-labeling) | BTC-USDT | 434 | 50.12% | -0.55 | 8.7 |
| Kontender 3 (meta-labeling) | ETH-USDT | 227 | 54.05% | 0.96 | 4.5 |

Sensitivitas overshoot stop (hanya dilaporkan; 2 bps ada di kriteria 6):

| Kontender | overshoot bps | Simbol | WR | gross bps | net/1M |
|---|---|---|---|---|---|
| Kontender 1 (struktur Pine) | 0.0 | BTC-USDT | 48.1% | -0.52 | -376 |
| Kontender 1 (struktur Pine) | 0.0 | ETH-USDT | 52.17% | 0.72 | -314 |
| Kontender 1 (struktur Pine) | 1.0 | BTC-USDT | 48.1% | -0.98 | -399 |
| Kontender 1 (struktur Pine) | 1.0 | ETH-USDT | 52.17% | 0.26 | -337 |
| Kontender 2 (logika bot) | 0.0 | BTC-USDT | 47.61% | -1.14 | -407 |
| Kontender 2 (logika bot) | 0.0 | ETH-USDT | 50.71% | 0.11 | -344 |
| Kontender 2 (logika bot) | 1.0 | BTC-USDT | 47.61% | -1.58 | -429 |
| Kontender 2 (logika bot) | 1.0 | ETH-USDT | 50.71% | -0.33 | -366 |
| Kontender 3 (meta-labeling) | 0.0 | BTC-USDT | 47.57% | -0.92 | -396 |
| Kontender 3 (meta-labeling) | 0.0 | ETH-USDT | 56.84% | 2.47 | -226 |
| Kontender 3 (meta-labeling) | 1.0 | BTC-USDT | 47.57% | -1.40 | -420 |
| Kontender 3 (meta-labeling) | 1.0 | ETH-USDT | 56.84% | 2.05 | -247 |

## Kesimpulan

Tidak ada kontender yang melewati gerbang: BTC-USDT, ETH-USDT tidak lulus. Bot tidak diubah, sampel K2 berjalan terus, dan bos menerima tabel WR vs pasokan per kontender dan simbol di atas.

Angka di atas adalah hasil simulasi offline; keputusan atas bot nyata tetap menunggu persetujuan terpisah.

Catatan simulasi:

- Batas 8 blok CSCV tidak di-purge: paling banyak sekitar 7 transaksi per aturan melintasi batas blok; dampaknya pada PBO kecil.
- Blackout funding diperiksa pada waktu tick, sedangkan bot live memeriksanya setelah panggilan AI; guard harian dihitung pada hari entri, sedangkan bot live menghitungnya pada closed_at. Keduanya berdampak kecil.
- Pada tampilan keputusan AI, tick ditempatkan satu latensi AI sebelum created_at, padahal latensi diukur setelah konteks dibangun; tick bisa tercatat sedikit terlambat, yang sedikit menguntungkan bucket B, E dan F dibanding A.
