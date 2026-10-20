# Analisis kerugian trade: bot SHORT live dan bot LONG forward

Tanggal: 20 Oktober 2026 · Data: Supabase (trade tertutup, verdict exit AI, keputusan entry) dan
candle BTC-USDT VST, semuanya dibaca tanpa mengubah apa pun.

## Ringkasan

- **Bot SHORT live: −899,6 USDT dari 40 trade** (12 Oktober – 20 Oktober 2026), atau **−450 per
  1 juta** volume. Sebelum fee hasilnya −96 per 1 juta. Fee menambah −354 per 1 juta.
- **Penyebab utama: fee tidak tertutup oleh keunggulan entry.** Entry yang netral (gross 0) pun
  tetap rugi sekitar −354 per 1 juta. Untuk sampai ke target −150, gross harus sekitar +200 per
  1 juta (sekitar 4 bps per trade). Yang tercapai justru negatif.
- **Kerugian terkumpul di SHORT yang dibuka saat pasar tidak sedang TURUN.** 23 trade di kondisi
  NAIK, SIDEWAYS dan TIDAK_ENTRY memberi −817 dari total −900. Di kondisi TURUN, 17 trade hampir
  impas (−82,5).
- **8 trade terakhir (19–20 Oktober) semuanya rugi: −573 USDT, 0 menang.** Semua dibuka di luar kondisi TURUN
  (SIDEWAYS, NAIK, TIDAK_ENTRY); tiga yang pertama saat BTC baru naik 0,4–0,8% dalam 3 jam. Sebelum itu (32 trade, 12–16 Oktober) hasilnya −204 per
  1 juta dengan gross +146 per 1 juta.
- **AI exit bukan penyebab utama di bot live.** Kalau posisi yang ditutup AI dibiarkan berjalan
  dengan aturan tetap, hasilnya kurang lebih sama (−1.088 lawan −1.062 aktual).
- **Bot LONG forward: −188,8 USDT dari 6 trade** (−629 per 1 juta, fee 0,04% per kaki). Di sini
  AI exit yang merugikan: aturan tetap akan memberi sekitar −13. Tapi 6 trade belum cukup untuk
  disimpulkan.
- **Sampel kecil.** 40 trade masih di bawah batas 60 yang kita pakai untuk menyimpulkan. Angka
  per kelompok di bawah adalah petunjuk, bukan bukti, dan perlu diuji lagi sebelum dipakai.

## Cara membaca

- **Per 1 juta** = 1.000.000 × jumlah PnL bersih / jumlah volume (kedua kaki). Satu trade bot
  ini bernilai 50.000 USDT volume (margin 250 × leverage 100, dua kaki).
- **Gross** = hasil sebelum fee. **Net** = setelah fee.
- **Kondisi pasar** dihitung dengan aturan pemilih mode dari dokumen Modul LONG: SMA20/SMA50 dan
  ATR14 di candle 1 jam, EMA50 di candle 4 jam, dibaca pada candle yang sudah tertutup saat entry.
- **Kisaran ketidakpastian (±)** = satu standar error. Untuk 40 trade sekitar ±177 per 1 juta.

## Bot SHORT live: 40 trade sejak 11 Oktober

| | Per 1 juta |
|---|---|
| Gross (sebelum fee) | −96 |
| Fee (0,035% per kaki, rata-rata 17,7 USDT per trade) | −354 |
| **Net** | **−450** |

| | Nilai |
|---|---|
| Menang | 12 dari 40 (30%) |
| Rata-rata trade untung | +54,8 USDT |
| Rata-rata trade rugi | −55,6 USDT |
| Titik impas win rate | sekitar 50% |

### Faktor 1: fee adalah beban terbesar

Fee menyumbang −354 dari −450 per 1 juta. Biaya per trade hampir tetap (17,7 USDT), sedangkan
keunggulan entry tidak ada. Setiap trade tambahan tanpa keunggulan menambah rugi sekitar 17,7 USDT.

### Faktor 2: win rate jauh di bawah titik impas

Profit dibatasi oleh floor +56 net (pergerakan +0,32%), sedangkan rugi bisa sampai stop
0,28–0,40%. Dengan rata-rata untung dan rugi yang hampir sama besar, titik impasnya sekitar 50%.
Yang tercapai hanya 30%.

### Faktor 3: entry tidak lebih baik dari acak

- **17 dari 40 trade tidak pernah bergerak ≥ 0,05% ke arah posisi** selama dipegang. Harga
  langsung berbalik setelah entry.
- **15 dari 40 (37%) sempat menyentuh +0,32% dalam 60 menit.** Ini sama dengan peluang acak untuk
  target dan stop sebesar ini.

### Faktor 4: SHORT di pasar yang tidak sedang turun

| Kondisi pasar saat entry | Trade | Menang | Net (USDT) | Per 1 juta | ± |
|---|---|---|---|---|---|
| TURUN | 17 | 7 | −82,5 | −97 | 297 |
| NAIK | 7 | 1 | −302,7 | −865 | 401 |
| TIDAK_ENTRY | 10 | 2 | −368,4 | −737 | 295 |
| SIDEWAYS | 6 | 2 | −145,9 | −486 | 444 |

- Di kondisi TURUN, gross-nya **+253 per 1 juta**: satu-satunya kelompok yang punya keunggulan
  sebelum fee.
- Di luar TURUN (23 trade): **−711 per 1 juta**. Bot live tidak punya filter kondisi pasar,
  jadi tetap SHORT saat BTC naik.

### Faktor 5: 8 trade terakhir, semuanya di luar kondisi TURUN

