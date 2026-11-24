# Pencatatan Toko Kue (prototype, fase 2)

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

Belum ada (fase 3): unduh Excel, backup otomatis, push ke GitHub.

### Pembaruan database
Saat `app.py` versi fase 2 pertama kali jalan, database lama otomatis dinaikkan ke versi 2: hanya menambah
tabel `user_perms`, `wage_payments` dan kolom `users.pct`, `users.gaji_mulai`. Tidak ada data yang dihapus.
Karyawan lama tetap punya akses input produksi. Tetap salin `data/toko.db` dulu sebelum memasang versi baru.

Tampilan: krem kertas, tinta cokelat kakao, tombol cokelat roti; navigasi bawah di HP, rel kiri di tablet.
Font Karla dan Playfair Display (lisensi SIL Open Font License) disimpan di `static/fonts/`,
jadi tampilan tidak bergantung pada internet.

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
| `KUE_TZ` | `Asia/Jakarta` | zona waktu untuk "hari ini" |

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

`test_app.py` (fase 1) dan `test_fase2.py` (migrasi, akses + 403, gaji, filter laporan) menyalakan server
sungguhan dengan database sementara. Alur satu hari fase 1:
produksi beberapa batch, koreksi, sisa, laporan, ganti harga, dan batas akses karyawan.

## Rumus (versi 1)

- terjual = dibuat − sisa
- omzet = terjual × harga jual
- kerugian dari sisa = sisa × harga modal
- untung = omzet − (terjual × harga modal) **(asumsi, menunggu konfirmasi pemilik)**
- untung setelah sisa = untung − kerugian dari sisa (ditampilkan juga sebagai pembanding)

Kalau harga kue diganti di tengah hari, laporan hari itu memakai harga batch terakhir.
