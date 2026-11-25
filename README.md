# Pencatatan Web Bakery

Pengganti buku catatan: produksi harian, sisa saat tutup, laporan, dan gaji pembuat kue.

Fase 1:
- Login. Akun admin pertama dibuat lewat halaman pertama saat database masih kosong.
- Input produksi per batch (dijumlahkan), koreksi batch sendiri di hari yang sama; setelah itu hanya admin.
- Input sisa saat tutup. Harga disalin ke tiap batch, jadi ganti harga tidak mengubah catatan lama.
- Semua perubahan tercatat di menu Riwayat.

Fase 2:
- Hak akses dicentang per karyawan: input produksi, input sisa, lihat omzet, lihat untung, lihat gaji sendiri,
  lihat harga modal, kelola kue & harga. Kelola user khusus admin. Karyawan baru mulai tanpa akses.
  Server menolak (403) aksi yang tidak dicentang.
- Gaji berjalan = persen karyawan (default 25%) × harga jual kue yang terjual sejak pembayaran terakhir.
  "Tandai sudah dibayar" menyimpan riwayat dan hitungan mulai lagi dari nol besoknya. Gaji tidak dikurangkan dari untung.
- Laporan: hari ini, 7 hari, bulan ini, rentang tanggal.
- Menerima gaji (Ya/Tidak) per karyawan, terpisah dari centang "Lihat gaji sendiri". Yang Tidak: tidak dihitung,
  tidak muncul di halaman Gaji/Beranda, tidak bisa ditandai dibayar. Ya -> Tidak dengan gaji belum dibayar minta
  konfirmasi. Akun baru default Tidak. Migrasi: karyawan dengan persen > 0 = Ya, 0% = Tidak, admin = Tidak.
- Gaji berjalan diberi label **sementara** (ada sisa belum diisi, dianggap terjual) atau **pasti**, plus baris
  "Gaji hari ini". Tidak ada muat ulang otomatis; ikon muat ulang ada di kanan judul Beranda dan Gaji. Admin bisa mengatur "Gaji dihitung mulai".
- Hapus user = nonaktifkan: tidak bisa masuk, sesi dihentikan, hilang dari daftar aktif dan Gaji; nama tetap di catatan
  lama; username tidak bisa dipakai lagi. Tidak bisa hapus diri sendiri atau admin terakhir.
- Pengambilan gaji (pilihan C): saldo = gaji terkumpul periode ini - yang sudah diambil. "Ambil sebagian" mengurangi
  saldo tanpa menutup periode; "Bayar semua" membayar seluruh saldo dan menutup periode. Saldo negatif = diambil lebih,
  dikurangkan dari gaji berikutnya. Pengambilan yang salah catat bisa dibatalkan (ditandai, tidak dihapus).

Tampilan baru:
- Beranda: header berwarna (penjualan hari ini, jumlah batch, terjual, sisa), menu berkelompok dalam kartu
  bersudut membulat, laporan hari ini, dan kue terlaris.
- Pilih kue lewat kartu bergambar di Produksi; jumlah dan tombol Simpan ada di bar bawah (zona jempol).
- Halaman Kue: foto atau warna per kue. Foto diperkecil di browser (640x480 JPEG) lalu disimpan di
  `data/foto/` (ikut folder data, tidak masuk git). Tanpa foto, kartu memakai warna + inisial nama.
- Sisa per batch: halaman Sisa menampilkan batch hari itu; halaman "Sisa per hari" menampilkan sisa
  per kue per hari (habis / ada sisa / belum diisi) dan daftar batch per tanggal buat.

Fase 3:
- **Unduh Excel** (khusus admin): menu Lainnya → Unduh Excel. Satu file `.xlsx` berisi lembar Produksi, Sisa,
  Gaji (saldo sekarang), dan Riwayat gaji. Angkanya memakai rumus yang sama dengan halaman Laporan dan Gaji.
  Dibuat dengan modul bawaan Python, tanpa pustaka tambahan.
- **Backup otomatis**: **sekali sehari** jam `KUE_BACKUP_JAM` (default 00:05 WIT), database disalin ke
  `data/backup/toko-YYYY-MM-DD.db` (berisi semua catatan sampai akhir hari sebelumnya). Kalau aplikasi mati
  pada jam itu, backup dibuat saat aplikasi menyala, tetap paling banyak sekali per hari.
  Hanya **7 hari terakhir** yang disimpan (`KUE_BACKUP_HARI`); backup harian yang lebih tua dihapus otomatis
  setelah salinan baru jadi. File lain (misalnya `data/toko-sebelum-*.db`) tidak disentuh. Memulihkan: matikan aplikasi, salin file backup ke `data/toko.db`, nyalakan lagi.

