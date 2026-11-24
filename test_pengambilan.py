"""Test pengambilan gaji (pilihan C): saldo, Ambil sebagian, Bayar semua, aturan keselamatan, diambil lebih, pembatalan.
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


class TestMigrasiPengambilan(unittest.TestCase):
    def test_tabel_baru_dan_pembayaran_lama_tetap(self):
        d = sqlite3.connect(os.path.join(tempfile.mkdtemp(), "lama.db"))
        d.row_factory = sqlite3.Row
        d.executescript(app.SCHEMA)
        d.execute("INSERT INTO users(username, name, pw, role) VALUES('kakak','Kakak','x','karyawan')")
        d.commit()
        app.migrasi(d)
        d.execute("""INSERT INTO wage_payments(user_id, period_start, period_end, omzet, pct, amount, paid_at, paid_by)
                     VALUES(1,'2026-11-01','2026-11-07',100000,25,25000,'2026-11-07 20:00:00',1)""")
        d.commit()
        for f in (app.migrasi_tampilan, app.migrasi_gaji, app.migrasi_hapus, app.migrasi_pengambilan, app.migrasi_pengambilan):
            f(d)
        bayar = d.execute("SELECT * FROM wage_payments").fetchone()
        self.assertEqual((bayar["amount"], bayar["diambil_sebelumnya"]), (25000, 0))
        self.assertEqual(d.execute("SELECT COUNT(*) FROM wage_withdrawals").fetchone()[0], 0)


class Bantuan(Dasar):
    def kakak(self, username="kakak", mulai=None):
        data = [("name", username.title()), ("username", username), ("password", "rahasia1"), ("pct", "25"), ("digaji", "1")]
        data += [("akses", a) for a in app.AKSES_KAKAK]
        self.admin._buka(urllib.request.Request(self.base + "/pengguna", urllib.parse.urlencode(data).encode()))
        d = self.db()
        uid = d.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        d.execute("UPDATE users SET gaji_mulai = ? WHERE id = ?", (mulai or hari(-3), uid))
        d.commit()
        k = Klien(self.base)
        k.post("/login", username=username, password="rahasia1")
        return uid, k

    def saldo(self, uid):
        return app.saldo_info(self.db(), self.db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())

    def ambil(self, uid, jumlah, klien=None, saldo=None, **lain):
        saldo = self.saldo(uid)["saldo"] if saldo is None else saldo
        return (klien or self.admin).post(f"/gaji/{uid}/ambil", jumlah=str(jumlah), saldo_tampil=str(saldo), **lain)


class TestContohAngka(Bantuan):
    def test_terkumpul_ambil_sebagian_lalu_bayar_semua(self):
        self.admin.post("/kue", name="Cake Potong", price_sell="2500", price_cost="1500")
        uid, k = self.kakak()
        self.produksi_langsung(hari(-2), 1, 300, sisa=0)     # Donat 300 x 5.000 = 1.500.000
        self.produksi_langsung(hari(-2), 3, 1, sisa=0)       # Cake Potong 1 x 2.500
        s = self.saldo(uid)
        self.assertEqual((s["omzet"], s["terkumpul"], s["diambil"], s["saldo"]), (1_502_500, 375_625, 0, 375_625))
        # ambil sebagian Rp 100.000
        status, html = self.ambil(uid, 100_000, tanggal=app.today(), catatan="belanja")
        self.assertIn("Saldo sekarang Rp 275.625", html)
        s = self.saldo(uid)
        self.assertEqual((s["terkumpul"], s["diambil"], s["saldo"]), (375_625, 100_000, 275_625))
        w = self.db().execute("SELECT * FROM wage_withdrawals").fetchone()
        self.assertEqual((w["amount"], w["saldo_setelah"], w["catatan"], w["created_by"], w["payment_id"]),
                         (100_000, 275_625, "belanja", 1, None))
        html = self.admin.get("/gaji")[1]
        for teks in ("Gaji terkumpul periode ini", "Rp 375.625", "Sudah diambil", "Rp 100.000", "<small>Rp</small>275.625",
                     "Riwayat pengambilan", "saldo setelahnya Rp 275.625", "dicatat Pemilik", "Bayar semua · Rp 275.625",
                     "Ambil sebagian"):
            self.assertIn(teks, html)
        # karyawan melihat saldo dan riwayatnya sendiri, tanpa tombol
        sendiri = k.get("/gaji")[1]
        self.assertIn("<small>Rp</small>275.625", sendiri)
        self.assertIn("Riwayat pengambilan", sendiri)
        self.assertNotIn("Bayar semua", sendiri)
        self.assertNotIn("Catat pengambilan", sendiri)
        self.assertIn("Saldo gaji", k.get("/")[1])
        # hanya admin
        self.assertEqual(self.ambil(uid, 1_000, klien=k)[0], 403)
        self.assertEqual(k.post(f"/gaji/{uid}/bayar", jumlah="275625")[0], 403)
        self.assertEqual(k.post(f"/gaji-ambil/{w['id']}/batal", alasan="x")[0], 403)
        # Bayar semua: bayar saldo, periode ditutup, pengambilan ikut tercatat di pembayaran
        status, html = self.admin.post(f"/gaji/{uid}/bayar", jumlah="275625")
        self.assertIn("Rp 275.625 ditandai sudah dibayar", html)
        p = self.db().execute("SELECT * FROM wage_payments WHERE user_id = ?", (uid,)).fetchone()
        self.assertEqual((p["amount"], p["diambil_sebelumnya"], p["period_end"]), (375_625, 100_000, app.today()))
        self.assertEqual(self.db().execute("SELECT payment_id FROM wage_withdrawals").fetchone()[0], p["id"])
        s = self.saldo(uid)
        self.assertEqual((s["sudah_lunas"], s["diambil"], s["saldo"]), (True, 0, 0))
        aksi = [r[0] for r in self.db().execute("SELECT action FROM audit")]
        self.assertIn("ambil gaji sebagian", aksi)
        self.assertIn("gaji dibayar", aksi)
        self.assertIn("dibayar saat ditutup Rp 275.625", self.admin.get("/gaji")[1])


class TestAturanAmbil(Bantuan):
    def test_jumlah_saldo_berubah_dan_sementara(self):
        uid, _ = self.kakak()
        self.produksi_langsung(hari(-2), 2, 10, sisa=0)      # Roti 10 x 8.000 = 80.000 -> 20.000
        saldo = self.saldo(uid)["saldo"]
        self.assertEqual(saldo, 20_000)
        for salah in ("0", "-5", "abc", ""):
            self.assertEqual(self.ambil(uid, salah)[0], 400, salah)
        self.assertEqual(self.ambil(uid, 20_001)[0], 400)                          # melebihi saldo
        self.assertEqual(self.ambil(uid, 5_000, tanggal=hari(1))[0], 400)          # tanggal besok
        self.assertEqual(self.ambil(uid, 5_000, saldo=19_000)[0], 409)             # saldo di layar sudah basi
        self.assertEqual(self.saldo(uid)["diambil"], 0)
        # penjualan hari ini masuk dan sisa belum diisi -> saldo sementara, perlu tanda paham
        self.produksi_langsung(app.today(), 2, 5)
        s = self.saldo(uid)
        self.assertTrue(s["sementara"])
        html = self.admin.get("/gaji")[1]
        self.assertIn("Saya paham saldo masih sementara", html)
        self.assertEqual(self.ambil(uid, 5_000)[0], 400)
        self.assertEqual(self.ambil(uid, 5_000, paham_sementara="1")[0], 200)
        self.assertEqual(self.saldo(uid)["diambil"], 5_000)


class TestDiambilLebih(Bantuan):
    def test_sisa_diisi_saldo_jadi_minus_lalu_terkejar(self):
        uid, _ = self.kakak(mulai=app.today())
        self.produksi_langsung(app.today(), 2, 10)            # 80.000 dianggap terjual -> 20.000 sementara
        self.assertEqual(self.ambil(uid, 20_000, paham_sementara="1")[0], 200)
        d = self.db()
        d.execute("INSERT INTO leftover VALUES(?,2,6,1,'x')", (app.today(),))   # ternyata 6 sisa: 4 x 8.000 = 32.000 -> 8.000
        d.commit()
        s = self.saldo(uid)
        self.assertEqual((s["terkumpul"], s["diambil"], s["saldo"]), (8_000, 20_000, -12_000))
        html = self.admin.get("/gaji")[1]
        self.assertIn("Diambil lebih Rp 12.000", html)
        self.assertIn("dikurangkan dari gaji berikutnya", html)
        self.assertNotIn("Bayar semua ·", html)
        self.assertEqual(self.admin.post(f"/gaji/{uid}/bayar", jumlah="-12000")[0], 400)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM wage_withdrawals WHERE dibatalkan = 0").fetchone()[0], 1)
        # penjualan berikutnya mengejar kekurangan
        self.produksi_langsung(app.today(), 1, 20)             # Donat 20 x 5.000 = 100.000 -> +25.000
        d = self.db()
        d.execute("INSERT INTO leftover VALUES(?,1,0,1,'x')", (app.today(),))
        d.commit()
        self.assertEqual(self.saldo(uid)["saldo"], 8_000 + 25_000 - 20_000)


class TestPembatalan(Bantuan):
    def test_batal_ditandai_bukan_dihapus(self):
        uid, _ = self.kakak()
        self.produksi_langsung(hari(-2), 1, 40, sisa=0)       # 200.000 -> 50.000
        self.ambil(uid, 30_000)
        w = self.db().execute("SELECT * FROM wage_withdrawals").fetchone()
        self.assertEqual(self.saldo(uid)["saldo"], 20_000)
        self.assertEqual(self.admin.post(f"/gaji-ambil/{w['id']}/batal", alasan="")[0], 400)
        s, html = self.admin.post(f"/gaji-ambil/{w['id']}/batal", alasan="salah ketik, harusnya 3.000")
        self.assertIn("dibatalkan", html)
        w = self.db().execute("SELECT * FROM wage_withdrawals").fetchone()
        self.assertEqual((w["dibatalkan"], w["alasan_batal"], w["dibatalkan_oleh"]), (1, "salah ketik, harusnya 3.000", 1))
        self.assertEqual(self.saldo(uid)["saldo"], 50_000)
        self.assertIn("salah ketik, harusnya 3.000", self.admin.get("/gaji")[1])
        self.assertIn("batalkan pengambilan gaji", [r[0] for r in self.db().execute("SELECT action FROM audit")])
        self.assertEqual(self.admin.post(f"/gaji-ambil/{w['id']}/batal", alasan="lagi")[0], 400)
        # pengambilan yang sudah masuk pembayaran tertutup tidak bisa dibatalkan
        self.ambil(uid, 3_000)
        w2 = self.db().execute("SELECT * FROM wage_withdrawals WHERE dibatalkan = 0").fetchone()
        self.admin.post(f"/gaji/{uid}/bayar", jumlah=str(self.saldo(uid)["saldo"]))
        self.assertEqual(self.admin.post(f"/gaji-ambil/{w2['id']}/batal", alasan="telat")[0], 400)


if __name__ == "__main__":
    unittest.main()
