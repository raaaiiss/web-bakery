"""Test tampilan baru: kolom warna/foto kue (migrasi), halaman Kue (foto, warna), kartu kue,
Beranda baru, sisa per batch dan sisa per kue per hari.
Jalankan semua test:  python3 -m unittest -v
"""
import base64
import http.client
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request

import app
from test_app import Klien
from test_fase2 import Dasar, hari

JPEG = b"\xff\xd8\xff\xe0" + b"\x00\x10JFIF" + b"\x00" * 200 + b"\xff\xd9"   # cukup untuk cek tanda JPEG
DATA_URL = "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


class TestMigrasiTampilan(unittest.TestCase):
    def test_kolom_warna_foto_ditambah_tanpa_ubah_data(self):
        d = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "v2.db"))
        d.row_factory = sqlite3.Row
        d.executescript(app.SCHEMA)
        d.execute("INSERT INTO users(username, name, pw, role) VALUES('bos','Pemilik','x','admin')")
        d.execute("INSERT INTO cakes(name, price_sell, price_cost) VALUES('Donat Gula', 5000, 3000)")
        d.execute("INSERT INTO production(date, cake_id, qty, price_sell, price_cost, user_id, created) "
                  "VALUES('2026-11-01', 1, 30, 5000, 3000, 1, '2026-11-01 07:00:00')")
        d.commit()
        app.migrasi(d)
        app.migrasi_tampilan(d)
        app.migrasi_tampilan(d)  # aman dijalankan berulang
        kue = d.execute("SELECT * FROM cakes").fetchone()
        self.assertEqual((kue["name"], kue["price_sell"], kue["color"], kue["photo"]), ("Donat Gula", 5000, None, None))
        self.assertEqual(d.execute("SELECT COUNT(*) FROM production").fetchone()[0], 1)
        self.assertEqual(d.execute("PRAGMA user_version").fetchone()[0], 2)
        # tanpa foto & warna: kartu tetap berisi warna cadangan dan inisial
        html = app.gambar_kue(kue)
        self.assertIn(">DG<", html)
        self.assertIn("background:#", html)


