# Laporan Perubahan Terbaru pada Bot Trading

Periode 21–25 Oktober 2026 · Disusun 25 Oktober 2026 · **Dikoreksi 26 Oktober 2026 (fee; versi 4: untuk hasil −250 per 1 juta, untung sebelum biaya yang dibutuhkan 0,05%, bukan 0,055%; biaya satu stop 100 sampai 115 USDT, bukan 92)**

Laporan ini menjelaskan dengan bahasa sederhana apa yang diubah pada bot, mengapa diubah, apa yang sudah diuji, dan apa yang belum kita ketahui. Ukuran hasilnya sama dengan laporan sebelumnya: kerugian untuk setiap 1 juta USDT nilai transaksi (disebut "per 1 juta"). Target kita: tidak lebih buruk dari −150 per 1 juta.

## Koreksi (26 Oktober): fee akun ternyata 0,05%

Laporan 25 Oktober memakai fee 0,035% per sisi. Pemeriksaan langsung ke BingX pada 26 Oktober menunjukkan fee akun sudah **0,05% per sisi** sejak 20 Oktober malam (sebelumnya 0,035%). Untuk order maker fee-nya 0,02%, tetapi bot memakai order taker. Semua angka perkiraan di laporan ini sudah dihitung ulang. Hasil nyata yang tercatat (misalnya rugi 965 USDT pada 19–23 Oktober) tidak terpengaruh karena sudah memakai fee yang sebenarnya dipotong.

| | Laporan 25 Oktober | Dikoreksi |
|---|---|---|
| Fee akun per sisi | 0,035% | **0,05%** |
| Biaya pulang-pergi satu transaksi | sekitar 0,07% | **sekitar 0,10%** |
| Untung sebelum biaya agar hasil mencapai −150 | 0,04% | **0,07%** |
| Perkiraan hasil (untung sebelum biaya 0,02%) | sekitar −250 per 1 juta | **sekitar −400 per 1 juta** |
| Untung sebelum biaya agar hasil −200 sampai −250 | 0,02–0,03% | **0,05–0,06%** |

Persetujuan bos pada 25 Oktober (hasil −200 sampai −250 tidak masalah) diberikan atas angka lama, jadi **perlu dikonfirmasi ulang** (bagian 9).

## Ringkasan

- **Masalah:** bot hampir tidak bertransaksi. Dalam 26 jam pasar turun yang seharusnya bisa dimanfaatkan, bot hanya melakukan 2 transaksi.
- **Penyebab:** petunjuk yang dibaca AI sebelum memutuskan membuatnya menjauhi satu-satunya jenis transaksi yang diizinkan aturan keselamatan. Padahal sebagian besar kerugian jenis itu adalah biaya transaksi, bukan salah menebak arah harga.
- **Perubahan:** petunjuk untuk AI ditulis ulang agar singkat dan jelas, dan batas pengaman harian diperketat selama minggu pertama. Sudah berjalan sejak Minggu 25 Oktober 2026 pukul 11.07 WIB. Dipasang saat bot belum boleh membuka transaksi (jam transaksi bot: Senin 07.00 sampai Sabtu 00.00 WIB) dan tidak ada transaksi yang sedang berjalan.
- **Uji di komputer:** pada 26 jam yang sama, petunjuk baru menghasilkan sekitar 11 transaksi, sedangkan petunjuk lama hanya 2. Ini baru membuktikan bot menjadi lebih aktif. Ini **belum** membuktikan bot untung.
- **Perkiraan yang jujur (sudah dikoreksi):** dengan fee 0,05%, hasil diperkirakan sekitar −400 per 1 juta, jauh dari target −150 dan di luar rentang −200 sampai −250 yang disetujui. Kepastiannya baru ada setelah beberapa minggu berjalan langsung.
- **Hari pertama (26 Oktober, sementara):** 8 transaksi, 5 menang, bersih −$11,80 setelah fee. Sebelum fee +$188. Dua transaksi kena batas rugi saat BTC berbalik naik di sore hari. Satu hari terlalu sedikit untuk menyimpulkan apa pun.
- **Bot kedua (akun 2)** dihentikan pada hari yang sama karena hanya melakukan 6 transaksi dalam seminggu dan hasilnya rugi.

