# Skill: anti-slop (kode)

Tujuan: kode terlihat ditulis orang yang peduli, bukan keluaran generik. Khusus kode dan struktur proyek. Tampilan diatur skill anti-slop-ui, tulisan diatur anti-slop-id. Aturan di bawah bisa diperiksa dari kode, dan pelanggaran dianggap masalah nyata.

- Tidak ada tombol, menu, atau tautan yang mati (`href="#"` tanpa tujuan, `onclick` kosong, handler yang tidak dipasang).
- Tidak ada komentar yang cuma mengulang isi kode ("// fungsi untuk menjumlah"). Komentar hanya untuk alasan atau hal yang tidak jelas.
- Tidak ada fungsi, file, variabel, atau dependensi yang tidak dipakai.
- Nama variabel dan file jelas dan konsisten. Tidak ada `foo`, `temp`, `data1`.
- Tangani error yang masuk akal (input kosong, fetch gagal, file tidak ada) dengan pesan yang berguna untuk pengguna, bukan hanya `console.log`.
- Semua rahasia lewat `.env`, tidak pernah ditulis di kode. Sediakan `.env.example`.
- Data yang ditampilkan harus berasal dari data user atau sumber yang jelas. Tidak mengarang proyek, angka, testimoni, atau nama.
