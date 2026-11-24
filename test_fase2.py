"""Test Fase 2: migrasi database, hak akses per centang (termasuk 403), gaji + tandai dibayar, filter laporan.
Jalankan semua test:  python3 -m unittest -v
"""
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta

import app
from test_app import Klien


def hari(n):
    """Tanggal n hari dari hari ini (negatif = lalu)."""
    return (date.fromisoformat(app.today()) + timedelta(days=n)).isoformat()


class Dasar(unittest.TestCase):
    """Server sungguhan dengan database sementara sendiri untuk tiap kelas test."""

    @classmethod
    def setUpClass(cls):
        cls._db_lama = app.DB_PATH
        app.DB_PATH = os.path.join(tempfile.mkdtemp(), "fase2.db")
        app.init_db()
        cls.srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.admin = Klien(cls.base)
        cls.admin.post("/setup", name="Pemilik", username="bos", password="rahasia1")
        cls.admin.post("/login", username="bos", password="rahasia1")
        cls.admin.post("/kue", name="Donat", price_sell="5000", price_cost="3000")
        cls.admin.post("/kue", name="Roti", price_sell="8000", price_cost="5000")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        app.DB_PATH = cls._db_lama

    def db(self):
        d = sqlite3.connect(app.DB_PATH)
        d.row_factory = sqlite3.Row
        return d

    def karyawan(self, username, *akses, pct=None):
        data = {"name": username.title(), "username": username, "password": "rahasia1"}
        if pct is not None:
            data["pct"] = str(pct)
        klien_admin = self.admin
        if akses:
            # urllib tidak mengirim list; kirim akses berulang lewat string query manual
            import urllib.parse
            body = urllib.parse.urlencode(list(data.items()) + [("akses", a) for a in akses]).encode()
            import urllib.request
            req = urllib.request.Request(self.base + "/pengguna", body)
            klien_admin._buka(req)
        else:
            klien_admin.post("/pengguna", **data)
        uid = self.db().execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        k = Klien(self.base)
        k.post("/login", username=username, password="rahasia1")
        return uid, k

    def produksi_langsung(self, tgl, cake_id, qty, sisa=None, user_id=1):
        """Masukkan catatan tanggal lampau langsung ke database (harga diambil dari kue saat ini)."""
        d = self.db()
        c = d.execute("SELECT * FROM cakes WHERE id = ?", (cake_id,)).fetchone()
        d.execute("INSERT INTO production(date, cake_id, qty, price_sell, price_cost, user_id, created) VALUES(?,?,?,?,?,?,?)",
                  (tgl, cake_id, qty, c["price_sell"], c["price_cost"], user_id, tgl + " 07:00:00"))
        if sisa is not None:
            d.execute("INSERT INTO leftover VALUES(?,?,?,?,?)", (tgl, cake_id, sisa, user_id, tgl + " 20:00:00"))
        d.commit()


class TestMigrasi(unittest.TestCase):
    def test_database_versi_1_naik_ke_versi_2_tanpa_kehilangan_data(self):
        path = os.path.join(tempfile.mkdtemp(), "v1.db")
        d = sqlite3.connect(path)
        d.row_factory = sqlite3.Row
        d.executescript(app.SCHEMA)  # persis struktur versi 1
        d.execute("INSERT INTO users(username, name, pw, role) VALUES('bos','Pemilik','x','admin')")
        d.execute("INSERT INTO users(username, name, pw, role) VALUES('kakak','Kakak','x','karyawan')")
        d.execute("INSERT INTO cakes(name, price_sell, price_cost) VALUES('Donat', 5000, 3000)")
        d.execute("INSERT INTO production(date, cake_id, qty, price_sell, price_cost, user_id, created) "
                  "VALUES('2026-11-01', 1, 30, 5000, 3000, 2, '2026-11-01 07:00:00')")
        d.execute("INSERT INTO leftover VALUES('2026-11-01', 1, 5, 1, '2026-11-01 20:00:00')")
        d.execute("INSERT INTO audit(at, user_id, action, detail) VALUES('2026-11-01', 1, 'x', 'y')")
        d.commit()
        sebelum = {t: d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                   for t in ("users", "cakes", "production", "leftover", "audit")}

        app.migrasi(d)
        app.migrasi(d)  # aman dijalankan dua kali

        sesudah = {t: d.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sebelum}
        self.assertEqual(sebelum, sesudah)
        self.assertEqual(d.execute("PRAGMA user_version").fetchone()[0], 2)
        kakak = d.execute("SELECT * FROM users WHERE username = 'kakak'").fetchone()
        self.assertEqual((kakak["pct"], kakak["gaji_mulai"]), (25, "2026-11-01"))
        # karyawan lama tetap bisa input produksi seperti di versi 1
        self.assertEqual([r[0] for r in d.execute("SELECT perm FROM user_perms WHERE user_id = 2")], ["produksi"])
        self.assertEqual(app.laporan_harian(d, "2026-11-01")["total"]["omzet"], 25 * 5000)


