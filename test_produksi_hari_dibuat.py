"""Test: input produksi tercatat di tanggal kue dibuat (hari ini), tanpa pilihan "Dijual untuk".
Jalankan semua test:  python3 -m unittest -v
"""
import unittest

import app
from test_fase2 import Dasar, hari


class TestProduksiHariDibuat(Dasar):
    def test_bawaan_hari_ini_tanpa_pilihan_besok(self):
        _, klien = self.karyawan("buat1", "produksi")
        s, body = klien.get("/produksi")
        self.assertEqual(s, 200)
        self.assertIn(f'name="tanggal" value="{app.today()}"', body)
        self.assertNotIn("Dijual untuk", body)
        self.assertNotIn("Besok", body)

    def test_batch_karyawan_masuk_hari_ini_dan_ditagih_sisa_hari_ini(self):
        _, klien = self.karyawan("buat2", "produksi")
        klien.post("/produksi", cake_id="2", qty="41", tanggal=hari(1))  # tanggal lain diabaikan untuk karyawan
        d = self.db()
        self.assertEqual(d.execute("SELECT date FROM production ORDER BY id DESC").fetchone()[0], app.today())
        self.assertIn("dibuat 41", self.admin.get(f"/sisa?tanggal={app.today()}")[1])


if __name__ == "__main__":
    unittest.main()
