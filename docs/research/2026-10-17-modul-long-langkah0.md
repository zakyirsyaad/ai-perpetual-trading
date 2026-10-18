# Langkah 0 — holdout uji Pine dipecah per arah dan kondisi pasar

Tanggal: 17 Oktober 2026 · Untuk: dokumen "Modul LONG — Spesifikasi & Rencana Uji", langkah 0 ·
Spec: `docs/superpowers/specs/2026-10-17-long-module-phase1-design.md` §8

## Ringkasan

- **Putar ulang identik.** Posisi tahap ujian uji Pine (5 Agustus – 13 Oktober 2026) diputar ulang
  dengan input yang dibekukan. Ke-15 angka hasil per kontender dan koin sama persis dengan file hasil
  ujian Pine, tanpa satu pun selisih. Data yang dipakai juga identik dengan data saat uji Pine.
- **LONG saat NAIK tidak punya keunggulan di uji Pine.** Untuk BTC hasilnya −359 sampai −479 per
  1 juta (133–276 posisi, menang 45–50%). Untuk ETH −318 sampai −437 (65–147 posisi).
- **Hanya satu kelompok dengan sampel memadai yang melewati −150, dan tipis sekali.** Kelompok itu
  c3 ETH LONG saat TURUN: 47 posisi, −143. Ini justru LONG yang melawan tren, dan ketidakpastiannya
  sekitar ±150. Kelompok yang positif semuanya sangat kecil (1–15 posisi). Dari 44 kelompok,
  beberapa wajar terlihat bagus karena kebetulan.
- **Ini belum menentukan nasib Mode A.** Posisi di tabel memakai exit uji Pine (TP 0,20%, SL 0,20%,
  batas 60 menit). Mode A memakai TP 0,40%, SL 0,25% dan batas 90 menit, dengan pemicu yang berbeda.
  Mode A tetap diuji persis sesuai dokumen (langkah 1).

## Cara membaca

- **Kontender** (sama dengan laporan 16 Oktober):
  - c1 = aturan tren 4 jam + koreksi + kekuatan tren;
  - c2 = aturan bot yang sekarang (pembanding): harga dekat rata-rata + arah tren 1 jam;
  - c3 = aturan tren + saringan model statistik.
- **Kondisi** = hasil pemilih mode dari dokumen (SMA20/SMA50 1 jam, ATR14 1 jam, EMA50 4 jam) pada
  waktu sinyal posisi itu, dihitung hanya dari candle yang sudah ditutup.
- **Menang** = TP / (TP + SL); posisi yang keluar karena batas waktu tidak dihitung.
- **Rata-rata bps** = hasil kotor per posisi, sebelum fee.
- **Hasil per 1 juta** = hasil bersih setelah fee 0,035% per sisi, per 1 juta USDT volume (setiap
  posisi 2 × 25.000 USDT).
- **Kombinasi yang tidak ada di tabel** berarti 0 posisi (misalnya c1 ETH SHORT saat NAIK).
- **Ketidakpastian.** Kira-kira ±80 per 1 juta untuk sekitar 150 posisi, ±160 untuk 40 posisi, dan
  ±260 untuk 15 posisi. Selisih antarkelompok yang lebih kecil dari itu belum bermakna.

## Tabel

