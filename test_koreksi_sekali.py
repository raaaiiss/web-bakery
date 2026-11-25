"""Test koreksi batch: karyawan hanya bisa sekali, admin bebas.
Jalankan semua test:  python3 -m unittest -v
"""
import unittest

import app
from test_fase2 import Dasar


class TestKoreksiSekali(Dasar):
    def batch_baru(self, klien, qty="10"):
        klien.post("/produksi", cake_id="1", qty=qty)
        return self.db().execute("SELECT id FROM production ORDER BY id DESC").fetchone()[0]

    def qty(self, bid):
        return self.db().execute("SELECT qty FROM production WHERE id = ?", (bid,)).fetchone()[0]

    def test_karyawan_hanya_sekali(self):
        _, klien = self.karyawan("koreksi1", "produksi")
        bid = self.batch_baru(klien)
        _, body = klien.get("/produksi")
        self.assertIn("Koreksi hanya bisa <b>sekali</b>", body)

        self.assertEqual(klien.post(f"/produksi/{bid}/ubah", qty="9")[0], 200)
        self.assertEqual(self.qty(bid), 9)
        s, _ = klien.post(f"/produksi/{bid}/ubah", qty="8")          # kedua kali ditolak
        self.assertEqual(s, 403)
        self.assertEqual(klien.post(f"/produksi/{bid}/hapus")[0], 403)  # hapus juga tidak bisa lagi
        self.assertEqual(self.qty(bid), 9)

        _, body = klien.get("/produksi")
        self.assertIn("Sudah dikoreksi", body)
        self.assertNotIn(f'action="/produksi/{bid}/ubah"', body)

    def test_admin_bebas_koreksi_berkali_kali(self):
        _, klien = self.karyawan("koreksi2", "produksi")
        bid = self.batch_baru(klien)
        klien.post(f"/produksi/{bid}/ubah", qty="9")                 # karyawan sudah pakai jatahnya
        for n in ("8", "7", "6"):
            self.assertEqual(self.admin.post(f"/produksi/{bid}/ubah", qty=n)[0], 200)
        self.assertEqual(self.qty(bid), 6)
        _, body = self.admin.get(f"/produksi?tanggal={app.today()}")
        self.assertIn(f'action="/produksi/{bid}/ubah"', body)
        self.assertNotIn("Koreksi hanya bisa", body)

    def test_hapus_dihitung_sebagai_koreksi(self):
        _, klien = self.karyawan("koreksi3", "produksi")
        bid = self.batch_baru(klien)
        self.assertEqual(klien.post(f"/produksi/{bid}/hapus")[0], 200)
        self.assertIsNone(self.db().execute("SELECT id FROM production WHERE id = ?", (bid,)).fetchone())

    def test_batch_lain_tetap_punya_jatah_sendiri(self):
        _, klien = self.karyawan("koreksi4", "produksi")
        a, b = self.batch_baru(klien, "5"), self.batch_baru(klien, "6")
        klien.post(f"/produksi/{a}/ubah", qty="4")
        self.assertEqual(klien.post(f"/produksi/{b}/ubah", qty="7")[0], 200)
        self.assertEqual(self.qty(b), 7)


if __name__ == "__main__":
    unittest.main()
