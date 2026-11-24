"""Test end-to-end: menyalakan server sungguhan dengan database sementara, lalu memakainya lewat HTTP.
Jalankan:  python3 -m unittest -v
"""
import http.cookiejar
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

TMP = tempfile.mkdtemp()
os.environ["KUE_DB"] = os.path.join(TMP, "test.db")
import app  # noqa: E402  (harus setelah KUE_DB diset)


class Klien:
    def __init__(self, base):
        self.base = base
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, path):
        return self._buka(urllib.request.Request(self.base + path))

    def post(self, path, **data):
        return self._buka(urllib.request.Request(self.base + path, urllib.parse.urlencode(data).encode()))

    def _buka(self, req):
        try:
            with self.op.open(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()


class TestTokoKue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        cls.srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_alur_harian_lengkap(self):
        admin = Klien(self.base)
        s, body = admin.get("/")
        self.assertIn("Buat akun admin pertama", body)
        admin.post("/setup", name="Pemilik", username="bos", password="rahasia1")
        s, body = admin.post("/login", username="bos", password="rahasia1")
        self.assertIn("Laporan harian", body)
        # setup tertutup setelah ada admin
        self.assertIn("Masuk", Klien(self.base).get("/setup")[1])

        admin.post("/kue", name="Donat", price_sell="5.000", price_cost="3000")
        admin.post("/kue", name="Roti Coklat", price_sell="8000", price_cost="5000")
        admin.post("/pengguna", name="Kakak", username="kakak", password="kakak123", akses="produksi")  # fase 2: user baru tanpa akses

        kakak = Klien(self.base)
        s, body = kakak.post("/login", username="kakak", password="kakak123")
        self.assertIn("Input produksi", body)
        kakak.post("/produksi", cake_id="1", qty="30")   # batch pagi
        kakak.post("/produksi", cake_id="1", qty="20")   # batch siang, dijumlahkan
        kakak.post("/produksi", cake_id="2", qty="10")
        # karyawan tidak boleh ke halaman admin
        self.assertEqual(kakak.get("/sisa")[0], 403)
        self.assertEqual(kakak.get("/laporan")[0], 403)
        # koreksi di hari yang sama boleh
        s, body = kakak.post("/produksi/3/ubah", qty="12")
        self.assertIn("Batch diubah", body)

        # sisa tidak boleh melebihi dibuat
        s, body = admin.post("/sisa", tanggal=app.today(), sisa_1="60", sisa_2="0")
        self.assertEqual(s, 400)
        s, body = admin.post("/sisa", tanggal=app.today(), sisa_1="5", sisa_2="2")
        self.assertIn("Sisa tersimpan", body)

        db = sqlite3.connect(os.environ["KUE_DB"]); db.row_factory = sqlite3.Row
        lp = app.laporan_harian(db, app.today())
        t = lp["total"]
        # Donat: 50 dibuat, 5 sisa, 45 terjual x 5000 = 225.000; Roti: 12-2 = 10 x 8000 = 80.000
        self.assertEqual((t["dibuat"], t["sisa"], t["terjual"]), (62, 7, 55))
        self.assertEqual(t["omzet"], 305_000)
        self.assertEqual(t["kerugian"], 5 * 3000 + 2 * 5000)          # 25.000
        self.assertEqual(t["untung"], 305_000 - (45 * 3000 + 10 * 5000))  # 120.000
        self.assertEqual(lp["laku"]["nama"], "Donat")
        self.assertIn("Rp 305.000", admin.get("/laporan")[1])

        # ganti harga: catatan hari ini tetap pakai harga lama
        admin.post("/kue/1/ubah", name="Donat", price_sell="6000", price_cost="3500", active="1")
        self.assertEqual(app.laporan_harian(db, app.today())["total"]["omzet"], 305_000)

        # batch kemarin: kakak tidak boleh ubah, admin boleh
        kemarin = (date.fromisoformat(app.today()) - timedelta(days=1)).isoformat()
        admin.post("/produksi", tanggal=kemarin, cake_id="1", qty="10")
        bid = db.execute("SELECT id FROM production WHERE date = ?", (kemarin,)).fetchone()[0]
        db.execute("UPDATE production SET user_id = 2 WHERE id = ?", (bid,)); db.commit()
        self.assertEqual(kakak.post(f"/produksi/{bid}/ubah", qty="9")[0], 403)
        self.assertIn("Batch diubah", admin.post(f"/produksi/{bid}/ubah", qty="9")[1])

        # admin buatkan password baru, sesi lama kakak putus
        admin.post("/pengguna/2/password", password="baru1234")
        self.assertIn("Masuk", kakak.get("/produksi")[1])
        self.assertEqual(Klien(self.base).post("/login", username="kakak", password="kakak123")[0], 401)

        # semua perubahan tercatat
        aksi = [r[0] for r in db.execute("SELECT action FROM audit")]
        for a in ("tambah produksi", "ubah produksi", "ubah sisa", "ubah kue", "ganti password"):
            self.assertIn(a, aksi)
        self.assertIn("Riwayat perubahan", admin.get("/riwayat")[1])


if __name__ == "__main__":
    unittest.main()
