"""Test zona waktu: jam dan "hari ini" mengikuti WIT (UTC+9).
Jalankan semua test:  python3 -m unittest -v
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

import app


class TestZonaWaktu(unittest.TestCase):
    @unittest.skipIf(os.environ.get("KUE_TZ"), "zona waktu diatur lewat KUE_TZ")
    def test_bawaan_wit(self):
        self.assertEqual(app.TZ.key, "Asia/Jayapura")
        self.assertEqual(app.now().utcoffset(), timedelta(hours=9))

    def test_jam_dan_hari_ini_ikut_wit(self):
        wit = datetime.now(timezone(timedelta(hours=9)))
        self.assertEqual(app.today(), wit.date().isoformat())
        selisih = datetime.strptime(app.stamp(), "%Y-%m-%d %H:%M:%S") - wit.replace(tzinfo=None)
        self.assertLess(abs(selisih.total_seconds()), 5)

    def test_tengah_malam_wit_ganti_hari(self):
        # 15:00 UTC = 00:00 WIT: tanggal sudah berganti, padahal di WIB masih 22:00 hari sebelumnya.
        t = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc).astimezone(app.TZ)
        self.assertEqual((t.date().isoformat(), t.hour), ("2026-11-25", 0))


if __name__ == "__main__":
    unittest.main()
