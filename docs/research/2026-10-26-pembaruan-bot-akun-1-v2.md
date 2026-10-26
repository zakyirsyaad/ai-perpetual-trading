# Pembaruan bot akun 1: hasil minggu ini dan koreksi laporan 21 Oktober

Tanggal: 25 Oktober 2026, **dikoreksi 26 Oktober 2026 (fee akun 0,05%; versi 4: untuk hasil −250 per 1 juta, gross yang dibutuhkan +5,0 bps, bukan +5,5)** · Dasar: trade sungguhan akun 1 dan akun 2 (19–23 Oktober), 387 keputusan AI di jam kondisi TURUN, dan replay offline pada keputusan yang sama. Laporan ini **menggantikan** laporan 21 Oktober pada bagian yang dikoreksi di bagian 2. Istilah: 1 bps = 0,01% harga; "per 1 juta" = hasil per 1 juta USDT volume.

## Koreksi 26 Oktober: fee akun 0,05% per sisi

Dokumen 25 Oktober memakai fee 0,035% per sisi. Pemeriksaan ke BingX pada 26 Oktober (hanya membaca) menunjukkan fee akun **0,05% taker / 0,02% maker sejak sekitar 20 Oktober malam**: komisi per order yang dilaporkan BingX (`commission` di `/trade/order`) 0,0350% pada #799 dan #800 (fill 20 Okt sampai sekitar 16.00 UTC) dan 0,0500% pada #801, #805, #812 dan #813; `commissionRate` menjawab taker 0,0500% dan maker 0,0200%. `pnl_usdt` yang tercatat sudah memakai komisi nyata, jadi **semua hasil nyata dan semua angka gross (bps) di dokumen ini tidak berubah**. Yang berubah hanya hitungan ke depan:

| | Dokumen 25 Okt | Dikoreksi |
|---|---|---|
| Hasil per 1 juta | 50 × gross (bps) − 350 | **50 × gross (bps) − 500** |
| Gross untuk −150 per 1 juta | +4 bps | **+7 bps** |
| Gross untuk −200 sampai −250 | +3 sampai +2 bps | **+6,0 sampai +5,0 bps** |
| Perkiraan (gross +2 ± 2,2 bps) | sekitar −250 (−360 sampai −140) | **sekitar −400 (−510 sampai −290)** |
| Gross nol | −350 | **−500** |
| Hipotesis +4 bps dan ambang ledger +4 bps | setara −150 | **setara −300** |
| Kerugian satu stop (0,30% dari 25.000 ditambah fee) | sekitar −92 USDT | **sekitar −100 sampai −115 USDT** |

Bagian yang dikoreksi: ringkasan, bagian 3 (rumus), 4 (angka stop), 5 (tabel dan catatan fee), 6 (hipotesis dan ledger), 8 sampai 10 (status keputusan), dan bagian 11 yang baru (hari pertama langkah 2). Jawaban bos 25 Oktober ("−200 sampai −250 tidak masalah") diberikan atas angka lama dan perlu dikonfirmasi ulang.

## Ringkasan