class TestAkses(Dasar):
    def test_user_baru_tanpa_akses_ditolak_server(self):
        _, k = self.karyawan("baru")
        jumlah_awal = self.db().execute("SELECT COUNT(*) FROM production").fetchone()[0]
        self.assertIn("belum punya akses", k.get("/")[1])
        for path in ("/produksi", "/sisa", "/laporan", "/gaji", "/kue", "/pengguna", "/riwayat"):
            self.assertEqual(k.get(path)[0], 403, path)
        self.assertEqual(k.post("/produksi", cake_id="1", qty="5")[0], 403)
        self.assertEqual(k.post("/sisa", tanggal=app.today(), sisa_1="0")[0], 403)
        self.assertEqual(k.post("/kue", name="Bolu", price_sell="1", price_cost="1")[0], 403)
        self.assertEqual(k.post("/gaji/1/bayar", jumlah="0")[0], 403)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM production").fetchone()[0], jumlah_awal)

    def test_centang_akses_kakak_lalu_server_ikut(self):
        uid, k = self.karyawan("kakak2", *app.AKSES_KAKAK)
        self.assertEqual({r[0] for r in self.db().execute("SELECT perm FROM user_perms WHERE user_id = ?", (uid,))},
                         set(app.AKSES_KAKAK))
        for path in ("/produksi", "/laporan", "/gaji"):
            self.assertEqual(k.get(path)[0], 200, path)
        for path in ("/sisa", "/kue", "/pengguna", "/riwayat"):
            self.assertEqual(k.get(path)[0], 403, path)
        # karyawan tidak bisa mengubah akses siapa pun, termasuk dirinya sendiri
        self.assertEqual(k.post(f"/pengguna/{uid}/akses", akses="sisa")[0], 403)
        # admin cabut "lihat untung": laporan tetap terbuka (masih punya omzet) tapi angka untung hilang
        import urllib.parse, urllib.request
        body = urllib.parse.urlencode([("akses", "produksi"), ("akses", "omzet"), ("pct", "25")]).encode()
        self.admin._buka(urllib.request.Request(f"{self.base}/pengguna/{uid}/akses", body))
        s, html = k.get("/laporan")
        self.assertEqual(s, 200)
        self.assertNotIn("Untung*", html)
        self.assertEqual(k.get("/gaji")[0], 403)
        self.assertIn("ubah akses", [r[0] for r in self.db().execute("SELECT action FROM audit")])

    def test_kelola_user_khusus_admin(self):
        uid, _ = self.karyawan("coba")
        s, body = self.admin.post(f"/pengguna/{uid}/akses", akses="kelola_user")
        self.assertEqual(s, 400)
        s, _ = self.admin.post("/pengguna/1/akses", akses="produksi")  # akses admin tidak bisa diubah
        self.assertEqual(s, 400)

    def test_input_sisa_karyawan_hanya_hari_ini(self):
        _, k = self.karyawan("penjaga", "sisa")
        self.produksi_langsung(hari(-1), 1, 10)
        self.assertEqual(k.post("/sisa", tanggal=hari(-1), sisa_1="2")[0], 403)

    def test_harga_modal_tersembunyi_tanpa_centang(self):
        _, k = self.karyawan("rina", "kue")
        s, body = k.get("/kue")
        self.assertEqual(s, 200)
        self.assertNotIn("modal Rp", body)
        self.assertNotIn('name="price_cost"', body)
        # ubah harga jual tanpa akses modal: harga modal lama tetap
        k.post("/kue/2/ubah", name="Roti", price_sell="8500", active="1")
        roti = self.db().execute("SELECT * FROM cakes WHERE id = 2").fetchone()
        self.assertEqual((roti["price_sell"], roti["price_cost"]), (8500, 5000))
        self.db().execute("UPDATE cakes SET price_sell = 8000 WHERE id = 2").connection.commit()

    def test_lupa_password_admin_buatkan_baru(self):
        uid, k = self.karyawan("lupa", "produksi")
        self.admin.post(f"/pengguna/{uid}/password", password="baru5678")
        self.assertIn("Masuk", k.get("/produksi")[1])  # sesi lama putus
        k2 = Klien(self.base)
        self.assertEqual(k2.post("/login", username="lupa", password="rahasia1")[0], 401)
        s, body = k2.post("/login", username="lupa", password="baru5678")
        self.assertEqual(s, 200)
        self.assertIn("Input produksi", body)


