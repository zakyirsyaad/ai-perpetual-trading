# Langkah 1 — Mode A (pullback LONG) di BTC: latihan dan ujian sekali jalan

Tanggal: 18 Oktober 2026 · Untuk: dokumen "Modul LONG — Spesifikasi & Rencana Uji", langkah 1
(Mode A di BTC) · Spec: `docs/superpowers/specs/2026-10-17-long-module-phase1-design.md` §2.8 dan §9

## Ringkasan

- **GAGAL.** Ujian sekali jalan (5 Agustus – 13 Oktober 2026) memberi 94 posisi dan −546 per
  1 juta, dan −617 setelah 3 posisi paling untung dibuang. Syarat lolos: minimal 40 posisi, lebih
  baik dari −150, dan tanpa 3 teratas lebih baik dari −250. Jumlah posisinya cukup, tapi kedua
  syarat hasil jauh dari terpenuhi.
- **Pemicu dipindah ke 5m sebelum ujian, sesuai dokumen.** Di latihan, pemicu 15m hanya memberi
  2,64 entry per hari NAIK (batasnya 3). Ini satu-satunya penyesuaian yang diizinkan, dan
  keputusannya diambil dari jumlah entry saja, bukan dari hasil.
- **Latihan juga rugi, di setiap periode.** Agustus 2025 – 4 Agustus 2026: −472 per 1 juta dari
  479 posisi. Tidak ada kuartal latihan yang mendekati −150 (terbaik −327, 110 posisi). Bulan
  ujian terbaik −258, tapi hanya 17 posisi (ketidakpastian sekitar ±315), jadi angka per bulan
  belum bermakna.
- **Tidak ada keunggulan sebelum biaya.** Sebelum fee dan slippage, rata-rata per posisi −0,4 bps
  di latihan dan −1,9 bps di ujian, sedangkan biayanya 9 bps per posisi (450 per 1 juta). Menang
  30–33%, padahal titik impas dengan TP 0,40%, SL 0,25% dan biaya itu sekitar 51%.
- **Pembanding juga rugi.** Aturan SHORT sekarang yang dijalankan di jam NAIK yang sama: −483 per
  1 juta di ujian (150 posisi). Tidak trading = 0.
- **Mode A di BTC tidak lanjut ke forward test.** Menurut tahapan dokumen, forward test hanya untuk
  mode yang lolos ujian, atau yang posisinya kurang dari 40 karena kondisinya jarang. Langkah
  berikutnya perlu keputusan (lihat "Langkah berikutnya").

## Cara membaca

- **Mode A** (dokumen, dengan pemicu 5m): candle 5m menyentuh EMA20 (5m); dalam 3 candle, candle
  sentuhan termasuk, ada candle hijau yang ditutup di atas EMA20; close itu paling jauh 1 × ATR14
  (1 jam) di atas SMA20 (1 jam). Entry market di open candle berikutnya. TP +0,40%, SL −0,25%,
  batas 90 menit. Hanya saat kondisi NAIK.
- **Kondisi NAIK** = hasil pemilih mode dari dokumen (SMA20 > SMA50 di 1 jam, close 4 jam terakhir
  di atas EMA50 4 jam, bukan SIDEWAYS), dihitung hanya dari candle yang sudah ditutup.
- **Hari NAIK** = hari WIB dengan minimal satu jam berkondisi NAIK di dalam jendela entry mingguan
  (Senin 07:00 – Sabtu 00:00 WIB), saat pemicu Mode A sudah bisa memberi sinyal. **Entry per hari
  aktif** = posisi / hari NAIK. Latihan punya 126 hari NAIK, ujian 32.
- **Menang** = TP / (TP + SL); posisi yang keluar karena batas waktu tidak dihitung, tapi tetap
  masuk Posisi dan Hasil.
- **Hasil per 1 juta** = hasil bersih setelah fee 0,035% per sisi dan slippage 0,01% di setiap
  order (masuk dan keluar), per 1 juta USDT volume. Setiap posisi 2 × 25.000 USDT volume.
- **Hasil kotor** = sebelum fee dan slippage. Biaya satu posisi 9 bps dari notional (fee 7 bps,
  slippage 2 bps), atau 450 per 1 juta. Hasil per 1 juta = 50 × hasil bersih per posisi dalam bps.
- **Tanpa 3 teratas** = Hasil per 1 juta setelah 3 posisi paling untung dibuang.
- **Pembanding** (dokumen, di jam NAIK yang sama):
  - "D di NAIK" = aturan SHORT sekarang versi backtest (Mode D: harga 3m paling jauh 0,75 × ATR dari
    EMA21; TP 0,20%, SL 0,20%, batas 60 menit), dijalankan saat NAIK, bukan TURUN;
  - "Tidak trading" = 0.
- **Ketidakpastian** (satu standard error): sekitar ±135 per 1 juta untuk 94 posisi ujian, ±64
  untuk 479 posisi latihan, dan ±76 untuk 150 posisi pembanding di ujian. Selisih yang lebih kecil
  dari itu belum bermakna.

## Tabel