- **Minggu ini rugi besar.** Akun 1: 15 trade, **−965 USDT (−1.286 per 1 juta)**. Sebanyak 12 dari 15 trade masuk di jam yang sekarang diblok gerbang langkah 1 (−806 USDT, hanya 1 yang menang). Siklus sejak 12 Oktober: 47 trade, 2,35 juta volume, **−550 per 1 juta**.
- **Langkah 1 berjalan sesuai desain tetapi hampir tidak trade:** hanya 2 trade dalam 26 jam kondisi TURUN. Penyebabnya sudah ditemukan: "pelajaran" yang dibaca AI menghukum satu-satunya bentuk entry yang diizinkan gerbang.
- **Dua angka di laporan 21 Oktober keliru.** Enam puluh trade butuh sekitar 3 bulan, bukan 2–4 minggu, dan jumlah trade turun lebih dari 85%, bukan separuh.
- **Langkah 2 dimajukan dan berjalan sejak 25 Oktober 11.07 WIB:** instruksi AI yang ramping dengan tujuan volume, ditambah pengaman minggu pertama. Di replay hasilnya 11 trade per 26 jam TURUN, dibanding 2 trade sebelumnya.
- **Perkiraan jujur (dikoreksi 26 Oktober):** keunggulan di kantong ini sekitar **+2 ± 2,2 bps** per trade sebelum fee. Dengan fee 0,05% per sisi itu cukup untuk sekitar **−400 per 1 juta** (rentang kasar −510 sampai −290), bukan −250 seperti tertulis semula; −150 butuh +7 bps dan rentang yang disetujui bos (−200 sampai −250) butuh +6,0 sampai +5,0 bps. Uji maju beberapa minggu ke depan yang menentukan.
- **Keputusan:** bos menjawab 25 Oktober: hasil −200 sampai −250 tidak masalah (diberikan atas fee lama, **perlu dikonfirmasi ulang**: perkiraan kini sekitar −400), pengaman dilonggarkan setelah seminggu bila sehat, dan akun 2 dihentikan total (sudah dijalankan; bagian 8).

## 1. Hasil minggu 19–23 Oktober

| | Trade | Volume | Hasil (USDT) | Per 1 juta |
|---|---|---|---|---|
| Akun 1, seluruh minggu | 15 | 0,75 juta | −965 | −1.286 |
| – sebelum langkah 0 (exit AI hidup) | 10 | 0,50 juta | −688 | −1.375 |
| – langkah 0 (exit AI mati) | 3 | 0,15 juta | −151 | −1.006 |
| – langkah 1 (gerbang kondisi pasar) | 2 | 0,10 juta | −126 | −1.259 |
| Akun 1, siklus sejak 12 Okt | 47 | 2,35 juta | −1.292 | −550 |
| Akun 2 (LONG, uji maju) | 6 | 0,30 juta | −189 | −630 |

- Dua minggu sebelumnya akun 1 berada di −221 dan −204 per 1 juta.
- **Menurut kondisi pasar pada jam entry:** 12 trade di jam SIDEWAYS, NAIK, atau TIDAK_ENTRY rugi 806 USDT (1 menang); 3 trade di jam TURUN rugi 159 USDT. Pada 20–21 Oktober BTC naik 2,7%, dan SHORT tanpa gerbang terkena berulang kali.
- Hanya 2 trade yang terjadi setelah gerbang langkah 1 aktif; gerbang itu memang dirancang memblok jenis trade yang merugikan minggu ini.

## 2. Koreksi atas laporan 21 Oktober

| Tertulis sebelumnya | Yang sebenarnya |
|---|---|
| 60 trade per langkah butuh 2–4 minggu | Langkah 1 menghasilkan sekitar 1 trade per hari (2 trade dalam 26 jam TURUN). Enam puluh trade butuh sekitar 3 bulan, sehingga aturan "60 trade" tidak bisa dipakai menilai langkah |
| Jumlah trade turun sekitar setengah | Turun lebih dari 85%: dari sekitar 7 menjadi sekitar 1 per hari. Volume 32 ribu per hari, sekitar 3% dari target 1 juta |
| Langkah 2 menunggu 60 trade dari langkah 1 | Dimajukan dan sudah berjalan sejak 25 Oktober |
| Instruksi ramping: "HOLD adalah jawaban normal" | Versi itu tidak akan menghasilkan trade. Jawabannya berkeyakinan 0,52–0,58, sedangkan kode hanya menerima 0,75 ke atas dan model tidak diberi tahu (0 dari 18 jawaban lolos di replay). Diganti versi yang menyebut ambang itu |
| SHORT di jam TURUN: +6,1 bps (58 trade) | Sampel kecil dan dipilih AI. Entry mekanis di jam TURUN (139 sampel, 8–23 Oktober) hanya +1,9 ± 2,2 bps. Angka realistis +2 sampai +4 bps |
| "Pelajaran" AI hanya saran dan tidak mengikat | Benar, tetapi dampaknya lebih besar dari perkiraan (bagian 3) |

## 3. Temuan baru