## 1. Latar belakang

Pada 21 Oktober dipasang dua perubahan, berdasarkan analisis 519 transaksi sungguhan akun 1 sejak 31 Agustus:

- Keputusan menutup transaksi tidak lagi diserahkan ke AI, melainkan ke aturan tetap (batas rugi, target untung, dan batas waktu 60 menit), karena data menunjukkan keputusan menutup oleh AI rata-rata lebih buruk.
- Penyaring kondisi pasar: bot hanya menjual (bertaruh harga turun) pada jam ketika pasar sedang turun. Ide membeli (bertaruh harga naik) hanya dicatat di atas kertas, tanpa uang. Aturan lama yang menolak transaksi saat harga terlalu jauh dari rata-rata tetap berlaku.

Hasil minggu 19–23 Oktober menunjukkan alasannya: akun 1 melakukan 15 transaksi dan rugi 965 USDT. Dua belas dari 15 transaksi itu terjadi pada kondisi pasar yang sekarang dijauhi bot (rugi 806 USDT, hanya 1 yang menang). Penyaring baru memang memblokir jenis transaksi itu, tetapi setelah dipasang nyaris tidak ada transaksi yang lolos:

| | Hasil |
|---|---|
| Akun 1, 19–23 Oktober | 15 transaksi, rugi 965 USDT (−1.286 per 1 juta) |
| – di kondisi pasar yang kini dijauhi | 12 transaksi, rugi 806 USDT |
| Akun 1, seluruh siklus sejak 12 Oktober | 47 transaksi, volume 2,35 juta, −550 per 1 juta |
| Setelah penyaring dipasang, dalam 26 jam pasar turun | hanya 2 transaksi (sekitar 1 per hari, kira-kira 3% dari target volume harian) |

## 2. Yang kami temukan

Dalam 26 jam pasar turun itu, AI dimintai pendapat 387 kali:

| Jawaban AI | Jumlah |
|---|---|
| Menahan diri (tidak bertransaksi) | 246 |
| Mengusulkan membeli, padahal saat pasar turun bot hanya boleh menjual | 115 |
| Mengusulkan menjual, tetapi harga terlalu jauh dari rata-rata sehingga ditolak penyaring | 24 |
| Mengusulkan menjual dan diterima | **2** |

**Akar masalahnya ada pada catatan pelajaran.** Sebelum memutuskan, AI membaca ringkasan transaksi lama. Isinya kira-kira: "jenis transaksi ini rugi 1.024 USDT dari 48 transaksi; butuh bukti yang sangat jelas sebelum dibuka lagi." Padahal 86% dari kerugian itu (877 USDT) adalah biaya transaksi. Sebelum biaya, kerugiannya hanya 146 USDT, kecil sekali. Jenis transaksi itu kebetulan satu-satunya yang diizinkan penyaring, jadi hanya itu yang punya catatan. Akibatnya AI belajar menghindari biaya, bukan menghindari sinyal yang buruk.

**Temuan kedua:** petunjuk singkat yang direncanakan pada laporan 21 Oktober ternyata tidak akan menghasilkan transaksi sama sekali. Jawaban AI berkeyakinan sekitar 0,52–0,58, sedangkan program hanya menerima 0,75 ke atas dan AI tidak diberi tahu batas itu (0 dari 18 jawaban lolos dalam uji). Versi itu belum pernah dipasang dan sudah diganti.

## 3. Apa yang diubah

| | Sebelum (sampai 25 Okt, 11.07 WIB) | Sekarang |
|---|---|---|
| Petunjuk untuk AI | Panjang, memuat catatan pelajaran dan puluhan indikator; AI cenderung menahan diri | Singkat dan jelas: bila harga sempat naik mendekati rata-rata pada jam pasar turun, jual. Menahan diri hanya bila AI bisa menyebut alasannya. AI diberi tahu batas keyakinan minimal (0,75) yang dipakai program |
| Biaya pemakaian AI per keputusan | patokan (100%) | sekitar 20% dari patokan, kira-kira 5 kali lebih hemat |
| Maksimum transaksi per hari | sampai 34 | 10 |
| Jeda setelah kena batas rugi otomatis | tidak ada | 20 menit |
| Maksimum kena batas rugi per hari | 12 kali | 5 kali |
| Batas rugi harian | 1.500 USDT | 600 USDT |

