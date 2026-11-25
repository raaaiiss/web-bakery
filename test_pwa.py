"""Test pintasan layar utama (PWA): manifest, ikon, tag meta iPhone, tanpa cache untuk halaman data,
dan TANPA service worker.  Jalankan semua test:  python3 -m unittest -v
"""
import json
import struct
import urllib.error
import urllib.request

import app
from test_app import Klien
from test_fase2 import Dasar


def ambil(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers, r.read()


def ukuran_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "bukan PNG"
    return struct.unpack(">II", data[16:24])


class TestPWA(Dasar):
    def test_manifest_bisa_diambil_tanpa_login(self):
        s, h, data = ambil(self.base + "/manifest.webmanifest")
        self.assertEqual(s, 200)
        self.assertEqual(h["Content-Type"], "application/manifest+json")
        m = json.loads(data)
        self.assertEqual((m["display"], m["start_url"], m["scope"], m["lang"]), ("standalone", "/", "/", "id"))
        self.assertEqual(m["short_name"], app.NAMA_TOKO)
        self.assertIn(app.NAMA_TOKO, m["name"])
        self.assertEqual((m["theme_color"], m["background_color"]), ("#F7F1E6", "#F7F1E6"))
        ukuran = {(i["sizes"], i["purpose"]) for i in m["icons"]}
        self.assertEqual(ukuran, {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")})

    def test_semua_ikon_png_dengan_ukuran_benar(self):
        m = json.loads(ambil(self.base + "/manifest.webmanifest")[2])
        ikon = [(i["src"], i["sizes"]) for i in m["icons"]]
        ikon += [("/static/ikon/apple-touch-icon-180.png", "180x180"), ("/apple-touch-icon.png", "180x180"),
                 ("/apple-touch-icon-precomposed.png", "180x180")]
        for src, sizes in ikon:
            s, h, data = ambil(self.base + src)
            self.assertEqual((s, h["Content-Type"]), (200, "image/png"), src)
            w, t = ukuran_png(data)
            self.assertEqual(f"{w}x{t}", sizes, src)
        for salah in ("/static/ikon/tidak-ada.png", "/static/ikon/..%2F..%2Fapp.py"):
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(self.base + salah)
            self.assertEqual(err.exception.code, 404, salah)

    def test_tag_layar_penuh_ada_di_semua_halaman(self):
        tamu = Klien(self.base)
        halaman = [tamu.get("/login")[1]] + [self.admin.get(p)[1] for p in ("/", "/produksi", "/sisa", "/laporan", "/kue")]
        for html in halaman:
            for tag in ('<link rel="manifest" href="/manifest.webmanifest">',
                        '<meta name="apple-mobile-web-app-capable" content="yes">',
                        f'<meta name="apple-mobile-web-app-title" content="{app.NAMA_TOKO}">',
                        '<meta name="apple-mobile-web-app-status-bar-style" content="default">',
                        '<link rel="apple-touch-icon" href="/static/ikon/apple-touch-icon-180.png">',
                        '<meta name="theme-color" content="#F7F1E6">', "viewport-fit=cover"):
                self.assertIn(tag, html)

    def test_halaman_data_tidak_disimpan_dan_tanpa_service_worker(self):
        for path in ("/", "/laporan", "/gaji", "/sisa-harian"):
            with self.admin.op.open(self.base + path) as r:
                self.assertEqual(r.headers["Cache-Control"], "no-store", path)
                self.assertNotIn("serviceWorker", r.read().decode(), path)
        self.assertEqual(self.admin.get("/sw.js")[0], 404)
        self.assertEqual(self.admin.get("/service-worker.js")[0], 404)


if __name__ == "__main__":
    import unittest
    unittest.main()