**Corong langkah 1.** Selama 26 jam TURUN AI dipanggil 387 kali:

| Jawaban AI | Jumlah |
|---|---|
| HOLD | 246 |
| Minta LONG (ditolak: bot hanya SHORT di jam TURUN) | 115 |
| Minta SHORT tetapi terlalu jauh dari rata-rata (ditolak gerbang stretch) | 24 |
| Minta SHORT dan lolos, menjadi trade | **2** |

Padahal sekitar separuh waktu di jam TURUN harga berada dekat rata-rata, dengan kapasitas sekitar 27 entry per hari TURUN.

**Penyebab: lingkaran "pelajaran".** AI membaca ringkasan trade lalu pada setiap panggilan. Isinya antara lain: "bentuk entry SHORT dekat rata-rata dengan volatilitas rendah rugi 1.023,66 USDT dari 48 trade; butuh bukti yang sangat jelas sebelum membuka bentuk itu lagi", dan tidak ada satu pun pola yang tercatat menang.

- Gerbang hanya mengizinkan entry dekat rata-rata, jadi hanya bentuk itu yang punya data.
- Kerugiannya 86% adalah fee (877 dari 1.024 USDT). Sebelum fee, 48 trade itu hanya −146 USDT, sekitar −1,2 bps per trade.
- Akibatnya 65% alasan HOLD menyebut bentuk itu, dan 24 dari 26 usulan SHORT berada di bentuk lain yang pasti ditolak kode. AI belajar menghindari fee, bukan menghindari sinyal buruk.

**Keunggulan di kantong ini tipis.** Pada harga dari bursa, jam TURUN dan NAIK, 8–23 Oktober. Entry mekanis memakai exit yang sama dengan bot live:

| Entry | Sampel | Gross per trade |
|---|---|---|
| SHORT dekat rata-rata, mekanis (semua) | 139 | +1,9 ± 2,2 bps |
| – harga di atas rata-rata (sedang memantul) | 112 | +4,4 ± 2,4 bps |
| – harga sudah di bawah rata-rata | 108 | 0,0 ± 2,5 bps |
| SHORT pilihan AI di jam TURUN (exit aturan) | 35 | +5,9 ± 4,7 bps |
| SHORT pilihan AI di luar jam TURUN (exit sebenarnya) | 31 | −7,1 bps |
| LONG dekat rata-rata di jam NAIK, mekanis | 74 | −1,4 ± 3,2 bps |

Rumusnya (fee 0,05% per sisi sejak 20 Oktober): hasil per 1 juta ≈ 50 × gross per trade (bps) − 500. Untuk −150 butuh +7 bps; dengan +2 bps hasilnya sekitar −400. Pemisahan "di atas atau di bawah rata-rata" sudah diintip beberapa kali pada data yang sama, jadi baru **hipotesis**.

## 4. Langkah 2 yang sudah berjalan (sejak 25 Oktober 11.07 WIB)

AI hanya diberi sisi yang dipetakan jam itu dan aturan stretch, tanpa "pelajaran" dan tanpa 86 faktor yang rusak. Tujuannya: **jual pantulan ke rata-rata di jam TURUN**, HOLD hanya bila bisa menyebut alasan, dan ambang keyakinan 0,75 disebut terang-terangan. Instruksi ini diuji dulu pada keputusan yang sama (replay, ±2,2 juta token):

| Request | Jawaban OPEN | Lolos gerbang kode | Trade dalam 26 jam | Token per panggilan |
|---|---|---|---|---|
| Lama (live sampai 25 Okt) | 26 | 2 | **2** | ±20 ribu |
| Ramping, versi "edge" | 18 | **0** | **0** | 3,6 ribu |
| Ramping, versi "volume" (dipakai) | 31 dari 111 tick | 31 | **11** | 4 ribu |

AI membuka SHORT di hampir semua tick yang memenuhi syarat dan tidak pernah di sisi yang salah, sehingga yang diuji ke depan terutama **aturan "jual pantulan" dengan veto AI**, bukan kemampuan AI memilih. Hasil replay (+4 sampai +5 bps) berasal dari jendela yang sama dengan sumber hipotesisnya, jadi **bukan bukti**.

