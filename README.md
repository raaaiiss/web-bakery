# Pencatatan Toko Kue (versi 1, prototype)

Pengganti buku catatan: produksi harian, sisa saat tutup, dan laporan harian (omzet, untung, kerugian dari sisa).

Isi versi 1:
- Login. Akun admin pertama dibuat lewat halaman pertama saat database masih kosong.
- Admin: tambah/ubah kue dan harga, input sisa, laporan harian, tambah karyawan, buatkan password baru, riwayat perubahan.
- Karyawan: input produksi per batch (dijumlahkan), koreksi batch sendiri di hari yang sama.
- Harga disalin ke tiap batch, jadi ganti harga tidak mengubah catatan lama.
- Semua perubahan tercatat di menu Riwayat.

Belum ada (fase 2 dan 3): gaji, hak akses per orang, laporan 7 hari/bulanan, unduh Excel, backup otomatis.

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

Test menyalakan server sungguhan dengan database sementara, lalu menjalankan alur satu hari:
produksi beberapa batch, koreksi, sisa, laporan, ganti harga, dan batas akses karyawan.

## Rumus (versi 1)

- terjual = dibuat − sisa
- omzet = terjual × harga jual
- kerugian dari sisa = sisa × harga modal
- untung = omzet − (terjual × harga modal) **(asumsi, menunggu konfirmasi pemilik)**
- untung setelah sisa = untung − kerugian dari sisa (ditampilkan juga sebagai pembanding)

Kalau harga kue diganti di tengah hari, laporan hari itu memakai harga batch terakhir.