| Kontender | Koin | Arah | Kondisi | Posisi | Menang | Rata-rata bps | Hasil per 1 juta |
|---|---|---|---|---|---|---|---|
| c1 | BTC | LONG | NAIK | 159 | 50,3% | -0,17 | -359 |
| c1 | BTC | LONG | TURUN | 92 | 48,8% | -0,86 | -393 |
| c1 | BTC | LONG | SIDEWAYS | 53 | 49,0% | -1,17 | -408 |
| c1 | BTC | LONG | TIDAK_ENTRY | 5 | 60,0% | 3,45 | -177 |
| c1 | BTC | SHORT | NAIK | 36 | 51,6% | 0,49 | -325 |
| c1 | BTC | SHORT | TURUN | 76 | 43,8% | -2,32 | -466 |
| c1 | BTC | SHORT | SIDEWAYS | 10 | 60,0% | 1,61 | -270 |
| c1 | BTC | SHORT | TIDAK_ENTRY | 18 | 25,0% | -8,21 | -760 |
| c1 | ETH | LONG | NAIK | 65 | 46,8% | -1,74 | -437 |
| c1 | ETH | LONG | TURUN | 39 | 53,8% | 0,94 | -303 |
| c1 | ETH | LONG | SIDEWAYS | 20 | 65,0% | 5,49 | -76 |
| c1 | ETH | LONG | TIDAK_ENTRY | 15 | 66,7% | 6,17 | -42 |
| c1 | ETH | SHORT | TURUN | 22 | 50,0% | 0,06 | -347 |
| c1 | ETH | SHORT | TIDAK_ENTRY | 30 | 46,4% | -2,59 | -479 |
| c2 | BTC | LONG | NAIK | 276 | 45,2% | -2,59 | -479 |
| c2 | BTC | LONG | TURUN | 27 | 61,5% | 3,58 | -171 |
| c2 | BTC | LONG | SIDEWAYS | 23 | 40,9% | -3,81 | -540 |
| c2 | BTC | LONG | TIDAK_ENTRY | 12 | 70,0% | 4,39 | -131 |
| c2 | BTC | SHORT | NAIK | 6 | 20,0% | -11,02 | -901 |
| c2 | BTC | SHORT | TURUN | 266 | 49,1% | -0,86 | -393 |
| c2 | BTC | SHORT | SIDEWAYS | 28 | 41,7% | -3,21 | -511 |
| c2 | BTC | SHORT | TIDAK_ENTRY | 10 | 55,6% | 1,45 | -278 |
| c2 | ETH | LONG | NAIK | 147 | 48,1% | -1,50 | -425 |
| c2 | ETH | LONG | TURUN | 5 | 60,0% | 3,45 | -177 |
| c2 | ETH | LONG | SIDEWAYS | 14 | 38,5% | -4,90 | -595 |
| c2 | ETH | LONG | TIDAK_ENTRY | 28 | 60,0% | 3,51 | -174 |
| c2 | ETH | SHORT | NAIK | 5 | 40,0% | -4,70 | -585 |
| c2 | ETH | SHORT | TURUN | 96 | 52,4% | 0,46 | -327 |
| c2 | ETH | SHORT | SIDEWAYS | 16 | 56,2% | 1,92 | -254 |
| c2 | ETH | SHORT | TIDAK_ENTRY | 1 | 100,0% | 19,75 | 638 |
| c3 | BTC | LONG | NAIK | 133 | 47,2% | -1,72 | -436 |
| c3 | BTC | LONG | TURUN | 87 | 43,6% | -2,97 | -499 |
| c3 | BTC | LONG | SIDEWAYS | 60 | 48,3% | -1,26 | -413 |
| c3 | BTC | LONG | TIDAK_ENTRY | 1 | 100,0% | 19,75 | 637 |
| c3 | BTC | SHORT | NAIK | 33 | 48,3% | -1,09 | -405 |
| c3 | BTC | SHORT | TURUN | 74 | 53,1% | 1,18 | -291 |
| c3 | BTC | SHORT | SIDEWAYS | 3 | 100,0% | 12,91 | 295 |
| c3 | BTC | SHORT | TIDAK_ENTRY | 13 | 30,8% | -8,46 | -773 |
| c3 | ETH | LONG | NAIK | 81 | 53,1% | 0,63 | -318 |
| c3 | ETH | LONG | TURUN | 47 | 61,7% | 4,14 | -143 |
| c3 | ETH | LONG | SIDEWAYS | 15 | 86,7% | 14,32 | 366 |
| c3 | ETH | LONG | TIDAK_ENTRY | 13 | 53,8% | 0,94 | -303 |
| c3 | ETH | SHORT | TURUN | 19 | 60,0% | 2,91 | -204 |
| c3 | ETH | SHORT | TIDAK_ENTRY | 21 | 36,8% | -5,98 | -649 |