Pengaturan yang berubah (restart saat posisi kosong, dicek tiga kali):

| Pengaturan | Sebelum | Sekarang |
|---|---|---|
| Instruksi AI | lama (legacy) | ramping + tujuan volume |
| Batas trade per hari | 34 | 10 |
| Jeda setelah stop loss | 0 | 20 menit |
| Batas stop loss per hari | 12 | 5 |
| Batas rugi harian | 1.500 USDT | 600 USDT |

Tiga pengaman terakhir dipasang untuk minggu pertama: bot kini bisa trade sampai 10 kali sehari, dan pembalikan arah di tengah jam bisa menumpuk 3–5 stop loss berturut-turut (sekitar −100 sampai −115 USDT masing-masing: stop 0,30–0,36% dari 25.000 ditambah fee $25; pada 26 Oktober −104 dan −114). Pengaman akan dilonggarkan jika minggu pertama sehat. Batas kerugian beruntun sengaja tidak diperketat karena 6 dari 10 trade di kantong ini memang net negatif akibat fee (angka itu dihitung pada fee 0,035%; dengan fee 0,05% porsinya lebih besar).

## 5. Perkiraan dan risiko

| Skenario | Hasil per 1 juta | Volume | Rugi per hari TURUN |
|---|---|---|---|
| Keunggulan seperti perkiraan (+2 bps) | sekitar −400 (rentang kasar −510 sampai −290) | sekitar 0,5 juta | sekitar −200 USDT |
| Keunggulan nol | −500 | sekitar 0,5 juta | sekitar −250 USDT |
| Keunggulan +4 bps (hipotesis terbukti) | −300 | sekitar 0,5 juta | sekitar −150 USDT |
| Keunggulan +5,0 sampai +6,0 bps (rentang yang disetujui bos) | −250 sampai −200 | sekitar 0,5 juta | sekitar −125 sampai −100 USDT |
| Keunggulan +7 bps (target −150) | −150 | sekitar 0,5 juta | sekitar −75 USDT |

- **Dampak ke siklus:** siklus sekarang −550 per 1 juta. Tambahan 3 juta volume di −400 membawanya ke sekitar −470. Selama keunggulan tidak terbukti, volume lebih besar berarti kerugian absolut lebih besar.
- **Hari TURUN hanya sebagian dari minggu.** Di luar jam TURUN bot tidak trade, jadi volume rata-rata per hari kerja lebih rendah dari 0,5 juta.
- **Catatan fee (dikoreksi 26 Oktober):** fee nyata akun 1 0,05% per sisi sejak 20 Oktober (sebelumnya 0,035%), jadi perkiraan fee bot (0,05%) kini sesuai. Satu-satunya tuas fee adalah order maker (0,02%), yang bertentangan dengan aturan taker saja.

## 6. Cara mengukur dan aturan hentikan

- **Hipotesis (dicatat sebelum uji maju):** SHORT di jam TURUN pada pantulan 0–0,75 ATR dari rata-rata punya gross rata-rata minimal +4 bps. Pada fee 0,05% itu setara sekitar −300 per 1 juta; −150 butuh +7 bps (koreksi 26 Oktober).
- **Bacaan awal di 30 trade:** rata-rata gross di bawah 0 berarti kembali ke pengaturan sebelumnya (restart saat posisi kosong). Bacaan penuh di 60 trade lewat ledger setup (batas atas = rata-rata + 1,28 × galat baku harus ≥ ambang ledger, kini +4 bps = −300 per 1 juta pada fee 0,05%: nilainya harus diputuskan ulang sebelum langkah 3), dipisah menurut posisi harga terhadap rata-rata.
- **Alat ukur:** laporan otomatis dari database dengan tiga titik awal (langkah 0, 1, dan 2), ditambah pemantauan harian corong (jumlah panggilan, jawaban OPEN, trade). Pengecekan pertama Senin 26 Oktober setelah jendela entry buka pukul 07.00 WIB (sudah dilakukan: bagian 11).
- **Pembatalan:** kembalikan file pengaturan dari backup (atau set instruksi AI ke versi lama), restart saat posisi kosong.