**Mengapa jeda dan dua batas rugi diperketat.** Karena bot kini bisa bertransaksi sampai 10 kali sehari, satu jam yang berbalik naik bisa memicu 3–5 batas rugi berturut-turut (sekitar 100 sampai 115 USDT masing-masing). Pengaman ini dipasang untuk minggu pertama dan akan dilonggarkan bila hasilnya sehat.

**Perubahan pada program.** Program kini punya mode petunjuk baru ini. Mode itu menolak berjalan bila pengaturan pendukungnya belum lengkap, sehingga tidak bisa dinyalakan setengah-setengah. Bila mode baru tidak dinyalakan, perilaku bot sama persis dengan sebelumnya.

## 4. Hasil uji di komputer

Kami meminta AI memutuskan ulang pada 26 jam pasar turun yang sama (data nyata, tanpa uang), lalu menghitung berapa transaksi yang akan terjadi.

| Petunjuk untuk AI | Transaksi dalam 26 jam |
|---|---|
| Lama (berjalan sampai 25 Oktober) | 2 |
| Singkat versi 21 Oktober (tidak pernah dipasang) | 0 |
| **Baru (berjalan sejak 25 Oktober)** | **sekitar 11** |

Dua catatan penting:

- Uji ini hanya menunjukkan **seberapa sering** bot bertransaksi, bukan apakah untung. Untung-rugi di simulasi tidak kami anggap bukti, sebab periodenya sama dengan periode asal dugaan kami.
- Dalam uji, AI hampir selalu membuka transaksi bila syaratnya terpenuhi, dan tidak ikut mengejar harga yang sudah turun. Artinya yang kita uji ke depan terutama **aturan "jual saat harga memantul naik"**, bukan kemampuan AI memilih transaksi.

<div style="break-before: page"></div>

## 5. Perkiraan hasil dan risikonya

Setiap transaksi dikenai biaya pulang-pergi sekitar 0,10% dari nilainya (0,05% per sisi). Dugaan terbaik kami: transaksi jenis ini rata-rata untung sekitar 0,02% sebelum biaya (dengan ketidakpastian sekitar ±0,02%). Artinya masih rugi sekitar 0,08% per transaksi setelah biaya. Agar mencapai target −150 per 1 juta, rata-rata untung sebelum biaya perlu minimal 0,07%. Untuk rentang −200 sampai −250 perlu sekitar 0,05–0,06%.

| Skenario | Hasil per 1 juta | Rugi per hari pasar turun (volume sekitar 0,5 juta) |
|---|---|---|
| Untung sebelum biaya sekitar 0,02% (perkiraan kami) | sekitar −400 (kira-kira −510 sampai −290) | sekitar 200 USDT |
| Tidak ada keunggulan sama sekali | −500 | sekitar 250 USDT |
| Untung sebelum biaya 0,04% (dugaan kami terbukti) | −300 | sekitar 150 USDT |
| Untung sebelum biaya 0,05–0,06% (rentang yang disetujui bos) | −250 sampai −200 | sekitar 125 sampai 100 USDT |
| Untung sebelum biaya 0,07% (target) | −150 | sekitar 75 USDT |

- Bot hanya bertransaksi pada jam pasar turun, jadi volume rata-rata per hari kerja lebih rendah dari 0,5 juta.
- Selama keunggulan belum terbukti, volume yang lebih besar berarti kerugian yang lebih besar dalam angka mutlak. Menambah 3 juta volume pada −400 per 1 juta akan membawa angka siklus dari −550 menjadi sekitar −470.
- **Hari pertama (26 Oktober, sementara):** 8 transaksi, 5 menang, bersih −$11,80 setelah fee $199,87 (sekitar −30 per 1 juta). Sebelum fee +$188, rata-rata sekitar +0,09% per transaksi, jauh di atas perkiraan 0,02%. Empat transaksi pagi menang semua; dua transaksi sore kena batas rugi (−$104,38 dan −$114,06) ketika BTC berbalik naik. Delapan transaksi pada satu hari yang kebetulan searah dengan arah jual terlalu sedikit untuk menyimpulkan keunggulan.