class TestGaji(Dasar):
    def test_gaji_naik_lalu_tandai_dibayar_mulai_nol(self):
        uid, kakak = self.karyawan("kakak", *app.AKSES_KAKAK)
        d = self.db()
        d.execute("UPDATE users SET gaji_mulai = ? WHERE id = ?", (hari(-1), uid))
        d.commit()
        # kemarin: Donat 20, sisa 4 -> 16 terjual x 5000 = 80.000
        self.produksi_langsung(hari(-1), 1, 20, sisa=4, user_id=uid)
        # hari ini lewat aplikasi: Donat 10, sisa belum diisi -> dianggap terjual semua = 50.000
        kakak.post("/produksi", cake_id="1", qty="10")
        u = d.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        g = app.gaji_info(d, u)
        self.assertEqual((g["omzet"], g["jumlah"], g["mulai"]), (130_000, 32_500, hari(-1)))
        for klien in (kakak, self.admin):
            body = klien.get("/gaji")[1]
            self.assertIn("<small>Rp</small>32.500", body)  # angka besar gaji berjalan
            self.assertIn("omzet Rp 130.000", body)

        # ganti harga jual: gaji tetap pakai harga yang tersimpan di catatan
        self.admin.post("/kue/1/ubah", name="Donat", price_sell="9000", price_cost="3000", active="1")
        self.assertEqual(app.gaji_info(d, u)["jumlah"], 32_500)

        # gaji tidak mengurangi untung (menunggu keputusan pemilik)
        untung_sebelum = app.laporan_rentang(d, hari(-1), app.today())["total"]["untung"]

        # angka di layar sudah basi -> ditolak, tidak tercatat
        self.assertEqual(self.admin.post(f"/gaji/{uid}/bayar", jumlah="30000")[0], 409)
        s, body = self.admin.post(f"/gaji/{uid}/bayar", jumlah="32500")
        self.assertIn("ditandai sudah dibayar", body)
        bayar = d.execute("SELECT * FROM wage_payments WHERE user_id = ?", (uid,)).fetchall()
        self.assertEqual(len(bayar), 1)
        self.assertEqual((bayar[0]["period_start"], bayar[0]["period_end"], bayar[0]["amount"], bayar[0]["pct"]),
                         (hari(-1), app.today(), 32_500, 25))
        self.assertEqual(app.laporan_rentang(d, hari(-1), app.today())["total"]["untung"], untung_sebelum)

        # mulai lagi dari nol; tidak bisa dibayar dua kali untuk hari yang sama
        g = app.gaji_info(d, u)
        self.assertTrue(g["sudah_lunas"])
        self.assertEqual(g["jumlah"], 0)
        self.assertEqual(self.admin.post(f"/gaji/{uid}/bayar", jumlah="0")[0], 400)
        self.assertIn("Riwayat pembayaran", kakak.get("/gaji")[1])

        # besok: hanya penjualan besok yang dihitung, pakai persen terbaru
        self.produksi_langsung(hari(1), 2, 10, sisa=0, user_id=uid)  # Roti 10 x 8000 = 80.000
        d.execute("UPDATE users SET pct = 30 WHERE id = ?", (uid,))
        d.commit()
        u = d.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        g = app.gaji_info(d, u, sampai=hari(1))
        self.assertEqual((g["mulai"], g["omzet"], g["jumlah"]), (hari(1), 80_000, 24_000))

        # karyawan tidak bisa menandai gajinya sendiri sudah dibayar
        self.assertEqual(kakak.post(f"/gaji/{uid}/bayar", jumlah="0")[0], 403)
        self.assertIn("gaji dibayar", [r[0] for r in d.execute("SELECT action FROM audit")])


