"""Test fase 3: unduh Excel (khusus admin) dan backup harian otomatis (disimpan 7 hari terakhir).
Jalankan semua test:  python3 -m unittest -v
"""
import io
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import zipfile
import xml.etree.ElementTree as ET

import app
from test_fase2 import Dasar

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def baca_xlsx(data):
    """Baca isi .xlsx kembali jadi {nama lembar: [[nilai sel]]}, seperti yang dilihat Excel."""
    z = zipfile.ZipFile(io.BytesIO(data))
    nama = [s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS)]
    hasil = {}
    for i, n in enumerate(nama, 1):
        akar = ET.fromstring(z.read(f"xl/worksheets/sheet{i}.xml"))
        hasil[n] = [[(c.find("m:v", NS).text if c.find("m:v", NS) is not None else c.find("m:is/m:t", NS).text)
                     for c in row] for row in akar.find("m:sheetData", NS)]
    return hasil


class TestUnduhExcel(Dasar):
    def unduh(self, klien):
        try:
            with klien.op.open(klien.base + "/unduh-excel") as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def test_admin_dapat_file_excel_berisi_produksi_sisa_gaji(self):
        uid, _ = self.karyawan("excel1", "produksi", pct=25)
        self.admin.post("/produksi", cake_id="1", qty="12", tanggal=app.today())
        self.admin.post("/sisa", tanggal=app.today(), sisa_1="2")
        s, h, data = self.unduh(self.admin)
        self.assertEqual(s, 200)
        self.assertIn("spreadsheetml", h["Content-Type"])
        self.assertIn(f'web-bakery-{app.today()}.xlsx', h["Content-Disposition"])

        x = baca_xlsx(data)
        self.assertEqual(list(x), ["Produksi", "Sisa", "Gaji", "Riwayat gaji"])
        self.assertIn([app.today(), "Donat", "12", "5000"], [r[:4] for r in x["Produksi"]])
        baris_sisa = [r for r in x["Sisa"] if r[:2] == [app.today(), "Donat"]][0]
        self.assertEqual(baris_sisa[2:5], [str(app.laporan_harian(self.db(), app.today())["items"][0]["dibuat"]), "2",
                                           str(app.laporan_harian(self.db(), app.today())["items"][0]["terjual"])])
        nama_gaji = [r[0] for r in x["Gaji"][1:]]
        self.assertIn("Excel1", nama_gaji)
        u = self.db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        baris = [r for r in x["Gaji"] if r[0] == "Excel1"][0]
        self.assertEqual(int(baris[7]), app.saldo_info(self.db(), u)["saldo"])  # sama dengan halaman Gaji

    def test_hanya_admin(self):
        _, klien = self.karyawan("excel2", "produksi", "omzet", "gaji")
        s, _, data = self.unduh(klien)
        self.assertEqual(s, 403)
        self.assertFalse(data.startswith(b"PK"))  # bukan file Excel
        self.assertNotIn("/unduh-excel", klien.get("/lainnya")[1])
        self.assertIn("/unduh-excel", self.admin.get("/lainnya")[1])

    def test_belum_masuk_dialihkan_ke_login(self):
        from test_app import Klien
        s, body = Klien(self.base).get("/unduh-excel")
        self.assertEqual(s, 200)
        self.assertIn("Masuk", body)

    def test_teks_aneh_tetap_jadi_file_valid(self):
        data = app.buat_xlsx([("Uji", [["Nama", "Angka"], ['Roti <"&"> \x01', 7], ["", None]])])
        self.assertEqual(baca_xlsx(data)["Uji"][1], ['Roti <"&"> ', "7"])


class TestBackupHarian(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "toko.db")
        d = sqlite3.connect(self.db_path)
        d.execute("CREATE TABLE t(x)")
        d.execute("INSERT INTO t VALUES (1), (2)")
        d.commit()
        d.close()
        self.folder = os.path.join(self.dir, "backup")

    def isi(self, path):
        d = sqlite3.connect(path)
        try:
            return d.execute("PRAGMA integrity_check").fetchone()[0], d.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        finally:
            d.close()

    def test_salinan_utuh_sekali_sehari(self):
        p = app.backup_harian(self.db_path, self.folder)
        self.assertEqual(os.path.basename(p), f"toko-{app.today()}.db")
        self.assertEqual(self.isi(p), ("ok", 2))

        d = sqlite3.connect(self.db_path)
        d.execute("INSERT INTO t VALUES (3)")
        d.commit()
        d.close()
        self.assertIsNone(app.backup_harian(self.db_path, self.folder))  # hari sama: tidak menyalin lagi
        self.assertEqual(os.listdir(self.folder), [f"toko-{app.today()}.db"])
        self.assertEqual(self.isi(p), ("ok", 2))

    def test_hanya_simpan_7_hari_terakhir(self):
        os.makedirs(self.folder)
        hari_ini = app.date.fromisoformat(app.today())
        nama = lambda n: f"toko-{(hari_ini - app.timedelta(days=n)).isoformat()}.db"
        for n in range(1, 10):                       # backup kemarin s/d 9 hari lalu
            with open(os.path.join(self.folder, nama(n)), "wb") as f:
                f.write(b"backup lama")
        for lain in ("catatan.txt", "toko-sebelum-wit.db"):   # file lain tidak boleh tersentuh
            with open(os.path.join(self.folder, lain), "wb") as f:
                f.write(b"x")
        app.backup_harian(self.db_path, self.folder, simpan_hari=7)
        sisa = set(os.listdir(self.folder))
        self.assertEqual(sisa, {nama(n) for n in range(0, 7)} | {"catatan.txt", "toko-sebelum-wit.db"})

    def test_simpan_0_berarti_tidak_menghapus(self):
        os.makedirs(self.folder)
        lama = os.path.join(self.folder, "toko-2020-01-01.db")
        with open(lama, "wb") as f:
            f.write(b"backup lama")
        app.backup_harian(self.db_path, self.folder, simpan_hari=0)
        self.assertTrue(os.path.exists(lama))

    def test_berkala_di_latar_dan_bisa_dimatikan(self):
        lama_db, lama_dir = app.DB_PATH, app.BACKUP_DIR
        app.DB_PATH, app.BACKUP_DIR = self.db_path, self.folder
        try:
            self.assertIsNone(app.mulai_backup_berkala(""))  # kosong = mati
            berhenti = app.mulai_backup_berkala("00:05")
            for _ in range(50):
                if os.path.exists(os.path.join(self.folder, f"toko-{app.today()}.db")):
                    break
                time.sleep(0.05)
            berhenti.set()
            self.assertTrue(os.path.exists(os.path.join(self.folder, f"toko-{app.today()}.db")))
        finally:
            app.DB_PATH, app.BACKUP_DIR = lama_db, lama_dir


class TestJadwalBackup(unittest.TestCase):
    def test_jam_berikutnya_wit(self):
        jam = lambda h, m: app.datetime(2026, 11, 25, h, m, tzinfo=app.TZ)
        self.assertEqual(app.detik_sampai("00:05", jam(0, 0)), 5 * 60)            # sebentar lagi, hari yang sama
        self.assertEqual(app.detik_sampai("00:05", jam(0, 5)), 24 * 3600)         # baru saja lewat: besok
        self.assertEqual(app.detik_sampai("00:05", jam(23, 0)), 65 * 60)          # malam: besok jam 00:05
        self.assertEqual(app.detik_sampai("23:55", jam(12, 0)), 11 * 3600 + 55 * 60)


if __name__ == "__main__":
    unittest.main()