| Periode | Trade | Menang | Net (USDT) | Per 1 juta | Gross per 1 juta |
|---|---|---|---|---|---|
| 12–16 Oktober | 32 | 12 | −326,7 | −204 | +146 |
| 19–20 Oktober | 8 | 0 | −572,8 | −1.432 | −1.063 |

Semua 8 trade terakhir dibuka di kondisi SIDEWAYS, NAIK atau TIDAK_ENTRY. Tiga yang pertama
(19 Oktober pagi) dibuka saat BTC baru naik 0,4–0,8% dalam 3 jam. Aturan "AI baru boleh exit setelah 10 menit"
mulai berlaku 16 Oktober 20:50 WIB, jadi waktunya bersamaan. Tapi posisi yang ditutup AI di
periode ini akan lebih rugi kalau ditahan (kebanyakan kena stop), sehingga penyebabnya adalah
kondisi pasar, bukan aturan exit.

### Faktor 6: mengejar gerakan

| Arah SHORT terhadap gerakan 3 jam sebelum entry | Trade | Menang | Per 1 juta | ± |
|---|---|---|---|---|
| Searah (harga sudah turun) | 22 | 5 | −641 | 243 |
| Melawan (harga sempat naik) | 18 | 7 | −217 | 254 |

Di kondisi TURUN saja: SHORT yang melawan gerakan 3 jam (menjual saat harga memantul) 7 trade,
4 menang, **+156 USDT**. SHORT yang searah 10 trade, 3 menang, −239 USDT. Sampelnya sangat
kecil, tapi polanya sama dengan temuan sebelumnya (SHORT + tren turun + dekat rata-rata).

### Faktor 7: AI exit bukan penyebab utama di bot live

25 dari 40 trade ditutup AI, rata-rata −42,5. Simulasi "tahan sampai stop, floor +56, atau 60
menit" dari saat AI menutup posisi: −1.087,7 lawan −1.062,2 aktual. Hasilnya kurang lebih sama.
AI exit hanya memotong rugi yang sudah terjadi.

### Faktor 8: skor keyakinan AI tidak membawa informasi

- Semua trade punya confidence 0,75–0,77, jadi angka ini tidak bisa dipakai memilih entry.
- Dari 1.921 keputusan entry: HOLD 1.269, OPEN_SHORT 311, OPEN_LONG 341. Semua 341 LONG diblokir
  (bot ini hanya SHORT), dan 270 dari 311 SHORT diblokir karena harga terlalu jauh dari rata-rata.
  Artinya bot sering trading melawan bacaan AI-nya sendiri.

### Faktor 9: jam trading (petunjuk saja)

| Jam WIB | Trade | Menang | Net (USDT) |
|---|---|---|---|
| 00–06 | 6 | 3 | +14,8 |
| 06–12 | 14 | 3 | −526,7 |
| 12–18 | 12 | 3 | −329,3 |
| 18–24 | 8 | 3 | −58,4 |

Sampel per jam terlalu kecil untuk dipakai sebagai aturan.

### Yang bukan faktor

- **Jarak dari rata-rata (stretch).** Entry yang dekat rata-rata (|stretch| ≤ 0,35) tidak lebih
  baik: 8 trade, 1 menang, rata-rata −32,5.
- **Slippage.** Diukur 18 Oktober: sekitar 0,3 bps di entry, jauh di bawah fee.

## Bot LONG forward: 6 trade (19 Oktober)

| | Nilai |
|---|---|
| Net | −188,8 USDT (−629 per 1 juta) |
| Gross | −213 per 1 juta |
| Fee | −417 per 1 juta (0,04% per kaki di akun kedua) |
| Menang | 1 dari 6 |

- **Semua 6 trade ditutup AI exit**, dan di sini AI exit merugikan. Simulasi aturan tetap dari
  saat AI menutup posisi: sekitar −13 USDT, bukan −189. Dua posisi yang dipotong AI kemudian
  menyentuh target +0,32%.
- Alasan AI menutup hampir selalu sama: "faktor tren berbalik bearish". Faktor jangka pendek
  berbalik dalam hitungan menit, sedangkan gate kondisi pasar membaca candle 1 jam.
- AI masih memilih SHORT di 43 dari 126 keputusan (diblokir).
- 6 trade terlalu sedikit untuk disimpulkan.

## Usulan untuk didiskusikan

Ini usulan untuk diuji, bukan keputusan. Semua perlu persetujuan, dan sebaiknya diuji dulu di
backtest atau forward test terpisah sebelum mengubah bot live.

1. **SHORT hanya saat kondisi TURUN.** Gate kondisi pasar yang sudah dipakai bot forward, untuk
   arah sebaliknya. Pengaruh: dari 40 trade, 23 tidak akan dibuka (−817 USDT), tapi volume
   turun sekitar 57%. Ini bertentangan dengan target volume 1 juta per hari.
2. **Di kondisi TURUN, SHORT saat harga memantul, bukan setelah turun.** Bukti masih tipis
   (7 trade), perlu diuji di backtest dulu.
3. **Fee lebih rendah lewat entry maker** (akun kedua: maker 0,014% lawan taker 0,04%). Saat ini
   kita taker saja, sehingga perlu keputusan.
4. **Bot forward: kurangi AI exit yang memotong posisi terlalu cepat**, misalnya hanya
   menghormati AI exit setelah rugi lebih dalam, atau setelah waktu lebih lama. Tunggu sampai
   sampelnya cukup.
5. **Kurangi trade, perbesar seleksi.** Setiap trade tanpa keunggulan menambah rugi sekitar 17,7
   USDT. Volume target dan hasil per 1 juta saling tarik-menarik, dan prioritasnya perlu
   ditetapkan.