## Yang bisa dibaca dari tabel

- **LONG saat NAIK (kondisi Mode A):** semua kontender merugi jauh dari −150. Untuk BTC: c1 −359
  (159 posisi), c2 −479 (276 posisi), c3 −436 (133 posisi). Hasil kotornya juga di sekitar nol
  atau negatif (−2,6 sampai −0,2 bps). Dengan bracket uji Pine, mengikuti tren naik belum memberi
  keunggulan.
- **SHORT saat TURUN (kondisi Mode D):** untuk BTC −291 sampai −466, dan untuk ETH −204 sampai −347.
  Umumnya sedikit lebih baik daripada LONG saat NAIK, kecuali c1 BTC (−466 lawan −359), tapi tetap
  di bawah target.
- **SHORT saat NAIK (melawan tren):** BTC c2 −901, tapi hanya 6 posisi. c1 −325 (36 posisi) dan c3
  −405 (33 posisi).
- **Satu-satunya kelompok ≥ 40 posisi yang lebih baik dari −150** adalah c3 ETH LONG saat TURUN
  (47 posisi, menang 61,7%, −143). Ini LONG yang melawan tren turun, kebalikan dari ide Mode A.
  Selisihnya dari −150 jauh di bawah ketidakpastiannya (sekitar ±150).
- **Kelompok yang terlihat positif** (c3 ETH LONG SIDEWAYS +366 dengan 15 posisi, beberapa kelompok
  1–3 posisi) terlalu kecil untuk dipercaya. Dengan 44 kelompok, beberapa angka bagus memang
  diharapkan muncul karena kebetulan.

## Batasan

- **Exit dan irama berbeda dari mode baru.** Posisi memakai exit uji Pine (TP dan SL 0,20%, batas
  waktu 60 menit), jam tick dengan jeda keputusan ala AI, dan penjaga harian uji Pine. Exit dan
  pemicu mode baru berbeda.
- **Periode ujian yang sama.** Periode ujian ini juga dipakai lagi di langkah 1 (Mode A). Hasil
  akhirnya tetap ditentukan oleh forward test.
- **Langkah 0 hanya membaca ulang.** Tidak ada aturan yang diuji dan tidak ada keputusan yang
  berubah.

## Asal angka

- **Dijalankan:** 17 Oktober 2026, 20:18 WIB, di Mac (Python 3.10). Lama 16,8 detik, memori puncak
  sekitar 410 MB.
- **Kode:** commit `57e33ed` di branch `feat/long-module-backtest`. `code_sha256` tercatat di file
  hasil.
- **Data:** salinan `~/replay/pine_gate/data/prod` dari VPS. Ke-892 file identik dengan VPS
  (diperiksa dengan `verify-copy`).
  - `data_sha256`: `d426d975544d90513ca3e97490869ef624ba461b00b46b636931fe297adfd820`, sama dengan
    uji Pine.
- **File uji Pine:**
  - holdout: `3459341410e5694a455594304baecf5966a7d362c298a4845069116f29c05702`
  - seleksi: `3687376145f2f111c976ed78b50e25b7e58be07308615387a7d3cdcee7a9e310`
  - kalibrasi: `e785deab7ea05ec4da1890e35c92dbc9960033d92d0bc9710e16f733ea61f37a`
- **Config:** `config/modes.yaml`, sha256 `eb6575937627312d17dd2af5e2dc814bb42806638d36e72c5284d19d7217d93e`.
- **Output:** `~/replay/mode_backtest/out/step0_20261017.json` dan `.md`.