class TestFilterLaporan(Dasar):
    def test_hari_ini_7_hari_bulan_ini_dan_rentang(self):
        d = self.db()
        catatan = [  # (hari ke-, kue, dibuat, sisa)
            (0, 1, 10, 2),    # Donat  8 terjual x 5000 = 40.000
            (-3, 1, 10, 1),   # Donat  9 x 5000 = 45.000
            (-3, 2, 10, 5),   # Roti   5 x 8000 = 40.000 (sisa banyak, tapi cuma 1 hari)
            (-10, 1, 20, 0),  # Donat 20 x 5000 = 100.000
        ]
        for n, kue, dibuat, sisa in catatan:
            self.produksi_langsung(hari(n), kue, dibuat, sisa)

        def omzet(dari, sampai):
            return sum((dibuat - sisa) * (5000 if kue == 1 else 8000)
                       for n, kue, dibuat, sisa in catatan if dari <= hari(n) <= sampai)

        # rentang = gabungan laporan harian, rumus sama
        satu = app.laporan_rentang(d, app.today(), app.today())
        self.assertEqual(satu["total"], app.laporan_harian(d, app.today())["total"])

        tujuh = app.laporan_rentang(d, hari(-6), app.today())
        self.assertEqual(tujuh["total"]["omzet"], 125_000)
        self.assertEqual(tujuh["total"]["sisa"], 8)
        self.assertEqual(tujuh["laku"]["nama"], "Donat")
        # Donat sisa di 2 hari, Roti sisa lebih banyak tapi 1 hari -> paling SERING sisa = Donat
        self.assertEqual((tujuh["sering_sisa"]["nama"], tujuh["sering_sisa"]["hari_sisa"]), ("Donat", 2))

        cases = {
            "/laporan": (app.today(), app.today()),
            "/laporan?periode=7": (hari(-6), app.today()),
            "/laporan?periode=bulan": (app.today()[:8] + "01", app.today()),
            f"/laporan?periode=rentang&dari={hari(-10)}&sampai={hari(-10)}": (hari(-10), hari(-10)),
            f"/laporan?periode=rentang&dari={hari(-12)}&sampai={app.today()}": (hari(-12), app.today()),
        }
        for path, (dari, sampai) in cases.items():
            s, body = self.admin.get(path)
            self.assertEqual(s, 200, path)
            target = app.rupiah(omzet(dari, sampai))
            if omzet(dari, sampai):
                self.assertIn(target, body, path)
            for label in ("Untung*", "Rugi dari sisa", "Terjual", "Sisa", "Paling laku"):
                if omzet(dari, sampai):
                    self.assertIn(label, body, f"{path} {label}")
        s, body = self.admin.get(f"/laporan?periode=rentang&dari={hari(-12)}&sampai={app.today()}")
        self.assertIn("Paling sering sisa", body)
        self.assertEqual(self.admin.get(f"/laporan?periode=rentang&dari={app.today()}&sampai={hari(-5)}")[0], 400)


if __name__ == "__main__":
    unittest.main()