## 7. Keamanan pelaksanaan

- Kode lolos 3.004 tes otomatis, cakupan 92%, dan dua putaran review (satu independen) dengan semua temuan penting sudah diperbaiki.
- Pengaturan baru divalidasi pada salinan dengan kode yang sudah ter-merge sebelum dipasang; backup pengaturan lama disimpan.
- Restart hanya saat posisi kosong (dicek ke bursa dan database), dan jendela entry sedang tutup saat penerapan. Bot LONG di akun 2 tidak tersentuh.

## 8. Bot LONG di akun 2 (sudah diputuskan: dihentikan total)

- Enam trade, **semuanya pada 19 Oktober**, hasil −189 USDT (−630 per 1 juta); sesudahnya tidak ada trade (225 usulan SHORT ditolak di jam NAIK).
- Pada entry yang sama, exit AI rata-rata −4,3 bps, sedangkan exit aturan akan memberi +7,0 bps (selisih +169 USDT dari 6 trade). Biayanya 9,2 juta token untuk 6 trade.
- Pertanyaan yang sama (LONG pilihan AI di jam NAIK) sudah dijawab tanpa biaya oleh data shadow LONG di akun 1.

| Opsi | Isi |
|---|---|
| A | Matikan exit AI di bot akun 2 dan mulai hitungan uji maju dari nol (restart saat posisi kosong) |
| B | Hentikan bot akun 2 |
| C | Biarkan berjalan seperti sekarang |

Rekomendasi: A atau B. Pilihan ini wewenang bos atau manajer.

**Keputusan:** opsi B dijalankan 25 Oktober 11.31 WIB (`pm2 stop`, FLAT sebelum dan sesudah). Atas jawaban bos (hentikan total, fokus ke akun 1) bot dihapus dari pm2 dan dari daftar tersimpan pukul 15.43 WIB (FLAT dicek dulu; baris cron pengawasnya dikomentari; `.env`, log, data, folder, dashboard dan tunnel tidak disentuh), sehingga restart server tidak bisa menyalakannya lagi.

## 9. Yang butuh keputusan

1. **Menerima uji maju langkah 2** dengan perkiraan sekitar −400 per 1 juta (bukan −150, dan di luar rentang −200 sampai −250 yang dijawab bos pada 25 Oktober atas fee lama) dan volume sekitar 0,5 juta per hari TURUN selama sekitar 3 minggu, atau kembali ke volume rendah. **Perlu konfirmasi ulang.**
2. **Akun 2:** sudah diputuskan, dihentikan total (bagian 8).
3. **Nilai pengaman minggu pertama** (bagian 4): bos setuju dilonggarkan setelah seminggu bila hasilnya sehat; tinjauan pertama Senin 2 November.
4. **Ambang ledger** (+4 bps, kini setara −300 per 1 juta) dan **syarat LONG dibuka** (+4 bps): diputuskan ulang pada fee 0,05% sebelum langkah 3.

## 10. Jadwal

| Kapan | Apa |
|---|---|
| Senin 26 Okt, setelah 07.00 WIB | Panggilan AI pertama langkah 2; cek instruksi yang dipakai, kondisi pasar, dan corong (selesai 26 Okt: bagian 11) |
| Sekitar 1–2 minggu | Bacaan awal di 30 trade |
| Sekitar 3 minggu atau lebih | Bacaan penuh di 60 trade (tergantung berapa lama pasar berkondisi TURUN) |
| Setiap Senin | Laporan mingguan akun 1 (akun 2 sudah dihentikan total) |

## 11. Pembaruan 26 Oktober: hari pertama langkah 2

Dibaca dari database dan log bot live (hanya membaca), sampai sekitar 17.05 WIB.

