"""Test pilihan di atas tab Sisa: 'Catat sisa' dan 'Sisa per hari' selalu kelihatan di bagian atas.
Jalankan semua test:  python3 -m unittest -v
"""
import unittest

import app
from test_fase2 import Dasar

TAB = 'class="tab-sisa"'


class TestTabSisa(Dasar):
    def test_tab_sisa_ada_meski_belum_ada_produksi(self):
        s, body = self.admin.get("/sisa?tanggal=2020-01-01")
        self.assertEqual(s, 200)
        self.assertIn("Belum ada produksi", body)
        self.assertIn('<a href="/sisa-harian">Sisa per hari</a>', body)
        self.assertIn('<a href="/sisa" aria-current=page>Catat sisa</a>', body)

    def test_tab_di_atas_daftar_kue(self):
        self.admin.post("/produksi", cake_id="1", qty="5", tanggal=app.today())
        _, body = self.admin.get("/sisa")
        self.assertLess(body.index(TAB), body.index('id="f-sisa"'))
        self.assertNotIn("Lihat sisa per kue per hari", body)  # tautan lama di bawah daftar sudah diganti

    def test_halaman_sisa_per_hari_menandai_tabnya(self):
        _, body = self.admin.get("/sisa-harian")
        self.assertIn('<a href="/sisa-harian" aria-current=page>Sisa per hari</a>', body)
        self.assertIn('<a href="/sisa">Catat sisa</a>', body)

    def test_tanpa_akses_sisa_tidak_ada_tab(self):
        _, klien = self.karyawan("lapor1", "omzet")
        s, body = klien.get("/sisa-harian")
        self.assertEqual(s, 200)
        self.assertNotIn(TAB, body)  # tidak menawarkan halaman yang tidak boleh dibuka


if __name__ == "__main__":
    unittest.main()
