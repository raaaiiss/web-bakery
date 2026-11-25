"""Test tombol Kembali: ada di semua halaman kecuali Beranda, di bawah header dan di atas judul.
Jalankan semua test:  python3 -m unittest -v
"""
import re
import unittest

from test_fase2 import Dasar


def tombol(body):
    """Kembalikan href tombol Kembali, atau None kalau tidak ada."""
    m = re.search(r'<a class="kembali" href="([^"]*)"', body)
    return m.group(1) if m else None


class TestTombolKembali(Dasar):
    def test_tidak_ada_di_beranda(self):
        _, body = self.admin.get("/")
        self.assertIsNone(tombol(body))

    def test_ada_di_semua_halaman_lain_dengan_tujuan_yang_pas(self):
        uid, _ = self.karyawan("hapus1", "produksi")
        harapan = {
            "/lainnya": "/", "/produksi": "/", "/sisa": "/", "/laporan": "/",
            "/sisa-harian": "/sisa", "/gaji": "/lainnya", "/kue": "/lainnya",
            "/pengguna": "/lainnya", "/riwayat": "/lainnya", f"/pengguna/{uid}/hapus": "/pengguna",
        }
        for path, tujuan in harapan.items():
            s, body = self.admin.get(path)
            self.assertEqual(s, 200, path)
            self.assertEqual(tombol(body), tujuan, path)

    def test_posisi_di_bawah_header_di_atas_judul(self):
        _, body = self.admin.get("/gaji")
        atas, kembali = body.index('class="atas"'), body.index('class="kembali"')
        alis, h1 = body.index('class="alis">Gaji berjalan'), body.index("<h1>Gaji</h1>")
        self.assertLess(atas, kembali)
        self.assertLess(kembali, alis)
        self.assertLess(alis, h1)
        self.assertIn(">Kembali</a>", body)

    def test_ramah_jari(self):
        _, body = self.admin.get("/gaji")
        self.assertRegex(body, r"\.kembali\{[^}]*min-height:44px")
        self.assertIn("m15 18-6-6 6-6", body)  # ikon panah kiri

    def test_karyawan_tanpa_menu_lainnya_kembali_ke_beranda(self):
        _, klien = self.karyawan("kembali1", "kue")
        s, body = klien.get("/kue")
        self.assertEqual(s, 200)
        self.assertEqual(tombol(body), "/")

    def test_halaman_galat_kembali_ke_halaman_sebelumnya(self):
        s, body = self.admin.get("/tidak-ada")
        self.assertEqual(s, 404)
        self.assertIn('class="kembali" href="/" data-riwayat', body)
        self.assertEqual(body.count(">Kembali<"), 1)  # tidak dobel dengan tautan lama


if __name__ == "__main__":
    unittest.main()