- **Instruksi dan corong.** 94 panggilan AI sejak jendela dibuka (07.02 WIB), semuanya `lean-entry-volume-v1`, semuanya sukses, sekitar 3,8 ribu token per panggilan (kira-kira 5,5 kali lebih hemat dari sekitar 21 ribu) dan 12,5 detik per panggilan. Regime gate TURUN sepanjang hari (satu baris log, 07.01 WIB), jadi tidak ada baris shadow LONG baru. Jawaban menurut stretch yang dilihat model: sisi "mengejar" (< −0,2) 35 HOLD dan 0 OPEN; sekitar rata-rata (−0,2 sampai 0) 1 HOLD; pita pantulan (0 sampai 0,75) 9 OPEN dan 1 HOLD; di atas 0,75 ATR 49 HOLD dan 0 OPEN. Tidak ada kandidat yang ditolak kode: 9 keputusan OPEN semuanya menjadi trade (keyakinan 0,77–0,82 pada 8 trade yang sudah tertutup, tinjauan risiko disetujui). 85 dari 95 keputusan (sekitar 89%) jatuh di luar pita pantulan dan semuanya HOLD: melewati panggilan AI pada tick seperti itu adalah penghematan yang jelas, tetapi sengaja ditahan sampai bacaan 30 trade supaya kode tetap beku selama uji maju.

| # | Tutup (WIB) | Gross (bps) | Bersih (USDT) | Keluar | Kemiringan EMA21 3 menit saat masuk (ATR per 5 bar) |
|---|---|---|---|---|---|
| 806 | 08.15 | +14,2 | +10,62 | time_stop | −0,27 |
| 807 | 08.30 | +33,8 | +59,61 | neutral_exit | −0,27 |
| 808 | 09.20 | +32,2 | +55,55 | neutral_exit | −0,27 |
| 809 | 10.50 | +32,8 | +56,99 | neutral_exit | −0,34 |
| 810 | 14.06 | −2,1 | −30,24 | time_stop | +0,54 |
| 811 | 15.03 | +31,6 | +54,10 | take_profit | +0,37 |
| 812 | 15.27 | −31,7 | −104,38 | stop_loss | −0,87 |
| 813 | 16.19 | −35,6 | −114,06 | stop_loss | +0,67 |

- **Hasil.** 8 trade tertutup, 5 menang, bersih −11,80 USDT setelah fee 199,87, gross +188,06 USDT (+9,4 bps per trade), volume 399.733, jadi −30 per 1 juta. Satu SHORT baru dibuka pukul 17.04 WIB dan belum tertutup saat ini ditulis.
- **Dua stop sore.** #812: masuk tepat di EMA (stretch 0,03), kemiringan EMA −0,87, ATR 0,13%; BTC melonjak sekitar +0,25% dalam 3 menit lalu kena stop (+0,317%), tanpa peringatan di fitur. #813: kemiringan EMA +0,67, RSI 67,5, +0,25% dalam satu jam terakhir, harga −0,14 ATR dari SMA20 1 jam; model melihat kemiringan naik itu tetapi tetap membuka, karena instruksinya hanya menahan diri untuk "gerakan menentukan" melawan arah; harga naik pelan selama 18 menit sampai stop (+0,356%). BTC naik dari 79.442 (10.50) ke sekitar 80.522 (+1,4%) sementara label jam tetap TURUN: SMA20/50 1 jam dan EMA50 4 jam (sekitar 1,6% di atas harga) lambat berbalik.
- **Pola awal (dugaan, bukan temuan).** Masuk saat kemiringan EMA21 3 menit negatif: 5 trade, 4 menang, 1 stop (#812). Positif: 3 trade, 1 menang, jumlah −$90. Hanya tiga trade, dan pola ini baru diintip dari data yang sama, jadi harus diuji dulu pada 139 sampel mekanis 8–23 Oktober dan trade ke depan sebelum menjadi aturan. Kode dan instruksi tetap dibekukan sampai bacaan 30 trade.
- **Titik impas per trade.** Kemenangan di target neutral sekitar +$55; satu stop sekitar −$105 sampai −$115: titik impas kira-kira 65% menang. Hari ini 5 dari 8 (62,5%).