### Pembaruan database
Saat `app.py` versi fase 2 pertama kali jalan, database lama otomatis dinaikkan ke versi 2: hanya menambah
tabel `user_perms`, `wage_payments` dan kolom `users.pct`, `users.gaji_mulai`. Tidak ada data yang dihapus.
Karyawan lama tetap punya akses input produksi. Tetap salin `data/toko.db` dulu sebelum memasang versi baru.
Versi tampilan baru menambah kolom `cakes.color` dan `cakes.photo` (dicek dulu, hanya menambah).

Tampilan: krem kertas, tinta cokelat kakao, tombol cokelat roti; navigasi bawah di HP, rel kiri di tablet.
Font Karla dan Poppins (lisensi SIL Open Font License) disimpan di `static/fonts/`,
jadi tampilan tidak bergantung pada internet.

## Pasang di layar utama HP (pintasan)

Aplikasi bisa ditambahkan ke layar utama HP sebagai ikon. HP harus tersambung ke wifi toko.
Tidak ada mode offline dan tidak ada service worker: data selalu diambil dari server.

- Nama di bawah ikon diatur lewat `KUE_NAMA_TOKO` (default "Web Bakery").
- Ikon dibuat oleh `python3 tools/buat_ikon.py` ke `static/ikon/`. Logo sendiri: ganti PNG dengan ukuran yang sama
  (192, 512, maskable 512, apple-touch-icon 180).

**Android (Chrome):** buka `http://<alamat-server>:8000` → menu titik tiga ⋮ → *Tambahkan ke layar utama* → *Tambahkan*.
**iPhone (Safari, bukan Chrome):** buka alamat yang sama → tombol Bagikan (kotak dengan panah ke atas) →
*Tambahkan ke Layar Utama* → *Tambah*.

Catatan: alamat `http` (tanpa HTTPS) dianggap "tidak aman" oleh browser. Di Android, pintasannya kemungkinan
terbuka di tab Chrome biasa dengan kotak alamat, bukan layar penuh. Ini belum dites di HP sungguhan.

## Kebutuhan

Hanya Python 3.9 atau lebih baru. Tidak perlu install paket lain. Linux, Windows, dan Mac bisa.

```bash
python3 --version
```

## Menjalankan

```bash
cd toko-kue
python3 app.py
```

Lalu buka dari HP yang tersambung ke wifi toko: `http://<IP-home-server>:8000`.
Cari alamat server di Linux dengan `hostname -I`.

Data tersimpan di `data/toko.db` (satu file SQLite). Folder `data/` tidak ikut ke git.

Pengaturan lewat environment variable (opsional):

| Variabel | Default | Arti |
|---|---|---|
| `KUE_PORT` | `8000` | port web |
| `KUE_HOST` | `0.0.0.0` | `0.0.0.0` = bisa dibuka dari perangkat lain di wifi |
| `KUE_DB` | `data/toko.db` | lokasi file database |
| `KUE_TZ` | `Asia/Jayapura` | zona waktu (WIT, UTC+9) untuk jam dan "hari ini" |
| `KUE_BACKUP_DIR` | `data/backup` | folder backup harian |
| `KUE_BACKUP_JAM` | `00:05` | jam backup harian (WIT); kosong = backup otomatis mati |
| `KUE_BACKUP_HARI` | `7` | berapa hari backup harian disimpan; `0` = simpan semua |

## Nyala otomatis (Linux dengan systemd)

```bash
sudo useradd --system --home /opt/toko-kue tokokue
sudo cp -r toko-kue /opt/toko-kue && sudo chown -R tokokue: /opt/toko-kue
sudo cp /opt/toko-kue/toko-kue.service /etc/systemd/system/
sudo systemctl enable --now toko-kue
systemctl status toko-kue
```

Kalau firewall aktif (`ufw`): `sudo ufw allow from <jaringan-wifi-toko> to any port 8000`.

## Test

```bash
python3 -m unittest -v
```

`test_app.py` (fase 1), `test_fase2.py` (migrasi, akses + 403, gaji, filter laporan) dan `test_tampilan.py`
(foto/warna kue, kartu, Beranda, sisa per batch dan per hari) dan `test_pwa.py` (manifest, ikon, tag iPhone,
tanpa cache, tanpa service worker) menyalakan server
sungguhan dengan database sementara. Alur satu hari fase 1:
produksi beberapa batch, koreksi, sisa, laporan, ganti harga, dan batas akses karyawan.

## Rumus (versi 1)

- terjual = dibuat − sisa
- omzet = terjual × harga jual
- kerugian dari sisa = sisa × harga modal
- untung = omzet − (terjual × harga modal) **(asumsi, menunggu konfirmasi pemilik)**
- untung setelah sisa = untung − kerugian dari sisa (ditampilkan juga sebagai pembanding)

Kalau harga kue diganti di tengah hari, laporan hari itu memakai harga batch terakhir.
