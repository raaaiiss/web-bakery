"""Test pengaturan per user 'Menerima gaji' (Ya/Tidak): migrasi, efek ke halaman Gaji/Beranda/menu,
peringatan saat dinonaktifkan dengan gaji belum dibayar, Riwayat, dan 403 untuk non-admin.
Jalankan semua test:  python3 -m unittest -v
"""
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
import urllib.request

import app
from test_app import Klien
from test_fase2 import Dasar, hari


class TestMigrasiGaji(unittest.TestCase):
    def test_status_awal_mengikuti_perilaku_lama(self):
        d = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "lama.db"))
        d.row_factory = sqlite3.Row
        d.executescript(app.SCHEMA)
        for u, role in (("bos", "admin"), ("kakak", "karyawan"), ("kasir", "karyawan")):
            d.execute("INSERT INTO users(username, name, pw, role) VALUES(?,?,'x',?)", (u, u, role))
        d.commit()
        app.migrasi(d)
        d.execute("UPDATE users SET pct = 0 WHERE username = 'kasir'")
        d.commit()
        app.migrasi_tampilan(d)
        app.migrasi_gaji(d)
        app.migrasi_gaji(d)  # aman dijalankan berulang
        status = {r["username"]: r["digaji"] for r in d.execute("SELECT username, digaji FROM users")}
        self.assertEqual(status, {"bos": 0, "kakak": 1, "kasir": 0})
        self.assertEqual(d.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
        self.assertEqual(d.execute("PRAGMA user_version").fetchone()[0], 2)


class TestMenerimaGaji(Dasar):
    def buat(self, username, *akses, digaji=None):
        data = [("name", username.title()), ("username", username), ("password", "rahasia1"), ("pct", "25")]
        data += [("akses", a) for a in akses] + ([("digaji", digaji)] if digaji is not None else [])
        self.admin._buka(urllib.request.Request(self.base + "/pengguna", urllib.parse.urlencode(data).encode()))
        u = self.db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        k = Klien(self.base)
        k.post("/login", username=username, password="rahasia1")
        return u, k

    def simpan(self, uid, *akses, digaji=None, pct="25", konfirmasi=False, klien=None):
        data = [("pct", pct)] + [("akses", a) for a in akses] + ([("digaji", digaji)] if digaji is not None else [])
        if konfirmasi:
            data.append(("konfirmasi_nonaktif", "1"))
        return (klien or self.admin)._buka(urllib.request.Request(f"{self.base}/pengguna/{uid}/akses",
                                                                  urllib.parse.urlencode(data).encode()))

    def user(self, uid):
        return self.db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def test_akun_baru_default_tidak_dan_tercatat(self):
        u, _ = self.buat("baru1", "produksi")
        self.assertEqual(u["digaji"], 0)
        u2, _ = self.buat("baru2", "produksi", digaji="1")
        self.assertEqual(u2["digaji"], 1)
        aksi = [r[0] for r in self.db().execute("SELECT detail FROM audit WHERE action = 'ubah status gaji'")]
        self.assertTrue(any("menerima gaji Tidak" in a for a in aksi))
        self.assertTrue(any("menerima gaji Ya" in a for a in aksi))
        html = self.admin.get("/pengguna")[1]
        self.assertIn('role="switch"', html)
        self.assertIn("dihitung dan dibayar gajinya", html)

    def test_tidak_digaji_tidak_dihitung_dan_tidak_muncul(self):
        u, k = self.buat("rina", *app.AKSES_KAKAK)         # punya centang Lihat gaji sendiri, tapi tidak digaji
        self.produksi_langsung(app.today(), 1, 10, user_id=u["id"])
        # karyawan: tidak ada menu/angka gaji
        beranda = k.get("/")[1]
        self.assertNotIn("</span>Gaji</a>", beranda)
        self.assertNotIn("Gaji berjalan", beranda)
        s, html = k.get("/gaji")
        self.assertEqual(s, 200)
        self.assertIn("tidak terdaftar menerima gaji", html)
        self.assertNotIn("Rp", html.split("<main>")[1])
        # admin: tidak ada blok gaji/tombol bayar untuk rina, hanya di daftar "Tidak menerima gaji"
        admin = self.admin.get("/gaji")[1]
        dibayar, tidak = admin.split("Tidak menerima gaji")[0], admin.split("Tidak menerima gaji")[1]
        self.assertNotIn("Rina", dibayar)
        self.assertIn("Rina", tidak)
        self.assertNotIn(f"/gaji/{u['id']}/bayar", admin)
        self.assertEqual(self.admin.post(f"/gaji/{u['id']}/bayar", jumlah="0")[0], 400)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM wage_payments WHERE user_id = ?", (u["id"],)).fetchone()[0], 0)

    def test_digaji_tetap_seperti_sebelumnya(self):
        u, k = self.buat("kakak", *app.AKSES_KAKAK, digaji="1")
        self.db().execute("UPDATE users SET gaji_mulai = ? WHERE id = ?", (app.today(), u["id"])).connection.commit()
        self.produksi_langsung(app.today(), 2, 10, sisa=0, user_id=u["id"])   # Roti 10 x 8000 = 80.000 -> 25% = 20.000
        g = app.gaji_info(self.db(), self.user(u["id"]))
        self.assertEqual((g["omzet"], g["jumlah"]), (80_000, 80_000 * 25 // 100))
        self.assertIn("<small>Rp</small>20.000", k.get("/gaji")[1])
        self.assertIn("</span>Gaji</a>", k.get("/")[1])

    def test_ya_ke_tidak_dengan_gaji_belum_dibayar_minta_konfirmasi(self):
        u, _ = self.buat("dewi", "produksi", "gaji", digaji="1")
        d = self.db()
        d.execute("UPDATE users SET gaji_mulai = ? WHERE id = ?", (hari(-3), u["id"]))
        # pembayaran lama untuk periode yang sudah lewat (hari -3 s.d. -2)
        d.execute("""INSERT INTO wage_payments(user_id, period_start, period_end, omzet, pct, amount, paid_at, paid_by)
                     VALUES(?,?,?,100000,25,25000,?,1)""", (u["id"], hari(-3), hari(-2), hari(-2) + " 20:00:00"))
        d.commit()
        self.produksi_langsung(hari(-1), 1, 8, sisa=0, user_id=u["id"])      # penjualan setelah pembayaran: belum dibayar
        tertunda = app.gaji_info(self.db(), self.user(u["id"]))["jumlah"]
        self.assertGreater(tertunda, 0)
        # tanpa konfirmasi: ditolak dengan peringatan, status tidak berubah
        status, teks = self.simpan(u["id"], "produksi", "gaji")           # sakelar tidak dicentang = Tidak
        self.assertEqual(status, 409)
        self.assertIn("gaji belum dibayar", teks)
        self.assertIn(app.rupiah(tertunda), teks)
        self.assertIn("Tetap nonaktifkan gaji", teks)
        self.assertEqual(self.user(u["id"])["digaji"], 1)
        # dengan konfirmasi: dinonaktifkan, tercatat, riwayat pembayaran lama tetap terlihat admin
        self.simpan(u["id"], "produksi", "gaji", digaji="0", konfirmasi=True)
        self.assertEqual(self.user(u["id"])["digaji"], 0)
        detail = self.db().execute("SELECT detail FROM audit WHERE action = 'ubah status gaji' ORDER BY id DESC").fetchone()[0]
        self.assertIn("dewi: menerima gaji Ya -> Tidak", detail)
        self.assertIn(app.rupiah(tertunda), detail)
        admin = self.admin.get("/gaji")[1]
        self.assertIn("Riwayat pembayaran lama", admin.split("Tidak menerima gaji")[1])
        self.assertIn("Rp 25.000", admin.split("Tidak menerima gaji")[1])
        # diaktifkan lagi: mulai dari hari ini, masa tidak digaji tidak ikut dihitung
        self.simpan(u["id"], "produksi", "gaji", digaji="1")
        g = app.gaji_info(self.db(), self.user(u["id"]))
        omzet_hari_ini = app.laporan_rentang(self.db(), app.today(), app.today())["total"]["omzet"]
        self.assertEqual((g["mulai"], g["omzet"]), (app.today(), omzet_hari_ini))   # penjualan kemarin tidak ikut

    def test_ya_ke_tidak_tanpa_gaji_tertunda_langsung_tersimpan(self):
        u, _ = self.buat("tono", "produksi", digaji="1")
        # gaji dihitung dari semua penjualan toko; mulai besok berarti belum ada gaji tertunda
        self.db().execute("UPDATE users SET gaji_mulai = ? WHERE id = ?", (hari(1), u["id"])).connection.commit()
        r = self.simpan(u["id"], "produksi")
        self.assertEqual(r[0], 200)                                          # dialihkan ke Pengguna, tidak perlu konfirmasi
        self.assertEqual(self.user(u["id"])["digaji"], 0)

    def test_hanya_admin_boleh_mengubah(self):
        u, k = self.buat("lina", "produksi", "sisa", "omzet", "untung", "gaji", "modal", "kue")
        s, _ = self.simpan(u["id"], *app.PERM_KEYS, digaji="1", klien=k)
        self.assertEqual(s, 403)
        self.assertEqual(self.user(u["id"])["digaji"], 0)
        self.assertEqual(k.post("/pengguna", name="X", username="x", password="rahasia1", digaji="1")[0], 403)


if __name__ == "__main__":
    unittest.main()
