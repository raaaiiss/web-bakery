"""Test gaji berjalan 'sementara/pasti', tanggal mulai gaji, muat ulang otomatis, dan hapus user (nonaktifkan).
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


class TestMigrasiHapus(unittest.TestCase):
    def test_kolom_aktif_ditambah_semua_akun_lama_aktif(self):
        d = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "lama.db"))
        d.row_factory = sqlite3.Row
        d.executescript(app.SCHEMA)
        d.execute("INSERT INTO users(username, name, pw, role) VALUES('bos','Bos','x','admin')")
        d.execute("INSERT INTO users(username, name, pw, role) VALUES('kakak','Kakak','x','karyawan')")
        d.commit()
        for f in (app.migrasi, app.migrasi_tampilan, app.migrasi_gaji, app.migrasi_hapus, app.migrasi_hapus):
            f(d)
        self.assertEqual([(r["username"], r["aktif"], r["dihapus_pada"]) for r in d.execute("SELECT * FROM users ORDER BY id")],
                         [("bos", 1, None), ("kakak", 1, None)])


class Bantuan(Dasar):
    def buat(self, username, *akses, digaji="1"):
        data = [("name", username.title()), ("username", username), ("password", "rahasia1"), ("pct", "25"), ("digaji", digaji)]
        data += [("akses", a) for a in akses]
        self.admin._buka(urllib.request.Request(self.base + "/pengguna", urllib.parse.urlencode(data).encode()))
        u = self.db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        k = Klien(self.base)
        k.post("/login", username=username, password="rahasia1")
        return u, k

    def user(self, uid):
        return self.db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


class TestGajiSementara(Bantuan):
    def test_gaji_hari_ini_sementara_lalu_pasti(self):
        u, k = self.buat("kakak", *app.AKSES_KAKAK)
        k.post("/produksi", cake_id="1", qty="20")                       # Donat 20 x 5.000, sisa belum diisi
        omzet = app.laporan_harian(self.db(), app.today())["total"]["omzet"]
        self.assertGreater(omzet, 0)
        bagian = app.rupiah(omzet * 25 // 100)
        for klien in (self.admin, k):
            html = klien.get("/gaji")[1]
            self.assertIn("Gaji hari ini", html)
            self.assertIn('<span class="lencana sementara">sementara</span>', html)
            self.assertIn(bagian, html)
            self.assertIn("dianggap terjual", html)
        self.assertIn("Gaji hari ini", k.get("/")[1])
        self.assertIn("Gaji karyawan hari ini", self.admin.get("/")[1])
        # rumus tidak berubah: bagian hari ini = persen x omzet hari ini
        h = app.gaji_hari_ini(self.db(), self.user(u["id"]))
        self.assertEqual((h["omzet"], h["jumlah"], h["sementara"]), (omzet, omzet * 25 // 100, True))
        # setelah sisa diisi: pasti, dan angka ikut turun bila ada sisa
        d = self.db()
        d.execute("INSERT OR REPLACE INTO leftover VALUES(?,1,4,1,'x')", (app.today(),))
        d.commit()
        for (cid,) in d.execute("SELECT DISTINCT cake_id FROM production WHERE date = ? AND cake_id != 1", (app.today(),)).fetchall():
            d.execute("INSERT OR REPLACE INTO leftover VALUES(?,?,0,1,'x')", (app.today(), cid))
        d.commit()
        h2 = app.gaji_hari_ini(self.db(), self.user(u["id"]))
        self.assertFalse(h2["sementara"])
        self.assertEqual(h2["jumlah"], app.laporan_harian(self.db(), app.today())["total"]["omzet"] * 25 // 100)
        self.assertLess(h2["jumlah"], h["jumlah"])
        self.assertIn('<span class="lencana pasti">pasti</span>', self.admin.get("/gaji")[1])

    def test_muat_ulang_otomatis_di_gaji_dan_beranda(self):
        for path in ("/", "/gaji"):
            html = self.admin.get(path)[1]
            self.assertIn("segarkan", html.split("<body")[1].split(">")[0], path)
            self.assertIn("Diperbarui", html)
        self.assertNotIn("segarkan", self.admin.get("/laporan")[1].split("<body")[1].split(">")[0])

    def test_tanggal_mulai_gaji_diatur_admin(self):
        u, _ = self.buat("baru")
        self.produksi_langsung(hari(-5), 2, 10, sisa=0)                   # Roti 10 x 8.000 = 80.000, lima hari lalu
        sebelum = app.gaji_info(self.db(), self.user(u["id"]))
        self.assertEqual(sebelum["mulai"], app.today())                   # akun baru: dihitung mulai hari dibuat
        data = [("pct", "25"), ("digaji", "1"), ("gaji_mulai", hari(-5))]
        r = self.admin._buka(urllib.request.Request(f"{self.base}/pengguna/{u['id']}/akses", urllib.parse.urlencode(data).encode()))
        self.assertEqual(r[0], 200)
        sesudah = app.gaji_info(self.db(), self.user(u["id"]))
        self.assertEqual(sesudah["mulai"], hari(-5))
        self.assertGreaterEqual(sesudah["omzet"], sebelum["omzet"] + 80_000)
        self.assertIn("ubah mulai gaji", [r[0] for r in self.db().execute("SELECT action FROM audit")])
        besok = [("pct", "25"), ("digaji", "1"), ("gaji_mulai", hari(1))]
        r = self.admin._buka(urllib.request.Request(f"{self.base}/pengguna/{u['id']}/akses", urllib.parse.urlencode(besok).encode()))
        self.assertEqual(r[0], 400)


class TestHapusUser(Bantuan):
    def test_hanya_admin_dan_tidak_bisa_hapus_diri_sendiri(self):
        u, k = self.buat("tini", "produksi", "sisa", "omzet", "untung", "gaji", "modal", "kue")
        self.assertEqual(k.get(f"/pengguna/{u['id']}/hapus")[0], 403)
        self.assertEqual(k.post(f"/pengguna/{u['id']}/hapus", yakin="1")[0], 403)
        self.assertEqual(k.post("/pengguna/1/hapus", yakin="1")[0], 403)
        self.assertEqual(self.user(u["id"])["aktif"], 1)
        s, html = self.admin.post("/pengguna/1/hapus", yakin="1")
        self.assertEqual(s, 400)
        self.assertIn("tidak bisa menghapus akunmu sendiri", html)
        self.assertEqual(self.user(1)["aktif"], 1)

    def test_admin_lain_boleh_dihapus_selama_masih_ada_admin(self):
        d = self.db()
        d.execute("INSERT INTO users(username, name, pw, role, gaji_mulai) VALUES('wakil','Wakil',?,'admin',?)",
                  (app.hash_pw("rahasia1"), app.today()))
        d.commit()
        wid = self.db().execute("SELECT id FROM users WHERE username = 'wakil'").fetchone()[0]
        s, _ = self.admin.post(f"/pengguna/{wid}/hapus", yakin="1")
        self.assertEqual(s, 200)
        self.assertEqual(self.user(wid)["aktif"], 0)

    def test_hapus_dengan_konfirmasi_dan_akibatnya(self):
        u, k = self.buat("budi", "produksi", digaji="0")
        k.post("/produksi", cake_id="2", qty="5")
        s, html = self.admin.get(f"/pengguna/{u['id']}/hapus")
        self.assertEqual(s, 200)
        for teks in ("Budi", "Tidak bisa masuk lagi", "tetap", "tidak bisa dipakai untuk akun baru", "Ya, hapus Budi"):
            self.assertIn(teks, html)
        self.assertEqual(self.admin.post(f"/pengguna/{u['id']}/hapus")[0], 400)     # tanpa konfirmasi
        s, html = self.admin.post(f"/pengguna/{u['id']}/hapus", yakin="1")
        self.assertIn("Budi dihapus", html)
        self.assertEqual(self.user(u["id"])["aktif"], 0)
        # tidak bisa masuk; sesi lama dihentikan
        self.assertIn("Masuk", k.get("/produksi")[1])
        self.assertEqual(Klien(self.base).post("/login", username="budi", password="rahasia1")[0], 401)
        # lapis kedua: token sesi yang tersisa pun ditolak untuk akun yang dihapus
        d = self.db()
        d.execute("INSERT INTO sessions VALUES('token-sisa', ?, ?)", (u["id"], app.stamp()))
        d.commit()
        req = urllib.request.Request(self.base + "/produksi", headers={"Cookie": "sesi=token-sisa"})
        with urllib.request.urlopen(req) as r:
            self.assertTrue(r.url.endswith("/login"))
        # catatan lama tetap menyebut namanya; tampil di daftar dihapus; tercatat di Riwayat
        self.assertIn("Budi (dihapus)", self.admin.get("/produksi")[1])
        pengguna = self.admin.get("/pengguna")[1]
        self.assertIn("Budi", pengguna.split("Pengguna yang dihapus")[1])
        self.assertIn("hapus pengguna", [r[0] for r in self.db().execute("SELECT action FROM audit")])
        self.assertIn("Budi (dihapus)", self.admin.get("/riwayat")[1])
        # username tidak dipakai ulang diam-diam
        s, html = self.admin.post("/pengguna", name="Budi Baru", username="budi", password="rahasia1")
        self.assertEqual(s, 400)
        self.assertIn("akun yang sudah dihapus", html)
        # yang sudah dihapus tidak bisa dihapus lagi
        self.assertEqual(self.admin.get(f"/pengguna/{u['id']}/hapus")[0], 404)

    def test_peringatan_gaji_belum_dibayar_dan_hilang_dari_gaji(self):
        u, k = self.buat("sari", "produksi")                              # menerima gaji
        k.post("/produksi", cake_id="1", qty="10")
        tertunda = app.gaji_info(self.db(), self.user(u["id"]))["jumlah"]
        self.assertGreater(tertunda, 0)
        self.assertIn("Sari", self.admin.get("/gaji")[1])
        s, html = self.admin.get(f"/pengguna/{u['id']}/hapus")
        self.assertIn(app.rupiah(tertunda), html)
        self.assertIn("Tetap hapus walau gaji belum dibayar", html)
        s, html = self.admin.post(f"/pengguna/{u['id']}/hapus", yakin="1")        # tanpa konfirmasi gaji
        self.assertEqual(s, 409)
        self.assertEqual(self.user(u["id"])["aktif"], 1)
        self.admin.post(f"/pengguna/{u['id']}/hapus", yakin="1", konfirmasi_gaji="1")
        self.assertEqual((self.user(u["id"])["aktif"], self.user(u["id"])["digaji"]), (0, 0))
        detail = self.db().execute("SELECT detail FROM audit WHERE action = 'hapus pengguna' ORDER BY id DESC").fetchone()[0]
        self.assertIn(app.rupiah(tertunda), detail)
        self.assertNotIn("Sari", self.admin.get("/gaji")[1].split("<main>")[1])
        self.assertEqual(self.admin.post(f"/gaji/{u['id']}/bayar", jumlah="0")[0], 400)


if __name__ == "__main__":
    unittest.main()