class TestKue(Dasar):
    def ambil(self, cid):
        return self.db().execute("SELECT * FROM cakes WHERE id = ?", (cid,)).fetchone()

    def test_warna_dan_foto_kue(self):
        # warna
        self.admin.post("/kue/1/ubah", name="Donat", price_sell="5000", price_cost="3000", active="1", color="stroberi")
        self.assertEqual(self.ambil(1)["color"], "stroberi")
        self.assertEqual(self.admin.post("/kue/1/ubah", name="Donat", price_sell="5000", price_cost="3000",
                                         active="1", color="neon")[0], 400)
        # foto dari browser (JPEG base64) disimpan sebagai file, lalu bisa diambil
        s, body = self.admin.post("/kue/1/ubah", name="Donat", price_sell="5000", price_cost="3000", active="1",
                                  foto_data=DATA_URL)
        self.assertIn("disimpan", body)
        foto1 = self.ambil(1)["photo"]
        self.assertRegex(foto1, r"^kue-1-[0-9a-f]{12}\.jpg$")
        with urllib.request.urlopen(f"{self.base}/foto/{foto1}") as r:
            self.assertEqual((r.status, r.headers["Content-Type"], r.read()), (200, "image/jpeg", JPEG))
        # foto baru menggantikan file lama
        self.admin.post("/kue/1/ubah", name="Donat", price_sell="5000", price_cost="3000", active="1", foto_data=DATA_URL)
        foto2 = self.ambil(1)["photo"]
        self.assertNotEqual(foto1, foto2)
        self.assertFalse(os.path.exists(os.path.join(app.foto_dir(), foto1)))
        # kartu Produksi memakai foto; kue tanpa foto memakai inisial
        html = self.admin.get("/produksi")[1]
        self.assertIn(f'src="/foto/{foto2}"', html)
        self.assertIn(">R<", html)  # "Roti" tanpa foto
        self.assertNotIn(" checked>", html.split('class="kartu-kue"')[1].split("</fieldset>")[0])  # tidak ada kue terpilih otomatis
        # hapus foto
        self.admin.post("/kue/1/ubah", name="Donat", price_sell="5000", price_cost="3000", active="1", hapus_foto="1")
        self.assertIsNone(self.ambil(1)["photo"])
        self.assertFalse(os.path.exists(os.path.join(app.foto_dir(), foto2)))
        self.assertIn("ubah foto kue", [r[0] for r in self.db().execute("SELECT action FROM audit")])

    def test_foto_ditolak_kalau_bukan_jpeg_atau_tanpa_akses(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
        palsu = "data:image/jpeg;base64," + base64.b64encode(b"<script>").decode()
        for data in (png, palsu, "data:image/jpeg;base64,@@@"):
            s, _ = self.admin.post("/kue/2/ubah", name="Roti", price_sell="8000", price_cost="5000", active="1", foto_data=data)
            self.assertEqual(s, 400, data[:30])
        self.assertIsNone(self.ambil(2)["photo"])
        _, k = self.karyawan("tanpakue", "produksi")
        self.assertEqual(k.post("/kue/2/ubah", name="Roti", price_sell="8000", foto_data=DATA_URL)[0], 403)
        # file di luar folder foto tidak bisa diambil
        for jalan in ("/foto/..%2Ffase2.db", "/foto/kue-1-zzzz.jpg", "/foto/fase2.db"):
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(self.base + jalan)
            self.assertEqual(err.exception.code, 404, jalan)

    def test_kiriman_terlalu_besar_ditolak(self):
        # server menolak dari ukuran yang diumumkan, sebelum membaca isinya
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.putrequest("POST", "/kue/2/ubah")
        conn.putheader("Content-Length", str(app.BODY_MAKS + 1))
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()


class TestBerandaDanSisa(Dasar):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        d = sqlite3.connect(app.DB_PATH)
        # hari ini: Donat 2 batch (30 + 20), Roti 1 batch (10); kemarin: Donat 40 sisa 0, Roti 10 sisa 3
        rows = [(app.today(), 1, 30, "06:10"), (app.today(), 1, 20, "10:30"), (app.today(), 2, 10, "06:20"),
                (hari(-1), 1, 40, "06:10"), (hari(-1), 2, 10, "06:20")]
        for tgl, cid, qty, jam in rows:
            hj, hm = (5000, 3000) if cid == 1 else (8000, 5000)
            d.execute("INSERT INTO production(date, cake_id, qty, price_sell, price_cost, user_id, created) VALUES(?,?,?,?,?,1,?)",
                      (tgl, cid, qty, hj, hm, f"{tgl} {jam}:00"))
        d.executemany("INSERT INTO leftover VALUES(?,?,?,1,?)",
                      [(hari(-1), 1, 0, "x"), (hari(-1), 2, 3, "x"), (app.today(), 1, 5, "x")])
        d.commit()

    def test_beranda_header_menu_dan_terlaris(self):
        html = self.admin.get("/")[1]
        self.assertIn("Penjualan hari ini", html)
        self.assertIn("<b>3</b><span>batch</span>", html)           # 3 batch hari ini
        self.assertIn("Kue terlaris hari ini", html)
        laris = html.split("Kue terlaris hari ini")[1]
        self.assertLess(laris.index("Donat"), laris.index("Roti"))  # Donat 45 terjual > Roti 10
        for menu in ("Catat produksi", "Catat sisa", "Sisa per hari", "Daftar kue", "Laporan", "Gaji", "Pengguna", "Riwayat"):
            self.assertIn(f"</span>{menu}</a>", html, menu)
        self.assertIn("Laporan harian", html)

    def test_menu_beranda_ikut_hak_akses(self):
        _, k = self.karyawan("rina", *app.AKSES_KAKAK)
        html = k.get("/")[1]
        for ada in ("Catat produksi", "Laporan", "Gaji", "Sisa per hari"):
            self.assertIn(f"</span>{ada}</a>", html, ada)
        for tidak in ("Catat sisa", "Daftar kue", "Pengguna", "Riwayat"):
            self.assertNotIn(f"</span>{tidak}</a>", html, tidak)

    def test_sisa_per_batch_dan_per_hari(self):
        d = self.db()
        data = app.sisa_per_hari(d, hari(-1), app.today())
        sel = {b["kue"]["name"]: b["sel"] for b in data["baris"]}
        self.assertEqual(sel["Donat"], {hari(-1): 0, app.today(): 5})
        self.assertEqual(sel["Roti"], {hari(-1): 3, app.today(): "?"})       # hari ini sisa Roti belum diisi
        ringkas = {b["kue"]["name"]: (b["hari_sisa"], b["hari_habis"]) for b in data["baris"]}
        self.assertEqual(ringkas, {"Donat": (1, 1), "Roti": (1, 0)})
        self.assertEqual([b["qty"] for b in data["batch"][app.today()][1]], [30, 20])
        # rumus tidak berubah: rugi di halaman = laporan_harian
        rugi_roti_kemarin = [i for i in app.laporan_harian(d, hari(-1))["items"] if i["nama"] == "Roti"][0]["kerugian"]
        self.assertEqual(rugi_roti_kemarin, 3 * 5000)
        s, html = self.admin.get("/sisa-harian")
        self.assertEqual(s, 200)
        self.assertIn("30 pcs (jam 06:10), 20 pcs (jam 10:30)", html)
        self.assertIn("rugi Rp 15.000", html)
        self.assertIn('<td class="belum">?</td>', html)
        # halaman Sisa menampilkan batch hari itu
        self.assertIn("Batch: 30 pcs (06:10), 20 pcs (10:30)", self.admin.get("/sisa")[1])

    def test_akses_halaman_sisa_per_hari(self):
        _, hanya_produksi = self.karyawan("p1", "produksi")
        self.assertEqual(hanya_produksi.get("/sisa-harian")[0], 403)
        _, penjaga = self.karyawan("p2", "sisa")
        s, html = penjaga.get("/sisa-harian")
        self.assertEqual(s, 200)
        self.assertNotIn("rugi Rp", html)                                    # tanpa akses untung: angka rugi tidak tampil
        self.assertEqual(self.admin.get(f"/sisa-harian?periode=rentang&dari={hari(-90)}&sampai={app.today()}")[0], 400)


if __name__ == "__main__":
    unittest.main()