| Mode | Pair | Tahap | Posisi | Entry per hari aktif | Menang | Hasil per 1 juta | Keluar TP / SL / waktu | Rata-rata lama posisi |
|---|---|---|---|---|---|---|---|---|
| A (15m) | BTC-USDT | Latihan | 333 | 2,64 | 29,9% | -567 | 82 / 192 / 59 | 43,1 menit |
| A (5m) | BTC-USDT | Latihan | 479 | 3,80 | 32,8% | -472 | 124 / 254 / 101 | 44,0 menit |
| D di NAIK | BTC-USDT | Latihan | 718 | 5,70 | 48,1% | -427 | 310 / 335 / 73 | 22,2 menit |
| Tidak trading | BTC-USDT | Latihan | 0 | 0,00 | – | 0 | 0 / 0 / 0 | – |
| A (5m) | BTC-USDT | Ujian | 94 | 2,94 | 29,7% | -546 | 19 / 45 / 30 | 52,0 menit |
| D di NAIK | BTC-USDT | Ujian | 150 | 4,69 | 44,0% | -483 | 55 / 70 / 25 | 25,1 menit |
| Tidak trading | BTC-USDT | Ujian | 0 | 0,00 | – | 0 | 0 / 0 / 0 | – |

Baris A (15m) hanya dasar keputusan pindah ke 5m; ujian memakai 5m yang sudah dikunci. Status yang
dicetak tahap `test`, dengan angka persis:

```
Ujian Mode A BTC-USDT (5m): GAGAL. Posisi 94 (minimal 40); Hasil per 1 juta = -546.4418251248859 (lolos bila > -150.0); tanpa 3 posisi teratas = -617.2285884300535 (lolos bila > -250.0).
```

### Hasil kotor dan biaya

| Run | Tahap | Hasil kotor per posisi | Hasil kotor per 1 juta | Biaya per 1 juta | Hasil per 1 juta |
|---|---|---|---|---|---|
| A (15m) | Latihan | -2,34 bps | -117 | 450 | -567 |
| A (5m) | Latihan | -0,43 bps | -22 | 450 | -472 |
| D di NAIK | Latihan | 0,45 bps | 23 | 450 | -427 |
| A (5m) | Ujian | -1,93 bps | -96 | 450 | -546 |
| D di NAIK | Ujian | -0,66 bps | -33 | 450 | -483 |

### Mode A (5m) per periode

| Tahap | Periode (waktu entry, WIB) | Posisi | Menang | Hasil per 1 juta |
|---|---|---|---|---|
| Latihan | Agustus – September 2025 | 76 | 26,9% | -624 |
| Latihan | Oktober – Desember 2025 | 103 | 31,7% | -505 |
| Latihan | Januari – Maret 2026 | 110 | 38,7% | -327 |
| Latihan | April – Juni 2026 | 135 | 34,9% | -383 |
| Latihan | 1 Juli – 4 Agustus 2026 | 55 | 23,8% | -707 |
| Ujian | 5 – 31 Agustus 2026 | 45 | 32,1% | -470 |
| Ujian | September 2026 | 32 | 23,1% | -807 |
| Ujian | 1 – 13 Oktober 2026 | 17 | 40,0% | -258 |

## Yang bisa dibaca dari hasil

- **Kerugiannya merata, bukan karena beberapa posisi.** Membuang 3 posisi paling untung menggeser
  ujian dari −546 ke −617. Kelima kuartal latihan di bawah −300 (terbaik −327, Januari – Maret
  2026), dan ketiga bulan ujian di bawah −250.
- **Menang yang rendah bukan masalah utamanya.** TP dan SL dihitung dari harga fill, yang sudah
  0,01% di atas open, jadi harga mulai 0,41% di bawah TP dan 0,24% di atas SL. Harga yang bergerak
  tanpa arah menyentuh SL lebih dulu kira-kira 6 dari 10 kali, jadi mode tanpa keunggulan menang
  sekitar 37%. Mode A menang 30–33%, sedikit di bawah itu, sesuai hasil kotornya yang sedikit
  negatif. Setelah biaya, posisi TP bersih +32 bps dan posisi SL −33 bps (sama di setiap posisi di
  log), jadi impas butuh sekitar 51%, dan −150 butuh sekitar 46%.
- **Pindah ke 5m tidak mengubah gambaran.** Di latihan 5m lebih baik dari 15m (−472 lawan −567),
  tapi keduanya jauh dari −150, dan hasil kotor keduanya di bawah nol.
- **Yang dibutuhkan untuk −150.** Biayanya 450 per 1 juta, jadi sebuah mode perlu hasil kotor
  sekitar +300 per 1 juta, atau +6 bps per posisi. Semua varian LONG saat NAIK yang sudah diuji
  (langkah 0 dan Mode A) hasil kotornya antara −2,6 dan +0,6 bps per posisi.
- **Pembanding sama buruknya.** SHORT dekat rata-rata saat NAIK juga rugi (−427 di latihan, −483
  di ujian) dengan hasil kotor sekitar nol. Selisih Mode A dengan pembanding di ujian (−546 lawan
  −483) lebih kecil dari ketidakpastiannya.
