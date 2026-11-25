"""Test tombol profil di kanan atas: menu kecil berisi Keluar, dan keluar benar-benar mengakhiri sesi.
Jalankan semua test:  python3 -m unittest -v
"""
import unittest

from test_app import Klien
from test_fase2 import Dasar

class TestProfilKeluar(Dasar):
    def halaman(self, klien, path):
        """Ambil halaman beserta header balasannya (Klien biasa hanya mengembalikan isi)."""
        with klien.op.open(klien.base + path) as r:
            return r.status, r.headers, r.read().decode()

    def test_menu_profil_ada_di_semua_halaman_berheader(self):
        for path in ("/", "/produksi", "/sisa", "/laporan", "/lainnya"):
            s, _, body = self.halaman(self.admin, path)
            self.assertEqual(s, 200, path)
            self.assertIn('class="profil"', body, path)
            self.assertIn(">Keluar<", body, path)
            self.assertIn("Pemilik", body, path)  # nama akun tampil di menu

    def test_target_sentuh_dan_tanda_bisa_ditekan(self):
        _, _, body = self.halaman(self.admin, "/")
        self.assertIn("min-height:44px", body)  # tombol profil dan Keluar cukup besar untuk jari
        self.assertIn('aria-label="Akun Pemilik, buka menu"', body)
        self.assertIn("m6 9 6 6 6-6", body)  # panah kecil penanda menu

    def test_keluar_mengakhiri_sesi_dan_kembali_ke_halaman_masuk(self):
        uid, klien = self.karyawan("keluar1", "produksi")
        d = self.db()
        self.assertEqual(d.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0], 1)

        s, body = klien.post("/logout")
        self.assertEqual(s, 200)  # mengikuti pengalihan ke /login
        self.assertIn("Masuk", body)
        self.assertNotIn("Catat produksi", body)

        d = self.db()
        self.assertEqual(d.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0], 0)
        # token lama tidak bisa dipakai lagi
        s, body = klien.get("/")
        self.assertIn("Masuk", body)
        self.assertNotIn("Hari ini", body)

    def test_setelah_keluar_halaman_data_tidak_terbuka(self):
        _, klien = self.karyawan("keluar2", "produksi")
        klien.post("/logout")
        for path in ("/", "/produksi", "/gaji", "/laporan"):
            s, body = klien.get(path)
            self.assertEqual(s, 200, path)
            self.assertIn("Masuk", body, path)
            self.assertNotIn("Catat produksi", body, path)

    def test_halaman_data_tidak_disimpan_browser(self):
        """Tombol back tidak boleh menampilkan data lama: setiap halaman data wajib no-store."""
        for path in ("/", "/produksi", "/laporan"):
            _, h, _ = self.halaman(self.admin, path)
            self.assertEqual(h.get("Cache-Control"), "no-store", path)

if __name__ == "__main__":
    unittest.main()
