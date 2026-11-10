# Skill: anti-slop-ui

Tujuan: antarmuka tidak boleh terbaca sebagai hasil AI generik (layout template, tipografi default, warna tanpa alasan). Aturan di bawah bisa diperiksa dari kode HTML/CSS/JS. Pelanggaran dianggap masalah nyata, kecuali permintaan user atau brand-nya memang mengharuskan.

## 1. Sinyal slop yang dilarang

**Warna dan latar**
- Gradien ungu-ke-biru sebagai identitas default, atau gradien hero yang tidak berkaitan dengan brand.
- Teks headline dengan gradien lewat `background-clip: text` / `bg-clip-text`.
- Warna aksen violet/indigo/purple tanpa alasan semantik. Kalau brand memang ungu, itu keputusan. Kalau tidak, itu default.
- Glassmorphism (blur + border + shadow) di setiap permukaan tanpa fungsi struktural.

**Layout dan struktur**
- Hero di tengah: pill kecil di atas + headline + dua tombol, lalu grid 3 kartu fitur dengan ikon di kotak rounded, lalu footer.
- Semua elemen memakai `border-radius` yang sama. Variasikan radius sesuai peran komponen.
- Kartu di dalam kartu di dalam kartu tanpa alasan hierarki.
- Semua section memakai `shadow` yang sama, atau semua kartu punya hover `scale(1.05)`.

**Tipografi**
- Inter, Geist, Space Grotesk, Roboto, atau Arial sebagai satu-satunya font tanpa pemilihan yang disengaja. Mengganti font default adalah perubahan tunggal yang paling membedakan halaman.
- Satu weight dan satu ukuran di seluruh halaman, tanpa hierarki.
- Italic pada heading. Untuk penekanan pakai weight, warna aksen, atau underline.

**Konten dan ikon**
- Metrik karangan: "+47% conversion", "trusted by 50,000+ teams", "10x faster" tanpa data dari user. Ini pelanggaran paling merusak karena membuat bukti palsu.
- Emoji sebagai ikon fitur (🚀 ⚡ ✨ 🎯). Pakai SVG sederhana dari satu set yang konsisten, atau tanpa ikon.
- Ikon robot, sparkle, tongkat sihir, atau starburst sebagai bahasa visual default untuk "AI".

## 2. Disiplin wajib

**Token terkunci**
Setelah palet dan font dipilih, definisikan sekali di `:root` sebagai variabel (`--color-accent`, `--font-display`). Semua warna dan `font-family` di CSS harus memakai `var(...)`. Nilai hex/rgb/oklch tidak boleh muncul di luar `:root`. Butuh nilai baru? Buat token baru dulu.

**Tipografi dengan kontras**
- Weight ekstrem (200 vs 800), bukan 400 vs 600.
- Ukuran hero minimal 3x teks isi (misalnya 72px vs 18px). Antar level heading pakai rasio 1.25 sampai 1.5.
- Font non-default boleh lewat Google Fonts atau font sistem yang dipilih sadar. Selalu sertakan fallback.

**Warna yang berkomitmen**
Tiap warna punya peran: background, surface, text-primary, text-muted, accent, success, error. Warna dekoratif tanpa peran dihapus atau diberi peran.

**Struktur bervariasi**
Pilih satu makrostruktur yang cocok dengan isi (editorial, hero asimetris, satu section bukti, detail dalam bentuk naratif). Jangan ikuti pola default hero, grid fitur, CTA. Tombol utama dan sekunder harus berbeda jelas (weight, warna, atau ukuran), bukan dua tombol yang sama.

**Aman di layar kecil (tulis CSS-nya sejak awal)**
- Rancang untuk lebar 320 sampai 768 px. Tidak boleh ada scroll horizontal: pakai `overflow-x: clip` pada `html` dan `body`, bukan `hidden`.
- Teks klikabel (tombol, link nav, CTA) tidak boleh pecah jadi dua baris: `white-space: nowrap` atau ukuran yang cukup.
- Grid yang berisi gambar memakai `minmax(0, 1fr)`, bukan `1fr` polos.

**Konten jujur**
Kalau user tidak memberi angka, jangan mengarang. Pakai placeholder yang jelas (`—` dengan label "perlu dikonfirmasi") atau pilih struktur yang tidak butuh angka.

## 3. Sebelum menulis file
1. Pilih satu arah estetika yang spesifik: brutally minimal, maximalist, editorial, organic, luxury, atau retro-futuristic. "Clean and modern" bukan arah.
2. Pilih makrostruktur dan tetapkan token di `:root`.
3. Audit sendiri, lalu perbaiki pelanggaran sebelum menulis file:
   - Apakah halaman ini bisa jadi milik 50 situs lain? Kalau ya, arahnya belum spesifik.
   - Apakah tiap komponen punya tugas, atau hanya dekorasi?
   - Apakah tipografi punya sistem (weight, ukuran, line-height yang disengaja)?
   - Apakah tampilan mobile punya ritmenya sendiri, bukan desktop yang ditumpuk?
   - Apakah semua warna dan font memakai token?