- **Aturan "SL dihitung bila TP dan SL tersentuh di satu candle" tidak pernah terpakai.** Semua exit
  SL (254 di latihan, 45 di ujian) kena di level SL, dan tidak satu pun candle 1m-nya juga
  menyentuh TP. Hasil ini tidak dibuat lebih buruk oleh aturan itu.

## Langkah berikutnya (perlu keputusan)

- Urutan di dokumen: 0 → A BTC → ETH (A + D) → B → C → D2. Langkah 0 dan Mode A di BTC sudah
  selesai.
- Langkah berikut menurut urutan adalah ETH dengan Mode A dan D. Mode A di ETH memakai aturan yang
  sama, dengan TP dan SL dikali faktor k (1–2). Di langkah 0, LONG saat NAIK di ETH juga rugi
  (−318 sampai −437 per 1 juta, dengan exit uji Pine).
- Pertanyaan untuk diputuskan:
  1. Apakah ETH tetap diuji dengan Mode A, padahal aturan A gagal di BTC, atau ETH dengan Mode D
     saja?
  2. Atau langsung ke Mode B (breakout LONG) di BTC?
- Mode A tidak bisa diuji ulang dengan angka lain di periode ini: hasil ujiannya sudah terlihat,
  jadi ujian kedua tidak lagi bersih. Setiap langkah berikutnya butuh spec sendiri sebelum kode.

## Batasan

- **Periode ujian tidak segar.** 5 Agustus – 13 Oktober 2026 juga periode ujian uji Pine, dan
  sebagian temuan tren di laporan 16 Oktober berasal dari periode ini. Angka Mode A berasal dari
  dokumen, bukan dicocokkan ke data, jadi pengaruhnya terbatas. Periode ini juga dipakai untuk mode
  berikutnya: makin banyak mode diuji di sini, makin besar peluang satu mode lolos karena
  kebetulan. Forward test tetap penentu akhir.
- **Sampel.** Dengan 94 posisi ketidakpastiannya sekitar ±135 per 1 juta. Bahkan ditambah dua kali
  ketidakpastian itu, hasil ujian baru sekitar −275, masih di bawah −150.
- **Mode D adalah pendekatan.** SHORT di bot live diputuskan AI, dengan exit berbasis ATR dan exit
  AI. Mode D di backtest memakai aturan dekat rata-rata dengan exit 0,20% / 0,20% / 60 menit.
- **Harga produksi, fill VST.** Backtest memakai candle 1m produksi BingX. Harga VST mengikuti
  produksi, tapi fill nyata akan berbeda beberapa bps.
- **Hanya BTC.** ETH, faktor k, dan mode B, C, D2 belum diuji.
- **Perbandingan dengan langkah 0 hanya kasar.** Angka langkah 0 memakai exit dan model biaya uji
  Pine; angka di sini memakai exit Mode A dan slippage tetap 0,01% per order sesuai dokumen.

## Asal angka

- **Dijalankan:** 18 Oktober 2026 di Mac (Python 3.10.20): `train` pukul 15:18 WIB dan `test`
  pukul 15:21 WIB, masing-masing 6,2 detik, memori puncak 242 MB dan 280 MB.
- **Kode:** commit `bd27a1e` di `main` (merge PR #39). `code_sha256`
  `63ec0bb3d3da232a722baa8c927f775721c6276353d886da257ccd80d8f4d348` (62 file).
- **Config:** `config/modes.yaml`, sha256
  `eb6575937627312d17dd2af5e2dc814bb42806638d36e72c5284d19d7217d93e`, sama dengan langkah 0.
- **Data:** salinan `~/replay/pine_gate/data/prod` dari VPS. Ke-892 file identik dengan VPS,
  diperiksa dengan `verify-copy` sebelum `train` dan tepat sebelum `test` (0 masalah).
  - manifest latihan: `c69c9ff2c358c0733f1f3b86f4c5146e78eaec04e737581b4d06493a278d87e6`
    (371 file)
  - semua file yang dibaca ujian: `f790d888179f0e8973eaee406eaa43cd9cdfac85c401313e758c660df9763027`
    (441 file)
  - hari ujian saja: `8ecd002850c49afe6af819afb61ec68e1ad7e1af3ee91cf63f0808a396855377` (70 file)
- **Kunci ujian:** lock `mode_A_BTC-USDT_lock.json`, sha256
  `eacf1ceb97a76293fbff2266ac70bfed810a04cf018dd74622feaad1f09d626f`. Marker
  `data/holdout_runs/mode_A_BTC-USDT_<sha256 lock>.json` dan catatan mulai
  `mode_A_BTC-USDT_test_started.json` membuat ujian ini tidak bisa dijalankan lagi.
- **Output:** di `~/replay/mode_backtest/out/`: `mode_A_BTC-USDT_train_20261018.json`,
  `mode_A_BTC-USDT_test_20261018.json` dan `.md`, serta log per posisi (CSV) untuk setiap run.
- **Angka tambahan** (hasil kotor, per periode, ketidakpastian, cek candle TP dan SL) dihitung dari
  log per posisi itu dan data 1m yang sama, tanpa menjalankan ujian lagi.