## 6. Cara kami menilai, dan kapan berhenti

- **Dugaan yang diuji** (ditulis sebelum pengujian dimulai): transaksi jual yang dibuka saat harga memantul naik mendekati rata-rata, pada jam pasar turun, rata-rata untung minimal 0,04% sebelum biaya (dengan fee 0,05% itu setara sekitar −300 per 1 juta; untuk mencapai −150 perlu 0,07%).
- **Pengecekan pertama:** Senin 26 Oktober setelah pukul 07.00 WIB (memastikan petunjuk baru terpakai), lalu laporan mingguan setiap Senin.
- **Bacaan awal setelah 30 transaksi** (sekitar 1–2 minggu): bila rata-rata sebelum biaya di bawah nol, kami kembali ke pengaturan sebelumnya.
- **Bacaan penuh setelah 60 transaksi** (sekitar 3 minggu atau lebih, tergantung berapa lama pasar berkondisi turun): hasilnya dipisah antara transaksi yang dibuka saat harga di atas dan di bawah rata-rata.
- **Kembali ke pengaturan lama:** memakai cadangan, memakan waktu beberapa menit, dan hanya dilakukan saat tidak ada transaksi yang berjalan.

## 7. Perubahan lain pada minggu ini

- **Bot kedua (akun 2, uji arah beli) dihentikan** pada Minggu 25 Oktober pukul 11.31 WIB, setelah dipastikan tidak ada transaksi terbuka. Data dan pengaturannya disimpan, jadi bisa dijalankan lagi bila diputuskan. Alasannya: hanya 6 transaksi dalam seminggu (semuanya pada 19 Oktober) dengan rugi 189 USDT; keputusan menutup transaksi oleh AI lebih buruk daripada aturan tetap pada transaksi yang sama (selisih sekitar 169 USDT dari 6 transaksi); dan pemakaian AI besar untuk hasil sekecil itu. Pertanyaan yang sama kini dijawab lewat catatan percobaan di atas kertas pada akun 1, tanpa biaya.
- **Dokumen proyek diperbarui** agar sesuai keadaan terbaru, termasuk hasil uji Modul LONG "Mode L" (tidak lulus) dan analisis kerugian bot yang disusun 20 Oktober.

## 8. Bagaimana perubahan ini dipasang dengan aman

- Program diperiksa dengan 3.004 pemeriksaan otomatis (26 di antaranya baru), semuanya lolos, dan melalui dua putaran peninjauan, satu di antaranya oleh peninjau independen. Temuan pentingnya sudah diperbaiki sebelum dipasang.
- Dipasang pada hari Minggu, ketika bot belum boleh membuka transaksi. Sebelum, selama, dan sesudah pemasangan kami memastikan tidak ada transaksi yang sedang berjalan.
- Pengaturan baru diuji pada salinan sebelum dipasang, dan pengaturan lama disimpan sebagai cadangan.

## 9. Yang perlu diputuskan

1. **Melanjutkan uji jalan langsung ini** selama sekitar 3 minggu dengan perkiraan sekitar −400 per 1 juta (bukan −150, dan di luar rentang −200 sampai −250 yang sempat disetujui) dan volume sekitar 0,5 juta pada hari pasar turun, atau kembali ke volume rendah. Mohon konfirmasi ulang, karena persetujuan sebelumnya memakai fee 0,035%.
2. **Kapan melonggarkan pengaman minggu pertama.** Usul kami: setelah seminggu, bila hasilnya sehat.
3. **Akun 2:** sudah dihentikan. Keputusan baru diperlukan hanya jika ingin menjalankannya lagi (hitungan uji akan mulai dari nol).
