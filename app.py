#!/usr/bin/env python3
"""Pencatatan Toko Kue, prototype versi 1.

Hanya memakai pustaka bawaan Python 3.9+ (http.server + sqlite3), jadi tidak perlu install apa pun.
Jalankan:  python3 app.py      lalu buka http://<ip-server>:8000 dari HP di wifi toko.
"""
import hashlib
import html
import json
import base64
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("KUE_DB", os.path.join(BASE, "data", "toko.db"))
TZ = ZoneInfo(os.environ.get("KUE_TZ", "Asia/Jakarta"))
HOST = os.environ.get("KUE_HOST", "0.0.0.0")
PORT = int(os.environ.get("KUE_PORT", "8000"))
SESSION_DAYS = 30
# Nama toko untuk judul, ikon layar utama, dan manifest. Nama asli toko belum diberikan pemilik.
NAMA_TOKO = os.environ.get("KUE_NAMA_TOKO", "Toko Kue")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','karyawan')));
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cakes(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, price_sell INTEGER NOT NULL,
  price_cost INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1);
-- Tiap batch produksi menyimpan salinan harga saat itu, jadi ganti harga tidak mengubah catatan lama.
CREATE TABLE IF NOT EXISTS production(
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, cake_id INTEGER NOT NULL REFERENCES cakes(id),
  qty INTEGER NOT NULL CHECK(qty > 0), price_sell INTEGER NOT NULL, price_cost INTEGER NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id), created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leftover(
  date TEXT NOT NULL, cake_id INTEGER NOT NULL REFERENCES cakes(id), qty INTEGER NOT NULL CHECK(qty >= 0),
  user_id INTEGER NOT NULL REFERENCES users(id), updated TEXT NOT NULL, PRIMARY KEY(date, cake_id));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, user_id INTEGER, action TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS production_date ON production(date);
"""


# ---------- bantuan umum ----------

def now():
    return datetime.now(TZ)


def today():
    return now().date().isoformat()


def stamp():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def e(x):
    return html.escape(str(x), quote=True)


def rupiah(n):
    s = f"{abs(n):,}".replace(",", ".")
    return f"-Rp {s}" if n < 0 else f"Rp {s}"


def tgl_indo(iso):
    hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    bulan = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
    d = date.fromisoformat(iso)
    return f"{hari[d.weekday()]}, {d.day} {bulan[d.month - 1]} {d.year}"


class Gagal(Exception):
    def __init__(self, pesan, status=400):
        super().__init__(pesan)
        self.status = status


def angka(s, nama, minimal=0):
    s = (s or "").replace("Rp", "").replace(".", "").replace(",", "").strip()
    if not s.isdigit():
        raise Gagal(f"{nama} harus berupa angka.")
    n = int(s)
    if n < minimal:
        raise Gagal(f"{nama} minimal {minimal}.")
    return n


def tanggal(s):
    try:
        return date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise Gagal("Tanggal tidak valid.")


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${h}"


def cek_pw(pw, simpanan):
    salt, _ = simpanan.split("$", 1)
    return secrets.compare_digest(hash_pw(pw, salt), simpanan)


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db():
    db = connect()
    db.executescript(SCHEMA)
    migrasi(db)
    migrasi_tampilan(db)
    migrasi_gaji(db)
    db.close()


def catat(db, user_id, aksi, detail):
    db.execute("INSERT INTO audit(at, user_id, action, detail) VALUES(?,?,?,?)", (stamp(), user_id, aksi, detail))


# ---------- hitungan ----------

def hitung(dibuat, sisa, harga_jual, harga_modal):
    """Rumus dari brief awal. 'untung' masih ASUMSI, menunggu konfirmasi pemilik."""
    terjual = dibuat - sisa
    omzet = terjual * harga_jual
    kerugian = sisa * harga_modal
    untung = omzet - terjual * harga_modal
    return {"dibuat": dibuat, "sisa": sisa, "terjual": terjual, "harga_jual": harga_jual,
            "harga_modal": harga_modal, "omzet": omzet, "kerugian": kerugian, "untung": untung,
            "untung_bersih": untung - kerugian}


def laporan_harian(db, tgl):
    # Harga per kue per hari = harga pada batch terakhir hari itu.
    rows = db.execute("""
        SELECT c.id, c.name, SUM(p.qty) AS dibuat,
          (SELECT price_sell FROM production x WHERE x.cake_id = c.id AND x.date = :d ORDER BY x.id DESC LIMIT 1) AS hj,
          (SELECT price_cost FROM production x WHERE x.cake_id = c.id AND x.date = :d ORDER BY x.id DESC LIMIT 1) AS hm,
          (SELECT qty FROM leftover l WHERE l.cake_id = c.id AND l.date = :d) AS sisa
        FROM production p JOIN cakes c ON c.id = p.cake_id
        WHERE p.date = :d GROUP BY c.id ORDER BY c.name""", {"d": tgl}).fetchall()
    items = [dict(nama=r["name"], sisa_diisi=r["sisa"] is not None,
                  **hitung(r["dibuat"], r["sisa"] or 0, r["hj"], r["hm"])) for r in rows]
    total = {k: sum(i[k] for i in items)
             for k in ("dibuat", "terjual", "sisa", "omzet", "kerugian", "untung", "untung_bersih")}
    laku = max(items, key=lambda i: i["terjual"], default=None)
    banyak_sisa = max((i for i in items if i["sisa"] > 0), key=lambda i: i["sisa"], default=None)
    return {"items": items, "total": total, "laku": laku, "banyak_sisa": banyak_sisa,
            "sisa_belum": [i["nama"] for i in items if not i["sisa_diisi"]]}


# ---------- fase 2: akses, laporan rentang, gaji ----------

# Hak akses yang bisa dicentang admin per karyawan. "Kelola user" tidak ada di sini: itu khusus admin.
PERMS = [("produksi", "Input produksi"), ("sisa", "Input sisa"), ("omzet", "Lihat omzet"),
         ("untung", "Lihat untung"), ("gaji", "Lihat gaji sendiri"), ("modal", "Lihat harga modal"),
         ("kue", "Kelola kue & harga")]
PERM_KEYS = {k for k, _ in PERMS}
AKSES_KAKAK = ("produksi", "gaji", "omzet", "untung")
GAJI_PERSEN_DEFAULT = 25

MIGRASI = """
CREATE TABLE IF NOT EXISTS user_perms(
  user_id INTEGER NOT NULL REFERENCES users(id), perm TEXT NOT NULL, PRIMARY KEY(user_id, perm));
CREATE TABLE IF NOT EXISTS wage_payments(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  period_start TEXT NOT NULL, period_end TEXT NOT NULL, omzet INTEGER NOT NULL, pct INTEGER NOT NULL,
  amount INTEGER NOT NULL, paid_at TEXT NOT NULL, paid_by INTEGER NOT NULL REFERENCES users(id));
"""


def migrasi(db):
    """Versi 1 -> 2. Hanya menambah tabel dan kolom; tidak ada data yang dihapus. Aman dijalankan berulang."""
    if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    kolom = {r[1] for r in db.execute("PRAGMA table_info(users)")}
    with db:
        db.executescript(MIGRASI)
        if "pct" not in kolom:
            db.execute(f"ALTER TABLE users ADD COLUMN pct INTEGER NOT NULL DEFAULT {GAJI_PERSEN_DEFAULT}")
        if "gaji_mulai" not in kolom:
            db.execute("ALTER TABLE users ADD COLUMN gaji_mulai TEXT")
        awal = db.execute("SELECT MIN(date) FROM production").fetchone()[0] or today()
        db.execute("UPDATE users SET gaji_mulai = ? WHERE gaji_mulai IS NULL", (awal,))
        # Karyawan lama di versi 1 sudah bisa input produksi; akses itu dipertahankan.
        db.execute("""INSERT OR IGNORE INTO user_perms(user_id, perm)
                      SELECT id, 'produksi' FROM users WHERE role = 'karyawan'""")
        db.execute("PRAGMA user_version = 2")


# Warna kartu kue (teks putih di atasnya, kontras >= 5:1).
PALET = {"cokelat": "#7A4A2A", "karamel": "#A8521C", "madu": "#8C6210", "pandan": "#4F6B2D",
         "stroberi": "#A3344A", "ubi": "#6B4A80", "kopi": "#4A3426", "matcha": "#3F6B4A"}
FOTO_MAKS = 1_500_000      # byte, setelah diperkecil di browser
BODY_MAKS = 3_000_000      # batas ukuran kiriman form


def migrasi_gaji(db):
    """Tambah kolom users.digaji (1 = menerima gaji). Perilaku lama dipertahankan: karyawan dengan persen > 0
    dianggap menerima gaji, 0% tidak, admin tidak (sekarang gaji admin memang tidak dihitung)."""
    kolom = {r[1] for r in db.execute("PRAGMA table_info(users)")}
    if "digaji" in kolom:
        return
    with db:
        db.execute("ALTER TABLE users ADD COLUMN digaji INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE users SET digaji = 1 WHERE role = 'karyawan' AND pct > 0")


def migrasi_tampilan(db):
    """Tambah kolom warna dan foto kue. Hanya menambah kolom (cek dulu), data lama tidak berubah."""
    kolom = {r[1] for r in db.execute("PRAGMA table_info(cakes)")}
    with db:
        if "color" not in kolom:
            db.execute("ALTER TABLE cakes ADD COLUMN color TEXT")
        if "photo" not in kolom:
            db.execute("ALTER TABLE cakes ADD COLUMN photo TEXT")


def warna_kue(k):
    return PALET.get(k["color"] or "", list(PALET.values())[k["id"] % len(PALET)])


def inisial(nama):
    kata = [w for w in nama.split() if w[:1].isalnum()]
    return "".join(w[0] for w in kata[:2]).upper() or "?"


def gambar_kue(k, kelas="gbr"):
    """Foto kue kalau ada; kalau belum, kotak berwarna dengan inisial supaya kartu tidak kosong."""
    if k["photo"]:
        return f'<span class="{kelas}"><img src="/foto/{e(k["photo"])}" alt="" loading="lazy"></span>'
    return f'<span class="{kelas} inisial" style="background:{warna_kue(k)}" aria-hidden="true">{e(inisial(k["name"]))}</span>'


def foto_dir():
    return os.path.join(os.path.dirname(DB_PATH), "foto")


def hapus_file_foto(nama):
    if nama and re.fullmatch(r"kue-\d+-[0-9a-f]{12}\.jpg", nama):
        try:
            os.remove(os.path.join(foto_dir(), nama))
        except FileNotFoundError:
            pass


def hari_hari(dari, sampai):
    d, akhir = date.fromisoformat(dari), date.fromisoformat(sampai)
    while d <= akhir:
        yield d.isoformat()
        d += timedelta(days=1)


def laporan_rentang(db, dari, sampai):
    """Gabungan laporan_harian per hari, jadi rumus dan harga tersimpan sama persis dengan laporan harian."""
    hari = [r[0] for r in db.execute(
        "SELECT DISTINCT date FROM production WHERE date BETWEEN ? AND ? ORDER BY date", (dari, sampai))]
    per_kue, sisa_belum = {}, []
    angka_ = ("dibuat", "terjual", "sisa", "omzet", "kerugian", "untung", "untung_bersih")
    for d in hari:
        lp = laporan_harian(db, d)
        for i in lp["items"]:
            k = per_kue.setdefault(i["nama"], {"nama": i["nama"], "hari_sisa": 0, **{a: 0 for a in angka_}})
            for a in angka_:
                k[a] += i[a]
            k["hari_sisa"] += 1 if i["sisa"] > 0 else 0
        sisa_belum += [(d, n) for n in lp["sisa_belum"]]
    items = sorted(per_kue.values(), key=lambda k: k["nama"])
    total = {a: sum(k[a] for k in items) for a in angka_}
    laku = max(items, key=lambda k: k["terjual"], default=None)
    sering_sisa = max((k for k in items if k["hari_sisa"] > 0), key=lambda k: (k["hari_sisa"], k["sisa"]), default=None)
    return {"dari": dari, "sampai": sampai, "hari": hari, "items": items, "total": total,
            "laku": laku, "sering_sisa": sering_sisa, "sisa_belum": sisa_belum}


def sisa_per_hari(db, dari, sampai):
    """Sisa per kue per hari + daftar batch per tanggal buat. Rumus tidak berubah: angka uang dari laporan_harian."""
    hari = list(hari_hari(dari, sampai))
    dibuat = {(r["date"], r["cake_id"]): r["q"] for r in db.execute(
        "SELECT date, cake_id, SUM(qty) q FROM production WHERE date BETWEEN ? AND ? GROUP BY date, cake_id", (dari, sampai))}
    sisa = {(r["date"], r["cake_id"]): r["qty"] for r in db.execute(
        "SELECT date, cake_id, qty FROM leftover WHERE date BETWEEN ? AND ?", (dari, sampai))}
    ids = sorted({c for _, c in dibuat})
    kue = {r["id"]: r for r in db.execute(
        f"SELECT * FROM cakes WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    baris = []
    for cid in sorted(ids, key=lambda c: kue[c]["name"]):
        sel = {}
        for d in hari:
            if (d, cid) not in dibuat:
                sel[d] = None                         # tidak ada produksi
            else:
                sel[d] = sisa.get((d, cid), "?")      # "?" = sisa belum diisi
        angka_ = [v for v in sel.values() if isinstance(v, int)]
        baris.append({"kue": kue[cid], "sel": sel, "hari_sisa": sum(1 for v in angka_ if v > 0),
                      "hari_habis": sum(1 for v in angka_ if v == 0), "total_sisa": sum(angka_)})
    batch = {}
    for r in db.execute("""SELECT p.date, p.cake_id, p.qty, p.created FROM production p
                           WHERE p.date BETWEEN ? AND ? ORDER BY p.date DESC, p.id""", (dari, sampai)):
        batch.setdefault(r["date"], {}).setdefault(r["cake_id"], []).append(r)
    return {"hari": hari, "baris": baris, "batch": batch, "kue": kue, "sisa": sisa, "dibuat": dibuat}


def gaji_info(db, user, sampai=None):
    """Gaji berjalan: persen user x omzet (harga jual tersimpan x terjual) sejak pembayaran terakhir.
    Tidak dikurangkan dari untung (menunggu keputusan pemilik)."""
    sampai = sampai or today()
    akhir = db.execute("SELECT MAX(period_end) FROM wage_payments WHERE user_id = ?", (user["id"],)).fetchone()[0]
    mulai = user["gaji_mulai"] or today()
    if akhir:
        mulai = max(mulai, (date.fromisoformat(akhir) + timedelta(days=1)).isoformat())
    lp = laporan_rentang(db, mulai, sampai) if mulai <= sampai else None
    omzet = lp["total"]["omzet"] if lp else 0
    return {"mulai": mulai, "sampai": sampai, "omzet": omzet, "pct": user["pct"],
            "jumlah": omzet * user["pct"] // 100, "sisa_belum": lp["sisa_belum"] if lp else [],
            "sudah_lunas": mulai > sampai}


# ---------- tampilan ----------
# Arah desain (disetujui pemilik): "etalase roti pagi hari". Krem kertas, tinta kakao, tombol kulit roti,
# garis tipis (tanpa bayangan), ikon garis SVG, angka penting besar, aksi utama di zona jempol.

FONTS = {"karla.woff2", "playfair.woff2"}
IKON_FILE = {"ikon-192.png", "ikon-512.png", "ikon-maskable-512.png", "apple-touch-icon-180.png"}
WARNA_LATAR = "#F7F1E6"


def manifest():
    """Web app manifest untuk "Tambahkan ke layar utama". Sengaja TANPA service worker: data selalu dari server."""
    return {
        "name": f"{NAMA_TOKO} — Catatan Harian",
        "short_name": NAMA_TOKO[:12],
        "description": "Catatan produksi, sisa, laporan, dan gaji toko kue",
        "lang": "id",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "theme_color": WARNA_LATAR,
        "background_color": WARNA_LATAR,
        "icons": [
            {"src": "/static/ikon/ikon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/ikon/ikon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/ikon/ikon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }


def kepala_html(judul):
    """Tag <head> bersama untuk semua halaman: layar penuh di HP (Android & iPhone), ikon, dan warna tema."""
    nama = e(NAMA_TOKO)
    return f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="{WARNA_LATAR}">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="{nama}">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="application-name" content="{nama}">
<link rel="apple-touch-icon" href="/static/ikon/apple-touch-icon-180.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/ikon/ikon-192.png">
<title>{e(judul)} · {nama}</title>"""

CSS = """
@font-face{font-family:Karla;src:url(/static/fonts/karla.woff2) format('woff2');font-weight:400 800;font-display:swap}
@font-face{font-family:Playfair;src:url(/static/fonts/playfair.woff2) format('woff2');font-weight:600 700;font-display:swap}
:root{
  --bg:#F7F1E6;--surface:#FFFBF4;--tint:#F1E4D1;--line:#E6D9C3;--line-strong:#CDB89B;
  --ink:#2E1F14;--ink-2:#6B5644;--crust:#92400E;--crust-press:#74330B;--on-crust:#FFFFFF;
  --olive:#4F6B2D;--terra:#B4532A;--terra-text:#93401C;--danger:#B3261E;--cream-on-dark:#F7E9D4;
  --r:14px;--r-sm:10px;--nav-h:64px;--bar-h:84px;
  --serif:Playfair,Georgia,serif;--sans:Karla,system-ui,-apple-system,sans-serif;
  color-scheme:light}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:400 17px/1.5 var(--sans);font-variant-numeric:tabular-nums}
a{color:var(--crust);text-underline-offset:3px}
:focus-visible{outline:3px solid var(--crust);outline-offset:2px;border-radius:6px}
.ikon{width:24px;height:24px;flex:none;fill:none;stroke:currentColor;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}

/* kerangka */
.atas{display:flex;align-items:center;gap:12px;padding:max(14px,env(safe-area-inset-top)) max(20px,env(safe-area-inset-right)) 0 max(20px,env(safe-area-inset-left));max-width:760px;margin:0 auto}
.merek{font:700 19px/1 var(--serif);color:var(--crust);text-decoration:none;letter-spacing:.2px}
.atas .siapa{margin-left:auto;font-size:14px;color:var(--ink-2)}
.atas form{margin:0}
main{max-width:760px;margin:0 auto;padding:8px max(20px,env(safe-area-inset-right)) calc(var(--nav-h) + 32px + env(safe-area-inset-bottom)) max(20px,env(safe-area-inset-left))}
body.ada-bar main{padding-bottom:calc(var(--nav-h) + var(--bar-h) + 32px + env(safe-area-inset-bottom))}
body.tanpa-nav main{padding-bottom:calc(32px + env(safe-area-inset-bottom))}
body.tanpa-nav.ada-bar main{padding-bottom:calc(var(--bar-h) + 32px + env(safe-area-inset-bottom))}
.alis{margin:20px 0 2px;font-size:14px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;color:var(--ink-2)}
h1{font:700 30px/1.15 var(--serif);margin:0 0 20px;letter-spacing:-.2px}
h2{font:700 13px/1.3 var(--sans);letter-spacing:.8px;text-transform:uppercase;color:var(--ink-2);margin:32px 0 8px}
.kecil{font-size:14px;color:var(--ink-2)}

/* navigasi bawah (HP) */
.nav{position:fixed;left:0;right:0;bottom:0;z-index:10;display:flex;background:var(--surface);
  border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
.nav a,.nav button{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  min-height:var(--nav-h);color:var(--ink-2);text-decoration:none;font:600 13px/1 var(--sans);position:relative}
.nav a[aria-current]{color:var(--crust)}
.nav a[aria-current]::before{content:"";position:absolute;top:0;left:28%;right:28%;height:3px;border-radius:0 0 3px 3px;background:var(--crust)}
.nav a:active{background:var(--tint)}

/* bar aksi menempel di zona jempol */
.bar{position:fixed;left:0;right:0;bottom:calc(var(--nav-h) + env(safe-area-inset-bottom));z-index:9;
  background:var(--bg);border-top:1px solid var(--line);padding:12px 20px}
body.tanpa-nav .bar{bottom:0;padding-bottom:calc(12px + env(safe-area-inset-bottom))}
.bar .isi{max-width:720px;margin:0 auto;display:flex;gap:12px}
.bar .tombol{flex:1;padding:0 12px;font-size:17px;white-space:nowrap}

/* tombol */
.tombol{display:inline-flex;align-items:center;justify-content:center;gap:10px;min-height:56px;padding:0 22px;
  border-radius:var(--r);border:2px solid var(--crust);background:var(--crust);color:var(--on-crust);
  font:700 18px/1.2 var(--sans);text-align:center;text-decoration:none;cursor:pointer;transition:background-color .12s}
.tombol:active{background:var(--crust-press);border-color:var(--crust-press)}
.tombol.garis{background:transparent;color:var(--crust)}
.tombol.garis:active{background:var(--tint)}
.tombol.penuh{width:100%}
.tombol.sedang{min-height:48px;font-size:16px;padding:0 18px}
.teks-merah{background:none;border:0;color:var(--danger);font:600 16px/1 var(--sans);padding:14px 4px;cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.biasa{background:none;border:0;color:var(--crust);font:600 15px/1 var(--sans);padding:12px 4px;cursor:pointer}

/* blok utama beranda: satu-satunya blok gelap */
.utama-blok{background:var(--ink);color:var(--cream-on-dark);border-radius:var(--r);padding:20px 22px 18px}
.utama-blok .label{font-size:15px;font-weight:600;opacity:.85}
.utama-blok .angka{font:800 44px/1.05 var(--sans);letter-spacing:-1px;margin:6px 0 8px;color:#fff;font-variant-numeric:tabular-nums}
.utama-blok .angka small{font-size:22px;font-weight:700;margin-right:6px;letter-spacing:0}
.utama-blok .sub{font-size:15px;opacity:.85}

/* dua angka berdampingan, dipisah garis */
.duo{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line)}
.duo>div{padding:18px 0 16px}
.duo>div+div{padding-left:18px;border-left:1px solid var(--line)}
.duo .label{font-size:14px;font-weight:600;color:var(--ink-2)}
.duo .nilai{font:800 26px/1.15 var(--sans);letter-spacing:-.5px;margin-top:4px}
.untung{color:var(--olive)}.rugi{color:var(--terra)}

/* angka besar laporan */
.besar .label{font-size:15px;font-weight:600;color:var(--ink-2)}
.besar .angka{font:800 44px/1.05 var(--sans);letter-spacing:-1px;margin:4px 0 2px}
.besar .angka small{font-size:22px;font-weight:700;margin-right:6px;letter-spacing:0}

/* daftar bergaris (pengganti kartu) */
.daftar{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.daftar>li{border-bottom:1px solid var(--line);padding:14px 0}
.baris{display:flex;align-items:center;gap:12px}
.baris .nama{font-weight:700;font-size:17px}
.baris .kanan{margin-left:auto;text-align:right}
.qty{font:800 22px/1 var(--sans)}
.info{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:14px;color:var(--ink-2);margin-top:4px}

/* pemberitahuan */
.kabar{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:var(--r-sm);margin:0 0 16px;font-size:16px}
.kabar .ikon{width:20px;height:20px;margin-top:2px}
.kabar.ok{background:#E7EEDD;color:#33461C}
.kabar.awas{background:#F8E7C6;color:#5E3B0A}
.kabar.galat{background:#F6DEDA;color:#7A1C15}

/* formulir */
label,.lbl{display:block;font-weight:700;font-size:16px;margin:18px 0 8px}
input[type=text],input[type=password],input[type=date],input:not([type]){width:100%;min-height:52px;padding:0 14px;font:500 18px var(--sans);
  color:var(--ink);background:var(--surface);border:1.5px solid var(--line-strong);border-radius:var(--r-sm)}
input:focus{border-color:var(--crust);outline:none;box-shadow:0 0 0 3px rgba(146,64,14,.18)}
.bantu{font-size:14px;color:var(--ink-2);margin-top:6px}

/* pilih kue: ubin besar */
.ubin{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.ubin input{position:absolute;opacity:0;pointer-events:none}
.ubin label{margin:0;display:flex;flex-direction:column;justify-content:center;min-height:72px;padding:12px 14px;
  background:var(--surface);border:1.5px solid var(--line-strong);border-radius:var(--r);cursor:pointer;font-size:17px;line-height:1.25}
.ubin label span{font-weight:500;font-size:14px;color:var(--ink-2);margin-top:4px}
.ubin input:checked+label{border:2px solid var(--crust);background:var(--tint);color:var(--crust)}
.ubin input:focus-visible+label{outline:3px solid var(--crust);outline-offset:2px}

/* stepper − angka + */
.stepper{display:flex;align-items:center;gap:8px}
.stepper button{width:52px;height:52px;flex:none;border-radius:var(--r-sm);border:1.5px solid var(--line-strong);background:var(--surface);
  color:var(--crust);display:flex;align-items:center;justify-content:center;cursor:pointer}
.stepper button:active{background:var(--tint)}
.stepper input{min-width:0;width:76px;min-height:52px;text-align:center;font:800 22px var(--sans);padding:0 4px}
.stepper.lebar input{flex:1;width:auto;font-size:30px}

/* ganti tanggal */
.tgl{display:flex;align-items:center;gap:8px;margin:4px 0 8px}
.tgl a{width:48px;height:48px;flex:none;display:flex;align-items:center;justify-content:center;border-radius:var(--r-sm);
  border:1.5px solid var(--line-strong);color:var(--ink);background:var(--surface)}
.tgl input{flex:1}

/* buka-tutup (ubah data) */
details>summary{list-style:none;cursor:pointer}
details>summary::-webkit-details-marker{display:none}
details[open]>summary .biasa{color:var(--ink-2)}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:4px 16px 16px;margin-top:10px}
.tambah{margin:0 0 8px}
.tambah>summary{display:flex}

/* masuk */
.masuk{max-width:440px;margin:0 auto;padding:max(48px,calc(env(safe-area-inset-top) + 24px)) max(24px,env(safe-area-inset-right)) calc(32px + env(safe-area-inset-bottom)) max(24px,env(safe-area-inset-left))}
.masuk .merek{font-size:34px;display:block;margin-bottom:6px}

/* sakelar Ya/Tidak */
.sakelar{display:flex;align-items:center;gap:12px;min-height:56px;margin:0;font-weight:600;font-size:17px;cursor:pointer}
.sakelar input{appearance:none;-webkit-appearance:none;width:56px;height:32px;margin:0;border-radius:999px;background:var(--line-strong);
  position:relative;flex:none;cursor:pointer;transition:background-color .15s}
.sakelar input::after{content:"";position:absolute;top:3px;left:3px;width:26px;height:26px;border-radius:50%;background:#fff;
  box-shadow:0 1px 2px rgba(0,0,0,.25);transition:transform .15s}
.sakelar input:checked{background:var(--olive)}
.sakelar input:checked::after{transform:translateX(24px)}
.sakelar input:focus-visible{outline:3px solid var(--crust);outline-offset:2px}
.sakelar input:checked+span .tidak,.sakelar input:not(:checked)+span .ya{display:none}
.sakelar .ya{color:var(--olive)}.sakelar .tidak{color:var(--ink-2)}

/* filter periode laporan */
.pilih{display:flex;gap:6px;margin:4px 0 12px}
.pilih a{flex:1;justify-content:center;white-space:nowrap;min-height:44px;display:flex;align-items:center;padding:0 6px;border-radius:999px;border:1.5px solid var(--line-strong);
  color:var(--ink);text-decoration:none;font-weight:700;font-size:15px;background:var(--surface)}
.pilih a[aria-current]{background:var(--ink);border-color:var(--ink);color:#fff}
.rentang{display:grid;grid-template-columns:1fr 1fr;gap:0 10px;align-items:end;margin-bottom:8px}
.rentang>div{min-width:0}.rentang input{min-width:0;padding:0 10px}
.rentang label{margin:4px 0 6px;font-size:14px}
.rentang .tombol{grid-column:1/-1;margin-top:10px}

/* gaji */
.gaji-blok{padding:16px 0 20px;border-bottom:1px solid var(--line)}
.gaji-blok+.gaji-blok{margin-top:8px}

/* centang akses */
.akses{border:0;padding:0;margin:0}
.centang{display:flex;align-items:center;gap:12px;min-height:48px;margin:0;font-weight:500;font-size:17px;border-bottom:1px solid var(--line)}
.centang input{width:24px;height:24px;flex:none;accent-color:var(--crust)}
.centang.mati{color:var(--ink-2)}
.centang small{font-size:14px}

/* gambar kue: foto atau warna + inisial */
.gbr{display:flex;align-items:center;justify-content:center;overflow:hidden;color:#fff;font:800 22px/1 var(--sans);letter-spacing:.5px;background:var(--tint)}
.gbr img{width:100%;height:100%;object-fit:cover;display:block}
.gbr.mini{width:52px;height:52px;flex:none;border-radius:12px;font-size:17px}
.gbr.pratinjau{width:96px;height:72px;flex:none;border-radius:12px;font-size:24px}
.sembunyi{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.tampilan{border:0;padding:0;margin:8px 0 0}
.pilih-foto{margin:0}
.palet{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.swatch{margin:0;display:flex;flex-direction:column;align-items:center;gap:4px;font-weight:500;cursor:pointer}
.swatch input{position:absolute;opacity:0;pointer-events:none}
.swatch span{width:100%;height:40px;border-radius:10px;border:3px solid transparent}
.swatch small{font-size:13px;color:var(--ink-2)}
.swatch input:checked+span{border-color:var(--ink);box-shadow:inset 0 0 0 2px #fff}
.swatch input:focus-visible+span{outline:3px solid var(--crust);outline-offset:2px}

/* kartu pilih kue (Produksi) */
.kartu-kue{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.kartu-kue>input{position:absolute;opacity:0;pointer-events:none}
.kartu-kue label{position:relative;margin:0;display:flex;flex-direction:column;background:var(--surface);border:2px solid var(--line);
  border-radius:18px;overflow:hidden;cursor:pointer;font-weight:700;transition:border-color .12s}
.kartu-kue .gbr.foto{aspect-ratio:3/2;width:100%;font-size:34px;border-radius:0}
.kartu-kue .nama-kue{padding:10px 12px 0;font-size:16px;line-height:1.25}
.kartu-kue .sub{padding:2px 12px 12px;font-weight:500;font-size:14px;color:var(--ink-2)}
.kartu-kue .cek{position:absolute;top:8px;right:8px;width:32px;height:32px;border-radius:50%;background:var(--crust);color:#fff;
  display:none;align-items:center;justify-content:center;box-shadow:0 0 0 3px #fff}
.kartu-kue .cek .ikon{width:20px;height:20px;stroke-width:2.5}
.kartu-kue>input:checked+label{border-color:var(--crust);background:var(--tint)}
.kartu-kue>input:checked+label .cek{display:flex}
.kartu-kue>input:focus-visible+label{outline:3px solid var(--crust);outline-offset:2px}
.stepper.di-bar{flex:none;gap:6px}
.stepper.di-bar button{width:48px;height:56px}
.stepper.di-bar input{width:64px;min-height:56px}

/* sisa per batch */
.daftar li>.baris:first-child:has(.gbr.mini)+details{margin-left:64px}
.sisa-baris{display:block}
.sisa-baris .stepper{gap:6px}.sisa-baris .stepper button{width:46px;height:50px}.sisa-baris .stepper input{width:58px;min-height:50px}
.batch-info{font-size:14px;color:var(--ink-2);margin:8px 0 0 64px}

/* sisa per kue per hari */
.geser{overflow-x:auto;margin:0 -20px;padding:0 20px}
.grid-sisa{border-collapse:separate;border-spacing:2px;font-size:15px}
.grid-sisa th{font-size:12px;font-weight:700;color:var(--ink-2);text-align:center;padding:2px;line-height:1.2}
.grid-sisa td{min-width:31px;height:36px;text-align:center;border-radius:8px;font-weight:800;padding:0}
.grid-sisa .kolom-kue{position:sticky;left:0;background:var(--bg);text-align:left;font-size:14px;color:var(--ink);
  min-width:96px;max-width:116px;padding-right:4px}
.grid-sisa .kolom-kue .baris{gap:8px}
.gbr.titik{width:22px;height:22px;flex:none;border-radius:6px;font-size:10px}
.grid-sisa td.habis,.cth.habis{background:#E1EBD4;color:#33461C}
.grid-sisa td.ada,.cth.ada{background:#F6DDCF;color:#7A2F12}
.grid-sisa td.belum{background:#F8E7C6;color:#5E3B0A}
.grid-sisa td.kosong{color:var(--line-strong);font-weight:500}
.grid-sisa th.hariini{color:var(--crust)}
.cth{display:inline-block;min-width:22px;text-align:center;border-radius:6px;font-weight:800;padding:0 4px;background:var(--tint)}
.tgl-batch{font:700 15px/1.3 var(--sans);margin:18px 0 6px}
.rugi-teks{color:var(--terra-text)}

/* beranda */
.kepala{background:var(--crust);color:#fff;border-radius:22px;padding:18px 20px 16px;margin-bottom:8px}
.kepala .sapa{font:700 18px/1.2 var(--serif);opacity:.95}
.kepala .label{font-size:15px;font-weight:600;opacity:.9;margin-top:12px}
.kepala .angka{font:800 42px/1.05 var(--sans);letter-spacing:-1px;margin:4px 0 0}
.kepala .angka small{font-size:21px;font-weight:700;margin-right:6px;letter-spacing:0}
.statistik{display:grid;grid-template-columns:repeat(3,1fr);margin-top:14px;padding-top:12px;border-top:1px solid rgba(255,255,255,.3)}
.statistik div{display:flex;flex-direction:column}
.statistik div+div{padding-left:14px;border-left:1px solid rgba(255,255,255,.3)}
.statistik b{font:800 22px/1.1 var(--sans)}
.statistik span{font-size:14px;opacity:.9}
.grup{background:var(--surface);border:1px solid var(--line);border-radius:22px;padding:4px 14px 14px;margin-top:12px}
.grup h2{margin:12px 2px 8px}
.menu-ikon{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.menu-ikon a{display:flex;flex-direction:column;align-items:center;gap:8px;padding:8px 2px;min-height:88px;border-radius:14px;
  color:var(--ink);text-decoration:none;font-weight:700;font-size:14px;text-align:center;line-height:1.2}
.menu-ikon a:active{background:var(--tint)}
.lingkar{width:52px;height:52px;border-radius:16px;background:var(--tint);color:var(--crust);display:flex;align-items:center;justify-content:center}
.lingkar .ikon{width:26px;height:26px}
.batang{height:8px;border-radius:4px;background:var(--tint);margin-top:6px;overflow:hidden}
.batang span{display:block;height:100%;background:var(--crust);border-radius:4px}

@media (min-width:768px){
  :root{--rail:calc(104px + env(safe-area-inset-left))}
  .nav{top:0;right:auto;width:var(--rail);flex-direction:column;justify-content:flex-start;padding:calc(20px + env(safe-area-inset-top)) 0 env(safe-area-inset-bottom) env(safe-area-inset-left);border-top:0;border-right:1px solid var(--line)}
  .nav a,.nav button{flex:none;min-height:80px}
  .nav a[aria-current]::before{top:16%;bottom:16%;left:0;right:auto;width:3px;height:auto;border-radius:0 3px 3px 0}
  body:not(.tanpa-nav) .atas,body:not(.tanpa-nav) main{margin-left:calc(var(--rail) + max(0px,(100vw - var(--rail) - 760px)/2))}
  body:not(.tanpa-nav) .bar{left:var(--rail)}
  .bar{bottom:0;padding:12px 32px calc(12px + env(safe-area-inset-bottom))}
  main,body.ada-bar main{padding-bottom:calc(var(--bar-h) + 40px)}
  .atas,main{padding-left:32px;padding-right:32px}
  h1{font-size:36px}
  .ubin{grid-template-columns:repeat(3,1fr)}
  .kartu-kue{grid-template-columns:repeat(4,1fr)}
  .kepala .angka{font-size:54px}
  .menu-ikon{grid-template-columns:repeat(4,1fr)}
  .utama-blok .angka,.besar .angka{font-size:56px}
  .duo .nilai{font-size:32px}
  .bar .isi{max-width:520px;margin:0}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = """document.addEventListener('click',function(ev){var b=ev.target.closest('[data-langkah]');if(!b)return;
var i=b.parentNode.querySelector('input');var v=parseInt(i.value||'0',10)||0;v=Math.max(0,v+parseInt(b.dataset.langkah,10));
if(i.dataset.maks)v=Math.min(v,parseInt(i.dataset.maks,10));i.value=v;});
document.addEventListener('click',function(ev){var b=ev.target.closest('[data-preset]');if(!b)return;
var pilih=b.dataset.preset.split(',');b.form.querySelectorAll('input[name=akses]').forEach(function(c){c.checked=pilih.indexOf(c.value)>=0;});});
document.addEventListener('change',function(ev){var t=ev.target,box=t.closest('fieldset.tampilan');if(!box)return;
var p=box.querySelector('.pratinjau');
if(t.name==='color'&&p.classList.contains('inisial')){p.style.background=t.dataset.hex;return;}
if(!t.matches('[data-foto]')||!t.files[0])return;
var r=new FileReader();r.onload=function(){var img=new Image();img.onload=function(){
var W=640,H=480,c=document.createElement('canvas');c.width=W;c.height=H;var k=Math.max(W/img.width,H/img.height);
var w=img.width*k,h=img.height*k;c.getContext('2d').drawImage(img,(W-w)/2,(H-h)/2,w,h);
var d=c.toDataURL('image/jpeg',0.82);box.querySelector('input[name=foto_data]').value=d;
p.className='gbr pratinjau';p.style.background='';p.innerHTML='<img alt="" src="'+d+'">';};img.src=r.result;};
r.readAsDataURL(t.files[0]);});
document.querySelectorAll('.geser').forEach(function(g){g.scrollLeft=g.scrollWidth;});"""

IKON = {
    "beranda": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/><path d="M10 21v-6h4v6"/>',
    "produksi": '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    "sisa": '<path d="M3 7.5 12 3l9 4.5-9 4.5z"/><path d="M3 7.5V16.5L12 21l9-4.5V7.5"/><path d="M12 12v9"/>',
    "laporan": '<path d="M3 21h18"/><path d="M6 17v-5"/><path d="M11 17V7"/><path d="M16 17v-8"/><path d="M21 17V4"/>',
    "lainnya": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "kiri": '<path d="m15 18-6-6 6-6"/>',
    "kanan": '<path d="m9 18 6-6-6-6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "cek": '<path d="M20 6 9 17l-5-5"/>',
    "awas": '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17.5v.01"/>',
    "kue": '<path d="M4 20h16"/><path d="M5 20v-7a7 7 0 0 1 14 0v7"/><path d="M9 13v3M15 13v3M12 12v4"/>',
    "orang": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "jam": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "dompet": '<path d="M4 7h15a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a2 2 0 0 1 2-2h11v3"/><path d="M16 13.5h.01"/>',
    "kalender": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/><path d="M8 14h.01M12 14h.01M16 14h.01M8 17.5h.01M12 17.5h.01"/>',
    "keluar": '<path d="M15 4h4v16h-4"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h10"/>',
}


def ikon(nama):
    return f'<svg class="ikon" viewBox="0 0 24 24" aria-hidden="true">{IKON[nama]}</svg>'


def rp_besar(n):
    s = rupiah(n)
    tanda, angka_ = s.split("Rp ")
    return f"<small>{tanda}Rp</small>{angka_}"


def kabar(jenis, isi):
    ik = {"ok": "cek", "awas": "awas", "galat": "awas"}[jenis]
    role = "alert" if jenis == "galat" else "status"
    return f'<div class="kabar {jenis}" role="{role}">{ikon(ik)}<div>{isi}</div></div>'


def stepper(nama, nilai, label, maks=None, lebar=False, form=None, kelas=""):
    m = f' data-maks="{maks}"' if maks is not None else ""
    m += f' form="{form}" required' if form else ""
    return f"""<div class="stepper{' lebar' if lebar else ''} {kelas}">
      <button type="button" data-langkah="-1" aria-label="Kurangi {e(label)}">{ikon("minus")}</button>
      <input name="{nama}" value="{e(nilai)}" inputmode="numeric" pattern="[0-9]*" placeholder="0" aria-label="{e(label)}"{m}>
      <button type="button" data-langkah="1" aria-label="Tambah {e(label)}">{ikon("plus")}</button></div>"""


def form_tanggal(aksi, tgl):
    kemarin = (date.fromisoformat(tgl) - timedelta(days=1)).isoformat()
    besok = (date.fromisoformat(tgl) + timedelta(days=1)).isoformat()
    return f"""<form class="tgl" method="get" action="{aksi}">
      <a href="{aksi}?tanggal={kemarin}" aria-label="Hari sebelumnya">{ikon("kiri")}</a>
      <input type="date" name="tanggal" value="{e(tgl)}" aria-label="Pilih tanggal" onchange="this.form.submit()">
      <a href="{aksi}?tanggal={besok}" aria-label="Hari berikutnya">{ikon("kanan")}</a></form>"""


def bar_aksi(*tombol):
    return f'<div class="bar"><div class="isi">{"".join(tombol)}</div></div>'


# ---------- aplikasi ----------

# Syarat: None = bebas, "login" = cukup masuk, "admin" = khusus admin (kelola user),
# tuple = butuh salah satu hak akses yang dicentang. Admin selalu punya semua akses.
ROUTES = {
    ("GET", "/setup"): ("admin_pertama_form", None),
    ("POST", "/setup"): ("admin_pertama", None),
    ("GET", "/login"): ("login_form", None),
    ("POST", "/login"): ("login", None),
    ("POST", "/logout"): ("logout", "login"),
    ("GET", "/"): ("home", "login"),
    ("GET", "/lainnya"): ("lainnya", "login"),
    ("GET", "/produksi"): ("produksi", ("produksi",)),
    ("POST", "/produksi"): ("produksi_tambah", ("produksi",)),
    ("POST", "/produksi/:id/ubah"): ("produksi_ubah", ("produksi",)),
    ("POST", "/produksi/:id/hapus"): ("produksi_hapus", ("produksi",)),
    ("GET", "/sisa"): ("sisa", ("sisa",)),
    ("POST", "/sisa"): ("sisa_simpan", ("sisa",)),
    ("GET", "/laporan"): ("laporan", ("omzet", "untung")),
    ("GET", "/sisa-harian"): ("sisa_harian", ("sisa", "omzet", "untung")),
    ("GET", "/gaji"): ("gaji", ("gaji",)),
    ("POST", "/gaji/:id/bayar"): ("gaji_bayar", "admin"),
    ("GET", "/kue"): ("kue", ("kue",)),
    ("POST", "/kue"): ("kue_tambah", ("kue",)),
    ("POST", "/kue/:id/ubah"): ("kue_ubah", ("kue",)),
    ("GET", "/pengguna"): ("pengguna", "admin"),
    ("POST", "/pengguna"): ("pengguna_tambah", "admin"),
    ("POST", "/pengguna/:id/password"): ("pengguna_password", "admin"),
    ("POST", "/pengguna/:id/akses"): ("pengguna_akses", "admin"),
    ("GET", "/riwayat"): ("riwayat", "admin"),
}

class App(BaseHTTPRequestHandler):
    server_version = "TokoKue/1"

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    # --- kerangka ---

    def handle_request(self, method):
        url = urlparse(self.path)
        if url.path.startswith("/static/fonts/"):
            return self.font(url.path.rsplit("/", 1)[-1])
        if url.path.startswith("/foto/"):
            return self.foto(url.path.rsplit("/", 1)[-1])
        if url.path == "/manifest.webmanifest":
            return self.berkas(json.dumps(manifest(), ensure_ascii=False).encode(), "application/manifest+json", 3600)
        if url.path.startswith("/static/ikon/") or url.path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            nama = "apple-touch-icon-180.png" if url.path.startswith("/apple-touch-icon") else url.path.rsplit("/", 1)[-1]
            if nama not in IKON_FILE:
                return self.berkas(b"", "text/plain", 0, 404)
            with open(os.path.join(BASE, "static", "ikon", nama), "rb") as fh:
                return self.berkas(fh.read(), "image/png", 86400)
        self.q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.f, self.fl, self.akses = {}, {}, set()
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n > BODY_MAKS:
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                return self.end_headers()
            body = self.rfile.read(n).decode("utf-8", "replace")
            self.fl = parse_qs(body, keep_blank_values=True)
            self.f = {k: v[0] for k, v in self.fl.items()}
        parts = url.path.rstrip("/").split("/") or [""]
        self.id = None
        if len(parts) == 4 and parts[2].isdigit():
            self.id = int(parts[2])
            parts[2] = ":id"
        key = (method, "/".join(parts) or "/")
        self.db = connect()
        try:
            ada_user = self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            if not ada_user and key[1] != "/setup":
                return self.redirect("/setup")
            if ada_user and key[1] == "/setup":
                return self.redirect("/login")
            self.user = self.user_sekarang()
            if key not in ROUTES:
                raise Gagal("Halaman tidak ditemukan.", 404)
            nama, syarat = ROUTES[key]
            if syarat and not self.user:
                return self.redirect("/login")
            if self.user:
                self.akses = {r[0] for r in self.db.execute(
                    "SELECT perm FROM user_perms WHERE user_id = ?", (self.user["id"],))}
            if syarat == "admin" and not self.admin():
                raise Gagal("Halaman ini khusus admin.", 403)
            if isinstance(syarat, tuple) and not any(self.boleh(p) for p in syarat):
                raise Gagal("Kamu belum punya akses untuk ini. Minta admin mencentangnya di menu Pengguna.", 403)
            getattr(self, nama)()
        except Gagal as g:
            back = '<p><a href="javascript:history.back()">Kembali</a></p>'
            self.page("Ada masalah", kabar("galat", e(g)) + back, g.status)
        finally:
            self.db.close()

    def font(self, nama):
        if nama not in FONTS:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        with open(os.path.join(BASE, "static", "fonts", nama), "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def berkas(self, data, jenis, umur, status=200):
        self.send_response(status)
        self.send_header("Content-Type", jenis)
        self.send_header("Cache-Control", f"public, max-age={umur}" if umur else "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def foto(self, nama):
        path = os.path.join(foto_dir(), nama)
        if not re.fullmatch(r"kue-\d+-[0-9a-f]{12}\.jpg", nama) or not os.path.isfile(path):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")  # nama file berganti tiap foto baru
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def user_sekarang(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if "sesi" not in c:
            return None
        batas = (now() - timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        return self.db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.created > ?",
            (c["sesi"].value, batas)).fetchone()

    def send(self, status, body, headers=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # data keuangan selalu diambil ulang dari server
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc, pesan=None, headers=()):
        if pesan:
            loc += ("&" if "?" in loc else "?") + urlencode({"pesan": pesan})
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def page(self, judul, isi, status=200, alis="", bar="", h1=None):
        u = getattr(self, "user", None)
        menu = self.menu() if u else []
        pakai_nav = len(menu) >= 2
        kelas = [] if pakai_nav else ["tanpa-nav"]
        if bar:
            kelas.append("ada-bar")
        nav = ""
        if pakai_nav:
            path = urlparse(self.path).path
            path = {"/sisa-harian": "/sisa"}.get(path, path)
            hrefs = [h for h, _, _ in menu]
            bagian = path if path in hrefs else ("/lainnya" if "/lainnya" in hrefs else "")
            nav = '<nav class="nav" aria-label="Menu utama">' + "".join(
                f'<a href="{h}"{" aria-current=page" if bagian == h else ""}>{ikon(i)}{t}</a>'
                for h, t, i in menu) + "</nav>"
        atas = ""
        if u:
            ada_lainnya = any(h == "/lainnya" for h, _, _ in menu)
            keluar = "" if ada_lainnya else ('<form method="post" action="/logout"><button class="biasa">Keluar</button></form>')
            atas = f'<div class="atas"><a class="merek" href="/">{e(NAMA_TOKO)}</a><span class="siapa">{e(u["name"])}</span>{keluar}</div>'
        pesan = kabar("ok", e(self.q["pesan"])) if self.q.get("pesan") else ""
        alis = f'<p class="alis">{alis}</p>' if alis else ""
        self.send(status, f"""<!doctype html><html lang="id"><head>{kepala_html(judul)}
<link rel="preload" href="/static/fonts/karla.woff2" as="font" type="font/woff2" crossorigin>
<style>{CSS}</style></head><body class="{' '.join(kelas)}">{atas}
<main>{alis}<h1>{h1 or e(judul)}</h1>{pesan}{isi}</main>{bar}{nav}<script>{JS}</script></body></html>""")

    def admin(self):
        return bool(self.user) and self.user["role"] == "admin"

    def boleh(self, perm):
        return self.admin() or perm in self.akses

    def lihat_gaji(self):
        """Admin melihat halaman Gaji semua karyawan; karyawan hanya kalau dicentang DAN menerima gaji."""
        return self.admin() or (self.boleh("gaji") and bool(self.user["digaji"]))

    def menu(self):
        """Menu bawah sesuai hak akses. Lebih dari 5 item -> sisanya masuk 'Lainnya'."""
        utama = [("/", "Beranda", "beranda", True), ("/produksi", "Produksi", "produksi", self.boleh("produksi")),
                 ("/sisa", "Sisa", "sisa", self.boleh("sisa")),
                 ("/laporan", "Laporan", "laporan", self.boleh("omzet") or self.boleh("untung"))]
        items = [(h, t, i) for h, t, i, ok in utama if ok]
        tambahan = self.menu_lainnya()
        if self.admin() or len(tambahan) > 1:
            items.append(("/lainnya", "Lainnya", "lainnya"))
        elif tambahan:
            h, i, t, _ = tambahan[0]
            items.append((h, t.split(" ")[0], i))
        return items

    def menu_lainnya(self):
        m = []
        if self.lihat_gaji():
            m.append(("/gaji", "dompet", "Gaji", "Gaji berjalan, tandai sudah dibayar, riwayat" if self.admin()
                      else "Gaji berjalan dan riwayat pembayaranmu"))
        if self.boleh("kue"):
            m.append(("/kue", "kue", "Daftar kue dan harga", "Tambah kue, ubah harga"))
        if self.admin():
            m += [("/pengguna", "orang", "Pengguna dan akses", "Tambah karyawan, centang akses, persen gaji, password"),
                  ("/riwayat", "jam", "Riwayat perubahan", "Siapa mengubah apa dan kapan")]
        return m

    # --- setup & login ---

    def halaman_masuk(self, judul, isi, status=200):
        self.send(status, f"""<!doctype html><html lang="id"><head>{kepala_html(judul)}<style>{CSS}</style></head>
<body class="tanpa-nav"><div class="masuk"><span class="merek">{e(NAMA_TOKO)}</span>
<p class="kecil" style="margin:0 0 28px">Catatan harian produksi, sisa, dan penjualan</p>
<h1 style="font-size:24px">{e(judul)}</h1>{isi}</div></body></html>""")

    def admin_pertama_form(self):
        self.halaman_masuk("Buat akun admin pertama", """<form method="post">
          <p class="kecil">Halaman ini hanya muncul sekali, saat belum ada pengguna.</p>
          <label for="n">Nama</label><input id="n" name="name" required autocomplete="name">
          <label for="u">Username</label><input id="u" name="username" required autocapitalize="none" autocomplete="username">
          <label for="p">Password</label><input id="p" name="password" type="password" required minlength="6" autocomplete="new-password">
          <p class="bantu">Minimal 6 huruf.</p>
          <button class="tombol penuh" style="margin-top:24px">Buat admin</button></form>""")

    def admin_pertama(self):
        uid = self.buat_user("admin")
        return self.redirect("/login", f"Admin dibuat (id {uid}). Silakan masuk.")

    def buat_user(self, role):
        name = self.f.get("name", "").strip()
        username = self.f.get("username", "").strip().lower()
        pw = self.f.get("password", "")
        if not name or not username:
            raise Gagal("Nama dan username wajib diisi.")
        if len(pw) < 6:
            raise Gagal("Password minimal 6 huruf.")
        try:
            with self.db:
                cur = self.db.execute("INSERT INTO users(username, name, pw, role, gaji_mulai) VALUES(?,?,?,?,?)",
                                      (username, name, hash_pw(pw), role, today()))
                catat(self.db, getattr(self, "user", None) and self.user["id"], "tambah pengguna",
                      f"{username} ({role})")
        except sqlite3.IntegrityError:
            raise Gagal("Username sudah dipakai.")
        return cur.lastrowid

    def login_form(self):
        pesan = kabar("ok", e(self.q["pesan"])) if self.q.get("pesan") else ""
        self.halaman_masuk("Masuk", pesan + """<form method="post">
          <label for="u">Username</label><input id="u" name="username" required autocapitalize="none" autocomplete="username">
          <label for="p">Password</label><input id="p" name="password" type="password" required autocomplete="current-password">
          <button class="tombol penuh" style="margin-top:24px">Masuk</button>
          <p class="bantu" style="margin-top:16px">Lupa password? Minta admin buatkan password baru.</p></form>""")

    def login(self):
        u = self.db.execute("SELECT * FROM users WHERE username = ?",
                            (self.f.get("username", "").strip().lower(),)).fetchone()
        if not u or not cek_pw(self.f.get("password", ""), u["pw"]):
            raise Gagal("Username atau password salah.", 401)
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES(?,?,?)", (token, u["id"], stamp()))
        cookie = f"sesi={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}"
        self.redirect("/", headers=[("Set-Cookie", cookie)])

    def logout(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE token = ?", (c["sesi"].value,))
        self.redirect("/login", headers=[("Set-Cookie", "sesi=; Path=/; Max-Age=0")])

    # --- beranda (semua user; isi sesuai hak akses) ---

    def home(self):
        tgl = today()
        boleh_omzet, boleh_untung = self.boleh("omzet"), self.boleh("untung")
        if not self.admin() and self.akses == {"produksi"}:
            return self.redirect("/produksi")
        if not self.admin() and not self.akses:
            return self.page("Beranda", kabar("awas", "Akunmu belum punya akses apa pun. Minta admin mencentang akses untukmu."),
                             alis=tgl_indo(tgl), h1="Hari ini")
        lp = laporan_harian(self.db, tgl)
        t = lp["total"]
        n_batch = self.db.execute("SELECT COUNT(*) FROM production WHERE date = ?", (tgl,)).fetchone()[0]
        lihat_angka = self.admin() or boleh_omzet or boleh_untung or self.boleh("sisa")

        # 1. Header berwarna: penjualan hari ini + jumlah batch
        sisa_teks = "belum" if lp["sisa_belum"] else t["sisa"]
        isi = f'<section class="kepala" aria-label="Ringkasan hari ini"><div class="sapa">Halo, {e(self.user["name"])}</div>'
        if boleh_omzet:
            isi += f'<div class="label">Penjualan hari ini</div><div class="angka">{rp_besar(t["omzet"])}</div>'
        if lihat_angka:
            isi += f"""<div class="statistik"><div><b>{n_batch}</b><span>batch</span></div>
              <div><b>{t['terjual']}</b><span>terjual</span></div><div><b>{sisa_teks}</b><span>sisa</span></div></div>"""
        isi += "</section>"

        # 2. Menu berkelompok dalam kartu bersudut membulat
        grup = [("Produksi & sisa", [("/produksi", "produksi", "Catat produksi", self.boleh("produksi")),
                                     ("/sisa", "sisa", "Catat sisa", self.boleh("sisa")),
                                     ("/sisa-harian", "kalender", "Sisa per hari", lihat_angka)]),
                ("Kue & laporan", [("/kue", "kue", "Daftar kue", self.boleh("kue")),
                                   ("/laporan", "laporan", "Laporan", boleh_omzet or boleh_untung)]),
                ("Orang", [("/gaji", "dompet", "Gaji", self.lihat_gaji()),
                           ("/pengguna", "orang", "Pengguna", self.admin()),
                           ("/riwayat", "jam", "Riwayat", self.admin())])]
        for judul, menu in grup:
            tombol = [(h, i, t_) for h, i, t_, ok in menu if ok]
            if tombol:
                isi += f'<section class="grup"><h2>{judul}</h2><div class="menu-ikon">' + "".join(
                    f'<a href="{h}"><span class="lingkar">{ikon(i)}</span>{t_}</a>' for h, i, t_ in tombol) + "</div></section>"

        if self.lihat_gaji() and not self.admin():
            g = gaji_info(self.db, self.user)
            isi += f"""<div class="duo"><div><div class="label">Gaji berjalan</div>
              <div class="nilai">{rupiah(g['jumlah'])}</div></div>
              <div><div class="label">Sejak</div><div class="nilai" style="font-size:20px">{tgl_indo(g['mulai']) if not g['sudah_lunas'] else 'baru dibayar'}</div></div></div>"""

        # 3. Laporan harian singkat + kue terlaris
        if lihat_angka:
            isi += "<h2>Laporan hari ini</h2>"
            if boleh_untung:
                isi += f"""<div class="duo" style="border-top:1px solid var(--line)">
                  <div><div class="label">Untung</div><div class="nilai untung">{rupiah(t['untung'])}</div></div>
                  <div><div class="label">Rugi dari sisa</div><div class="nilai rugi">{rupiah(t['kerugian'])}</div></div></div>"""
            if not lp["items"]:
                isi += kabar("awas", "Belum ada produksi hari ini.")
            elif lp["sisa_belum"]:
                isi += kabar("awas", f'Sisa belum diisi untuk {len(lp["sisa_belum"])} kue. Sampai diisi, semua dianggap terjual.')
            else:
                isi += kabar("ok", "Sisa hari ini sudah diisi.")
            if lp["items"]:
                kue = {r["name"]: r for r in self.db.execute("SELECT id, name, color, photo FROM cakes")}
                laris = sorted(lp["items"], key=lambda i: (-i["terjual"], i["nama"]))[:5]
                puncak = max(laris[0]["terjual"], 1)
                isi += '<h2>Kue terlaris hari ini</h2><ul class="daftar laris">'
                for i in laris:
                    isi += f"""<li class="baris">{gambar_kue(kue[i['nama']], "gbr mini")}
                      <div style="flex:1;min-width:0"><div class="nama">{e(i['nama'])}</div>
                      <div class="batang" aria-hidden="true"><span style="width:{round(100 * i['terjual'] / puncak)}%"></span></div></div>
                      <div class="kanan"><div class="qty">{i['terjual']}</div><div class="kecil">terjual</div></div></li>"""
                isi += "</ul>"
        if boleh_omzet or boleh_untung:
            isi += f'<p style="margin-top:20px"><a href="/laporan?tanggal={tgl}">Laporan harian lengkap</a></p>'
        tombol = []
        if self.boleh("produksi"):
            tombol.append('<a class="tombol" href="/produksi">Catat produksi</a>')
        if self.boleh("sisa"):
            tombol.append(f'<a class="tombol{" garis" if tombol else ""}" href="/sisa">Catat sisa tutup</a>')
        bar = bar_aksi(*tombol) if tombol else ""
        self.page("Beranda", isi, alis=tgl_indo(tgl), bar=bar, h1="Hari ini")

    def lainnya(self):
        menu = self.menu_lainnya()
        isi = '<ul class="daftar">' + "".join(
            f"""<li><a class="baris" href="{h}" style="color:inherit;text-decoration:none;min-height:48px">{ikon(i)}
              <span><span class="nama">{t}</span><span class="info" style="margin:2px 0 0">{d}</span></span>
              <span class="kanan" style="color:var(--ink-2)">{ikon("kanan")}</span></a></li>""" for h, i, t, d in menu)
        isi += f"""<li><form method="post" action="/logout"><button class="baris biasa" style="width:100%;color:var(--danger);font-size:17px;min-height:48px">
              {ikon("keluar")}Keluar ({e(self.user['name'])})</button></form></li></ul>"""
        self.page("Lainnya", isi)

    # --- produksi ---

    def boleh_ubah(self, row):
        return self.admin() or (row["user_id"] == self.user["id"] and row["date"] == today())

    def produksi(self):
        tgl = tanggal(self.q["tanggal"]) if self.admin() and self.q.get("tanggal") else today()
        kue = self.db.execute("""SELECT c.*, (SELECT SUM(qty) FROM production p WHERE p.cake_id = c.id AND p.date = ?) hari_ini
            FROM cakes c WHERE active = 1 ORDER BY name""", (tgl,)).fetchall()
        isi = form_tanggal("/produksi", tgl) if self.admin() else ""
        bar = ""
        if kue:
            kartu = "".join(
                f"""<input type="radio" name="cake_id" id="k{k['id']}" value="{k['id']}" required>
                <label for="k{k['id']}">{gambar_kue(k, "gbr foto")}<span class="cek">{ikon("cek")}</span>
                <span class="nama-kue">{e(k['name'])}</span><span class="sub">hari ini {k['hari_ini'] or 0}</span></label>"""
                for k in kue)
            isi += f"""<form id="f-produksi" method="post" action="/produksi">
              <input type="hidden" name="tanggal" value="{tgl}">
              <fieldset style="border:0;padding:0;margin:0"><legend class="lbl" style="margin-top:4px">Pilih kue, isi jumlah di bawah, lalu simpan</legend>
              <div class="kartu-kue">{kartu}</div></fieldset>
              <p class="bantu">Bikin lagi nanti? Simpan sebagai batch baru, otomatis dijumlahkan.</p></form>"""
            bar = bar_aksi(stepper("qty", "", "Jumlah dibuat di batch ini", form="f-produksi", kelas="di-bar"),
                           '<button class="tombol" form="f-produksi">Simpan batch</button>')
        else:
            isi += kabar("awas", "Belum ada kue. Admin perlu menambah kue dulu di menu Lainnya, Daftar kue.")
        rows = self.db.execute("""SELECT p.*, c.name kue, c.name, c.color, c.photo, c.id cid, u.name oleh FROM production p
            JOIN cakes c ON c.id = p.cake_id JOIN users u ON u.id = p.user_id
            WHERE p.date = ? ORDER BY p.id DESC""", (tgl,)).fetchall()
        if rows:
            isi += '<h2>Batch tercatat</h2><ul class="daftar">'
            for r in rows:
                ubah = ""
                if self.boleh_ubah(r):
                    ubah = f"""<details><summary><span class="biasa">Koreksi</span></summary><div class="panel">
                      <form method="post" action="/produksi/{r['id']}/ubah">
                      <label for="q{r['id']}">Jumlah yang benar</label>
                      <div class="baris"><input id="q{r['id']}" name="qty" value="{r['qty']}" inputmode="numeric" style="max-width:140px">
                      <button class="tombol sedang">Simpan</button></div></form>
                      <form method="post" action="/produksi/{r['id']}/hapus" onsubmit="return confirm('Hapus batch ini?')">
                      <button class="teks-merah">Hapus batch ini</button></form></div></details>"""
                k = {"id": r["cid"], "name": r["kue"], "color": r["color"], "photo": r["photo"]}
                isi += f"""<li><div class="baris">{gambar_kue(k, "gbr mini")}<span style="min-width:0"><span class="nama">{e(r['kue'])}</span>
                  <span class="info" style="margin:2px 0 0"><span>{r['created'][11:16]}</span><span>{e(r['oleh'])}</span></span></span>
                  <span class="kanan qty">{r['qty']}</span></div>{ubah}</li>"""
            isi += "</ul>"
        alis = tgl_indo(tgl) if self.admin() else f"Halo, {e(self.user['name'])} · {tgl_indo(tgl)}"
        self.page("Input produksi", isi, alis=alis, bar=bar)

    def produksi_tambah(self):
        tgl = tanggal(self.f.get("tanggal")) if self.admin() else today()
        qty = angka(self.f.get("qty"), "Jumlah", 1)
        k = self.db.execute("SELECT * FROM cakes WHERE id = ? AND active = 1",
                            (angka(self.f.get("cake_id"), "Kue"),)).fetchone()
        if not k:
            raise Gagal("Kue tidak ditemukan.")
        with self.db:
            self.db.execute("""INSERT INTO production(date, cake_id, qty, price_sell, price_cost, user_id, created)
                VALUES(?,?,?,?,?,?,?)""", (tgl, k["id"], qty, k["price_sell"], k["price_cost"], self.user["id"], stamp()))
            catat(self.db, self.user["id"], "tambah produksi", f"{tgl} {k['name']} +{qty}")
        self.redirect(f"/produksi?tanggal={tgl}", f"Tersimpan: {k['name']} {qty} pcs.")

    def ubah_batch(self, hapus):
        r = self.db.execute("SELECT p.*, c.name kue FROM production p JOIN cakes c ON c.id = p.cake_id WHERE p.id = ?",
                            (self.id,)).fetchone()
        if not r:
            raise Gagal("Data tidak ditemukan.", 404)
        if not self.boleh_ubah(r):
            raise Gagal("Batch hanya bisa dikoreksi pembuatnya di hari yang sama. Setelah itu minta admin.", 403)
        baru = 0 if hapus else angka(self.f.get("qty"), "Jumlah", 1)
        total = self.db.execute("SELECT SUM(qty) FROM production WHERE date = ? AND cake_id = ?",
                                (r["date"], r["cake_id"])).fetchone()[0]
        sisa = self.db.execute("SELECT qty FROM leftover WHERE date = ? AND cake_id = ?",
                               (r["date"], r["cake_id"])).fetchone()
        if sisa and total - r["qty"] + baru < sisa["qty"]:
            raise Gagal(f"Total dibuat jadi lebih kecil dari sisa yang sudah dicatat ({sisa['qty']}). Ubah sisa dulu.")
        with self.db:
            if hapus:
                self.db.execute("DELETE FROM production WHERE id = ?", (r["id"],))
            else:
                self.db.execute("UPDATE production SET qty = ? WHERE id = ?", (baru, r["id"]))
            catat(self.db, self.user["id"], "hapus produksi" if hapus else "ubah produksi",
                  f"{r['date']} {r['kue']} batch #{r['id']}: {r['qty']} -> {baru}")
        self.redirect(f"/produksi?tanggal={r['date']}", "Batch dihapus." if hapus else "Batch diubah.")

    def produksi_ubah(self):
        self.ubah_batch(hapus=False)

    def produksi_hapus(self):
        self.ubah_batch(hapus=True)

    # --- sisa (admin) ---

    def sisa(self):
        # Non-admin dengan akses "input sisa" hanya untuk hari ini; tanggal lain khusus admin.
        tgl = tanggal(self.q["tanggal"]) if self.admin() and self.q.get("tanggal") else today()
        rows = self.db.execute("""SELECT c.id, c.name, c.color, c.photo, SUM(p.qty) dibuat,
              (SELECT qty FROM leftover l WHERE l.cake_id = c.id AND l.date = :d) sisa
            FROM production p JOIN cakes c ON c.id = p.cake_id WHERE p.date = :d
            GROUP BY c.id ORDER BY c.name""", {"d": tgl}).fetchall()
        batch = {}
        for b in self.db.execute("SELECT cake_id, qty, created FROM production WHERE date = ? ORDER BY id", (tgl,)):
            batch.setdefault(b["cake_id"], []).append(f'{b["qty"]} pcs ({b["created"][11:16]})')
        isi = form_tanggal("/sisa", tgl) if self.admin() else ""
        bar = ""
        if not rows:
            isi += kabar("awas", "Belum ada produksi di tanggal ini.")
        else:
            isi += '<p class="kecil" style="margin:8px 0 12px">Isi jumlah kue yang tidak terjual. Kosong dihitung 0. Sisa dibagikan, jadi dicatat sebagai kerugian.</p>'
            isi += f'<form id="f-sisa" method="post" action="/sisa"><input type="hidden" name="tanggal" value="{tgl}"><ul class="daftar">'
            for r in rows:
                val = "" if r["sisa"] is None else r["sisa"]
                isi += f"""<li class="sisa-baris"><div class="baris">{gambar_kue(r, "gbr mini")}
                  <div style="min-width:0"><div class="nama">{e(r['name'])}</div>
                  <div class="info" style="margin-top:2px">dibuat {r['dibuat']}</div></div>
                  <div class="kanan">{stepper(f"sisa_{r['id']}", val, f"Sisa {r['name']}", maks=r['dibuat'])}</div></div>
                  <div class="batch-info">Batch: {", ".join(e(x) for x in batch.get(r["id"], []))}</div></li>"""
            isi += "</ul></form>"
            if self.boleh("sisa") or self.boleh("omzet") or self.boleh("untung"):
                isi += '<p style="margin-top:16px"><a href="/sisa-harian">Lihat sisa per kue per hari</a></p>'
            bar = bar_aksi('<button class="tombol" form="f-sisa">Simpan sisa</button>')
        self.page("Sisa saat tutup", isi, bar=bar, alis=tgl_indo(tgl))

    def sisa_simpan(self):
        tgl = tanggal(self.f.get("tanggal"))
        if not self.admin() and tgl != today():
            raise Gagal("Sisa tanggal lain hanya bisa diubah admin.", 403)
        rows = self.db.execute("""SELECT c.id, c.name, SUM(p.qty) dibuat,
              (SELECT qty FROM leftover l WHERE l.cake_id = c.id AND l.date = :d) lama
            FROM production p JOIN cakes c ON c.id = p.cake_id WHERE p.date = :d GROUP BY c.id""",
                               {"d": tgl}).fetchall()
        ubah = []
        for r in rows:
            n = angka(self.f.get(f"sisa_{r['id']}") or "0", f"Sisa {r['name']}")
            if n > r["dibuat"]:
                raise Gagal(f"Sisa {r['name']} ({n}) lebih besar dari jumlah dibuat ({r['dibuat']}).")
            if n != r["lama"]:
                ubah.append((r, n))
        with self.db:
            for r, n in ubah:
                self.db.execute("""INSERT INTO leftover(date, cake_id, qty, user_id, updated) VALUES(?,?,?,?,?)
                    ON CONFLICT(date, cake_id) DO UPDATE SET qty = excluded.qty, user_id = excluded.user_id,
                    updated = excluded.updated""", (tgl, r["id"], n, self.user["id"], stamp()))
                catat(self.db, self.user["id"], "ubah sisa", f"{tgl} {r['name']}: {r['lama']} -> {n}")
        tujuan = f"/laporan?tanggal={tgl}" if (self.boleh("omzet") or self.boleh("untung")) else "/"
        self.redirect(tujuan, "Sisa tersimpan.")

    # --- laporan (omzet / untung) ---

    def rentang_laporan(self):
        """Baca filter: hari (tanggal), 7 hari, bulan ini, atau rentang (dari-sampai)."""
        hari_ini = today()
        periode = self.q.get("periode") or "hari"
        if periode == "7":
            return periode, (date.fromisoformat(hari_ini) - timedelta(days=6)).isoformat(), hari_ini
        if periode == "bulan":
            return periode, hari_ini[:8] + "01", hari_ini
        if periode == "rentang":
            dari = tanggal(self.q.get("dari") or hari_ini)
            sampai = tanggal(self.q.get("sampai") or hari_ini)
            if dari > sampai:
                raise Gagal("Tanggal awal harus sebelum tanggal akhir.")
            if (date.fromisoformat(sampai) - date.fromisoformat(dari)).days > 366:
                raise Gagal("Rentang maksimal 1 tahun.")
            return periode, dari, sampai
        tgl = tanggal(self.q.get("tanggal") or hari_ini)
        return "hari", tgl, tgl

    def laporan(self):
        periode, dari, sampai = self.rentang_laporan()
        lp = laporan_rentang(self.db, dari, sampai)
        t = lp["total"]
        boleh_omzet, boleh_untung = self.boleh("omzet"), self.boleh("untung")
        pilihan = [("hari", "Hari ini", f"/laporan?tanggal={today()}"), ("7", "7 hari", "/laporan?periode=7"),
                   ("bulan", "Bulan ini", "/laporan?periode=bulan"),
                   ("rentang", "Rentang", f"/laporan?periode=rentang&dari={dari}&sampai={sampai}")]
        isi = '<nav class="pilih" aria-label="Pilih periode">' + "".join(
            f'<a href="{h}"{" aria-current=true" if periode == p else ""}>{t_}</a>' for p, t_, h in pilihan) + "</nav>"
        if periode == "hari":
            isi += form_tanggal("/laporan", dari)
        elif periode == "rentang":
            isi += f"""<form class="rentang" method="get" action="/laporan"><input type="hidden" name="periode" value="rentang">
              <div><label for="dari">Dari</label><input id="dari" type="date" name="dari" value="{dari}"></div>
              <div><label for="sampai">Sampai</label><input id="sampai" type="date" name="sampai" value="{sampai}"></div>
              <button class="tombol sedang">Tampilkan</button></form>"""
        judul = "Laporan harian" if periode == "hari" else "Laporan"
        alis = tgl_indo(dari) if dari == sampai else f"{tgl_indo(dari)} – {tgl_indo(sampai)}"
        if not lp["items"]:
            return self.page(judul, isi + kabar("awas", "Belum ada produksi di periode ini."), alis=alis)
        if lp["sisa_belum"]:
            daftar = ", ".join(f"{n}" if dari == sampai else f"{n} ({tgl_indo(d)[-11:]})" for d, n in lp["sisa_belum"][:6])
            lebih = f" dan {len(lp['sisa_belum']) - 6} lagi" if len(lp["sisa_belum"]) > 6 else ""
            link = f' <a href="/sisa?tanggal={lp["sisa_belum"][0][0]}">Isi sisa</a>' if self.boleh("sisa") else ""
            isi += kabar("awas", f"Sisa belum diisi untuk: {e(daftar)}{lebih}. Sampai diisi, kue itu dianggap terjual semua.{link}")
        if boleh_omzet:
            isi += f"""<section class="besar" style="margin-top:12px"><div class="label">Omzet</div>
              <div class="angka">{rp_besar(t['omzet'])}</div></section>"""
        if boleh_untung:
            isi += f"""<div class="duo">
              <div><div class="label">Untung*</div><div class="nilai untung">{rupiah(t['untung'])}</div></div>
              <div><div class="label">Rugi dari sisa</div><div class="nilai rugi">{rupiah(t['kerugian'])}</div></div></div>"""
        laku = f'{e(lp["laku"]["nama"])} <span class="kecil" style="display:block">{lp["laku"]["terjual"]} terjual</span>' if lp["laku"] else "-"
        ss = lp["sering_sisa"]
        if ss and dari == sampai:
            sering = f'{e(ss["nama"])} <span class="kecil" style="display:block">sisa {ss["sisa"]}</span>'
        elif ss:
            sering = f'{e(ss["nama"])} <span class="kecil" style="display:block">{ss["hari_sisa"]} hari ada sisa, total {ss["sisa"]}</span>'
        else:
            sering = "-"
        isi += f"""<div class="duo">
            <div><div class="label">Terjual</div><div class="nilai">{t['terjual']}</div></div>
            <div><div class="label">Sisa</div><div class="nilai">{t['sisa']}</div></div></div>
          <ul class="daftar" style="border-top:0">"""
        if boleh_untung:
            isi += f"""<li class="baris"><span class="kecil" style="min-width:150px">Untung setelah sisa*</span><span class="kanan" style="font-weight:800">{rupiah(t['untung_bersih'])}</span></li>"""
        isi += f"""<li class="baris"><span class="kecil" style="min-width:150px">Paling laku</span><span class="kanan" style="font-weight:700">{laku}</span></li>
            <li class="baris"><span class="kecil" style="min-width:150px">{"Paling banyak sisa" if dari == sampai else "Paling sering sisa"}</span><span class="kanan" style="font-weight:700">{sering}</span></li></ul>"""
        if dari != sampai:
            isi += f'<p class="bantu">{len(lp["hari"])} hari ada produksi di periode ini.</p>'
        if boleh_untung:
            isi += """<p class="bantu">*Untung = omzet − (terjual × harga modal). Untung setelah sisa = untung − rugi dari sisa.
              Rumus untung masih menunggu konfirmasi pemilik. Gaji belum dikurangkan.</p>"""
        isi += '<h2>Per kue</h2><ul class="daftar">'
        for i in lp["items"]:
            kanan = f'<span class="kanan" style="font-weight:800">{rupiah(i["omzet"])}</span>' if boleh_omzet else \
                f'<span class="kanan qty">{i["terjual"]}</span>'
            uang = ""
            if boleh_untung:
                uang = (f'<div class="info"><span class="untung">untung {rupiah(i["untung"])}</span>'
                        f'<span style="color:var(--terra-text)">rugi sisa {rupiah(i["kerugian"])}</span></div>')
            isi += f"""<li><div class="baris"><span class="nama">{e(i['nama'])}</span>{kanan}</div>
              <div class="info"><span>dibuat {i['dibuat']}</span><span>sisa {i['sisa']}</span><span>terjual {i['terjual']}</span></div>{uang}</li>"""
        total_kanan = f'<span class="kanan" style="font-weight:800">{rupiah(t["omzet"])}</span>' if boleh_omzet else ""
        isi += f"""<li><div class="baris"><span class="nama">Total</span>{total_kanan}</div>
          <div class="info"><span>dibuat {t['dibuat']}</span><span>sisa {t['sisa']}</span><span>terjual {t['terjual']}</span></div></li></ul>"""
        self.page(judul, isi, alis=alis)

    # --- sisa per kue per hari + per batch ---

    def sisa_harian(self):
        if not self.q.get("periode"):
            self.q["periode"] = "7"
        periode, dari, sampai = self.rentang_laporan()
        if (date.fromisoformat(sampai) - date.fromisoformat(dari)).days > 61:
            raise Gagal("Untuk halaman ini, rentang maksimal 2 bulan.")
        data = sisa_per_hari(self.db, dari, sampai)
        pilihan = [("hari", "Hari ini", f"/sisa-harian?periode=hari&tanggal={today()}"), ("7", "7 hari", "/sisa-harian?periode=7"),
                   ("bulan", "Bulan ini", "/sisa-harian?periode=bulan"),
                   ("rentang", "Rentang", f"/sisa-harian?periode=rentang&dari={dari}&sampai={sampai}")]
        isi = '<nav class="pilih" aria-label="Pilih periode">' + "".join(
            f'<a href="{h}"{" aria-current=true" if periode == p else ""}>{t_}</a>' for p, t_, h in pilihan) + "</nav>"
        if periode == "rentang":
            isi += f"""<form class="rentang" method="get" action="/sisa-harian"><input type="hidden" name="periode" value="rentang">
              <div><label for="dari">Dari</label><input id="dari" type="date" name="dari" value="{dari}"></div>
              <div><label for="sampai">Sampai</label><input id="sampai" type="date" name="sampai" value="{sampai}"></div>
              <button class="tombol sedang">Tampilkan</button></form>"""
        alis = tgl_indo(dari) if dari == sampai else f"{tgl_indo(dari)} – {tgl_indo(sampai)}"
        if not data["baris"]:
            return self.page("Sisa per hari", isi + kabar("awas", "Belum ada produksi di periode ini."), alis=alis)
        nama_hari = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"]
        kepala = "".join(f'<th scope="col"{" class=hariini" if d == today() else ""}>{nama_hari[date.fromisoformat(d).weekday()]}<br>{int(d[8:])}</th>'
                         for d in data["hari"])
        isi += f"""<h2>Sisa per kue per hari</h2>
          <p class="bantu" style="margin-top:0">Angka = sisa saat tutup. <span class="cth habis">0</span> habis terjual,
          <span class="cth ada">3</span> ada sisa, <span class="cth">?</span> belum diisi, – tidak dibuat.</p>
          <div class="geser"><table class="grid-sisa"><thead><tr><th scope="col" class="kolom-kue">Kue</th>{kepala}
          </tr></thead><tbody>"""
        for b in data["baris"]:
            sel = ""
            for d in data["hari"]:
                v = b["sel"][d]
                if v is None:
                    sel += '<td class="kosong">–</td>'
                elif v == "?":
                    sel += '<td class="belum">?</td>'
                else:
                    sel += f'<td class="{"habis" if v == 0 else "ada"}">{v}</td>'
            isi += f"""<tr><th scope="row" class="kolom-kue"><span class="baris">{gambar_kue(b['kue'], "gbr titik")}{e(b['kue']['name'])}</span></th>
              {sel}</tr>"""
        isi += "</tbody></table></div>"
        sering = sorted((b for b in data["baris"] if b["hari_sisa"]), key=lambda b: (-b["hari_sisa"], -b["total_sisa"]))[:3]
        habis = sorted((b for b in data["baris"] if b["hari_habis"]), key=lambda b: -b["hari_habis"])[:3]
        if sering or habis:
            isi += '<ul class="daftar" style="margin-top:16px">'
            if sering:
                isi += ('<li><div class="kecil">Rutin sisa</div><div style="font-weight:700">'
                        + ", ".join(f'{e(b["kue"]["name"])} ({b["hari_sisa"]} hari)' for b in sering) + "</div></li>")
            if habis:
                isi += ('<li><div class="kecil">Sering habis</div><div style="font-weight:700">'
                        + ", ".join(f'{e(b["kue"]["name"])} ({b["hari_habis"]} hari)' for b in habis) + "</div></li>")
            isi += "</ul>"

        # per batch, dikelompokkan per tanggal buat (paling baru dulu, maksimal 7 tanggal)
        boleh_untung = self.boleh("untung")
        tanggal_batch = sorted(data["batch"], reverse=True)
        isi += "<h2>Per batch</h2>"
        for d in tanggal_batch[:7]:
            rugi = {}
            if boleh_untung:
                rugi = {i["nama"]: i["kerugian"] for i in laporan_harian(self.db, d)["items"]}
            isi += f'<h3 class="tgl-batch">Dibuat {tgl_indo(d)}</h3><ul class="daftar">'
            for cid, bs in sorted(data["batch"][d].items(), key=lambda kv: data["kue"][kv[0]]["name"]):
                k = data["kue"][cid]
                sisa = data["sisa"].get((d, cid))
                teks_sisa = "sisa belum diisi" if sisa is None else ("habis" if sisa == 0 else f"sisa {sisa}")
                uang = f' · rugi {rupiah(rugi[k["name"]])}' if boleh_untung and sisa else ""
                daftar_batch = ", ".join(f'{b["qty"]} pcs (jam {b["created"][11:16]})' for b in bs)
                isi += f"""<li class="baris">{gambar_kue(k, "gbr mini")}<div style="flex:1;min-width:0">
                  <div class="nama">{e(k['name'])}</div><div class="info" style="margin-top:2px">{daftar_batch}</div>
                  <div class="info{' rugi-teks' if sisa else ''}" style="margin-top:2px;font-weight:700">{teks_sisa}{uang}</div></div>
                  <div class="kanan"><div class="qty">{data['dibuat'][(d, cid)]}</div><div class="kecil">dibuat</div></div></li>"""
            isi += "</ul>"
        if len(tanggal_batch) > 7:
            isi += f'<p class="bantu">Menampilkan 7 tanggal terbaru dari {len(tanggal_batch)} tanggal di periode ini.</p>'
        self.page("Sisa per hari", isi, alis=alis)

    # --- gaji ---

    def gaji(self):
        tidak_digaji = []
        if self.admin():
            semua = self.db.execute("SELECT * FROM users WHERE role = 'karyawan' ORDER BY name").fetchall()
            orang = [u for u in semua if u["digaji"]]
            tidak_digaji = [u for u in semua if not u["digaji"]]
        elif not self.user["digaji"]:
            return self.page("Gaji", kabar("awas", "Akunmu tidak terdaftar menerima gaji, jadi tidak ada angka gaji di sini. "
                                                     "Tanyakan ke admin kalau ini keliru."), alis="Gaji berjalan")
        else:
            orang = [self.user]
        if not orang and not tidak_digaji:
            return self.page("Gaji", kabar("awas", "Belum ada karyawan. Tambah di menu Pengguna dan akses."))
        isi = "" if orang else kabar("awas", "Belum ada karyawan yang menerima gaji. Atur di menu Pengguna dan akses.")
        tanpa_persen = [u for u in orang if u["pct"] == 0 and self.admin()]
        for u in orang:
            if u in tanpa_persen:
                continue
            g = gaji_info(self.db, u)
            periode = "Sudah dibayar sampai hari ini." if g["sudah_lunas"] else \
                f"{tgl_indo(g['mulai'])} – {tgl_indo(g['sampai'])}"
            isi += f"""<section class="gaji-blok"><div class="baris"><span class="nama">{e(u['name'])}</span>
                <span class="kanan kecil">{g['pct']}% dari omzet</span></div>
              <div class="besar"><div class="angka">{rp_besar(g['jumlah'])}</div></div>
              <div class="info"><span>{periode}</span><span>omzet {rupiah(g['omzet'])}</span></div>"""
            if g["sisa_belum"]:
                isi += kabar("awas", f"Sisa belum diisi di {len(g['sisa_belum'])} catatan. Gaji bisa turun setelah sisa diisi.")
            if self.admin() and not g["sudah_lunas"]:
                isi += f"""<form method="post" action="/gaji/{u['id']}/bayar"
                  onsubmit="return confirm('Tandai gaji {e(u['name'])} {rupiah(g['jumlah'])} sudah dibayar? Hitungan mulai lagi dari nol besok.')">
                  <input type="hidden" name="jumlah" value="{g['jumlah']}">
                  <button class="tombol penuh" style="margin-top:12px">Tandai sudah dibayar</button></form>
                  <p class="bantu">Tekan setelah sisa hari ini diisi. Penjualan sampai hari ini masuk pembayaran ini.</p>"""
            riwayat = self.db.execute("""SELECT * FROM wage_payments WHERE user_id = ? ORDER BY id DESC LIMIT 24""",
                                      (u["id"],)).fetchall()
            if riwayat:
                isi += '<h2>Riwayat pembayaran</h2><ul class="daftar">' + "".join(
                    f"""<li><div class="baris"><span class="nama" style="font-size:16px">{tgl_indo(r['period_start'])[-11:]} – {tgl_indo(r['period_end'])[-11:]}</span>
                      <span class="kanan" style="font-weight:800">{rupiah(r['amount'])}</span></div>
                      <div class="info"><span>dibayar {tgl_indo(r['paid_at'][:10])[-11:]}</span><span>{r['pct']}% × {rupiah(r['omzet'])}</span></div></li>"""
                    for r in riwayat) + "</ul>"
            isi += "</section>"
        if tanpa_persen:
            isi += '<h2>Tanpa gaji persen (0%)</h2><ul class="daftar">' + "".join(
                f'<li class="baris"><span class="nama">{e(u["name"])}</span><a class="kanan" href="/pengguna">Atur persen</a></li>'
                for u in tanpa_persen) + "</ul>"
        if tidak_digaji:
            isi += '<h2>Tidak menerima gaji</h2><ul class="daftar">'
            for u in tidak_digaji:
                lama = self.db.execute("SELECT * FROM wage_payments WHERE user_id = ? ORDER BY id DESC", (u["id"],)).fetchall()
                riwayat = ""
                if lama:
                    riwayat = '<details><summary><span class="biasa">Riwayat pembayaran lama</span></summary><ul class="daftar">' + "".join(
                        f"""<li><div class="baris"><span>{tgl_indo(r['period_start'])[-11:]} – {tgl_indo(r['period_end'])[-11:]}</span>
                          <span class="kanan" style="font-weight:800">{rupiah(r['amount'])}</span></div></li>""" for r in lama) + "</ul></details>"
                isi += f"""<li><div class="baris"><span class="nama">{e(u['name'])}</span>
                  <a class="kanan" href="/pengguna">Atur</a></div>{riwayat}</li>"""
            isi += "</ul>"
        isi += '<p class="bantu">Gaji = persen × harga jual kue yang terjual (harga saat dicatat). Belum dikurangkan dari untung.</p>'
        self.page("Gaji", isi, alis="Gaji berjalan")

    def gaji_bayar(self):
        u = self.db.execute("SELECT * FROM users WHERE id = ? AND role = 'karyawan'", (self.id,)).fetchone()
        if not u:
            raise Gagal("Karyawan tidak ditemukan.", 404)
        if not u["digaji"]:
            raise Gagal(f"{u['name']} tidak terdaftar menerima gaji.")
        g = gaji_info(self.db, u)
        if g["sudah_lunas"]:
            raise Gagal("Gaji sudah ditandai dibayar sampai hari ini.")
        if angka(self.f.get("jumlah"), "Jumlah") != g["jumlah"]:
            raise Gagal("Angka gaji berubah sejak halaman dibuka. Muat ulang halaman Gaji lalu coba lagi.", 409)
        with self.db:
            self.db.execute("""INSERT INTO wage_payments(user_id, period_start, period_end, omzet, pct, amount, paid_at, paid_by)
                VALUES(?,?,?,?,?,?,?,?)""", (u["id"], g["mulai"], g["sampai"], g["omzet"], g["pct"], g["jumlah"], stamp(),
                                             self.user["id"]))
            catat(self.db, self.user["id"], "gaji dibayar",
                  f"{u['username']} {g['mulai']}..{g['sampai']}: {g['pct']}% x {g['omzet']} = {g['jumlah']}")
        self.redirect("/gaji", f"Gaji {u['name']} {rupiah(g['jumlah'])} ditandai sudah dibayar.")

    # --- kue ---

    def isian_tampilan(self, k, pre):
        """Pilihan foto dan warna kartu kue. Foto diperkecil di browser (JS) lalu dikirim sebagai teks base64."""
        sekarang = (k["color"] or "") if k else ""
        warna = "".join(
            f"""<label class="swatch" title="{n}"><input type="radio" name="color" value="{n}" data-hex="{h}"{' checked' if n == sekarang else ''}>
              <span style="background:{h}"></span><small>{n}</small></label>""" for n, h in PALET.items())
        contoh = gambar_kue(k, "gbr pratinjau") if k else '<span class="gbr pratinjau inisial" style="background:#7A4A2A">?</span>'
        hapus = (f"""<label class="centang" style="border:0"><input type="checkbox" name="hapus_foto" value="1">
                   <span>Hapus foto, pakai warna saja</span></label>""" if k and k["photo"] else "")
        return f"""<fieldset class="tampilan"><legend class="lbl">Tampilan kartu</legend>
          <div class="baris" style="align-items:flex-start">{contoh}
          <div style="flex:1;min-width:0"><label class="tombol garis sedang pilih-foto" for="foto{pre}">Pilih foto</label>
          <input id="foto{pre}" type="file" accept="image/*" data-foto class="sembunyi">
          <input type="hidden" name="foto_data">
          <p class="bantu">Tanpa foto, kartu memakai warna dan huruf depan nama kue.</p></div></div>
          {hapus}
          <div class="palet" role="radiogroup" aria-label="Warna kartu">{warna}</div></fieldset>"""

    def kue(self):
        rows = self.db.execute("SELECT * FROM cakes ORDER BY active DESC, name").fetchall()
        modal = self.boleh("modal")
        buka = "" if rows else " open"
        isian_modal = ('<label for="km">Harga modal</label><input id="km" name="price_cost" inputmode="numeric" required placeholder="3000">'
                       if modal else '<p class="bantu">Harga modal diisi oleh admin.</p>')
        isi = f"""<details class="tambah"{buka}><summary><span class="tombol garis sedang">{ikon("plus")}Tambah kue</span></summary>
          <div class="panel"><form method="post" action="/kue">
          <label for="kn">Nama kue</label><input id="kn" name="name" required>
          <label for="kj">Harga jual</label><input id="kj" name="price_sell" inputmode="numeric" required placeholder="5000">
          {isian_modal}
          {self.isian_tampilan(None, "baru")}
          <button class="tombol penuh" style="margin-top:20px">Simpan kue</button></form></div></details>"""
        if rows:
            isi += '<ul class="daftar" style="margin-top:16px">'
        for k in rows:
            cek = "checked" if k["active"] else ""
            status = "" if k["active"] else ' <span class="kecil">· tidak dijual</span>'
            info_modal = (f" · modal {rupiah(k['price_cost'])}" if k["price_cost"] else " · modal belum diisi") if modal else ""
            ubah_modal = (f"""<label for="m{k['id']}">Harga modal</label><input id="m{k['id']}" name="price_cost" value="{k['price_cost']}" inputmode="numeric" required>"""
                          if modal else "")
            isi += f"""<li><details><summary><div class="baris">{gambar_kue(k, "gbr mini")}
                <span style="min-width:0"><span class="nama">{e(k['name'])}</span>{status}
                <span class="info" style="margin:2px 0 0">jual {rupiah(k['price_sell'])}{info_modal}</span></span>
                <span class="kanan biasa">Ubah</span></div></summary>
              <div class="panel"><form method="post" action="/kue/{k['id']}/ubah">
              <label for="n{k['id']}">Nama kue</label><input id="n{k['id']}" name="name" value="{e(k['name'])}" required>
              <label for="j{k['id']}">Harga jual</label><input id="j{k['id']}" name="price_sell" value="{k['price_sell']}" inputmode="numeric" required>
              {ubah_modal}
              {self.isian_tampilan(k, k['id'])}
              <label style="display:flex;gap:10px;align-items:center;font-weight:500"><input type="checkbox" name="active" value="1" {cek} style="width:24px;height:24px;accent-color:var(--crust)"> Masih dijual</label>
              <p class="bantu">Harga baru hanya berlaku untuk batch berikutnya. Catatan lama tetap.</p>
              <button class="tombol penuh" style="margin-top:12px">Simpan perubahan</button></form></div></details></li>"""
        if rows:
            isi += "</ul>"
        self.page("Daftar kue dan harga", isi)

    def isian_kue(self, modal_lama=0):
        name = self.f.get("name", "").strip()
        if not name:
            raise Gagal("Nama kue wajib diisi.")
        hm = angka(self.f.get("price_cost"), "Harga modal") if self.boleh("modal") else modal_lama
        return name, angka(self.f.get("price_sell"), "Harga jual"), hm

    def isian_warna(self, lama=None):
        w = self.f.get("color") or lama
        if w and w not in PALET:
            raise Gagal("Warna tidak dikenal.")
        return w

    def simpan_foto(self, cake_id, lama):
        """Simpan foto baru (JPEG dari browser) atau hapus foto. Kembalikan nama file foto yang berlaku."""
        data_url = self.f.get("foto_data", "")
        if self.f.get("hapus_foto") and not data_url:
            hapus_file_foto(lama)
            return None
        if not data_url:
            return lama
        awalan = "data:image/jpeg;base64,"
        if not data_url.startswith(awalan):
            raise Gagal("Foto harus berupa gambar JPEG. Coba pilih ulang fotonya.")
        try:
            data = base64.b64decode(data_url[len(awalan):], validate=True)
        except ValueError:
            raise Gagal("Foto rusak. Coba pilih ulang fotonya.")
        if not data.startswith(b"\xff\xd8\xff") or len(data) > FOTO_MAKS:
            raise Gagal("Foto tidak bisa dipakai (bukan JPEG atau terlalu besar).")
        os.makedirs(foto_dir(), exist_ok=True)
        nama = f"kue-{cake_id}-{secrets.token_hex(6)}.jpg"
        sementara = os.path.join(foto_dir(), nama + ".tmp")
        with open(sementara, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(sementara, os.path.join(foto_dir(), nama))
        hapus_file_foto(lama)
        return nama

    def kue_tambah(self):
        name, hj, hm = self.isian_kue()
        warna = self.isian_warna()
        try:
            with self.db:
                cur = self.db.execute("INSERT INTO cakes(name, price_sell, price_cost, color) VALUES(?,?,?,?)",
                                      (name, hj, hm, warna))
                catat(self.db, self.user["id"], "tambah kue", f"{name} jual {hj} modal {hm}")
        except sqlite3.IntegrityError:
            raise Gagal("Nama kue sudah ada.")
        foto = self.simpan_foto(cur.lastrowid, None)
        if foto:
            with self.db:
                self.db.execute("UPDATE cakes SET photo = ? WHERE id = ?", (foto, cur.lastrowid))
                catat(self.db, self.user["id"], "ubah foto kue", f"{name}: foto baru")
        self.redirect("/kue", f"{name} ditambahkan.")

    def kue_ubah(self):
        lama = self.db.execute("SELECT * FROM cakes WHERE id = ?", (self.id,)).fetchone()
        if not lama:
            raise Gagal("Kue tidak ditemukan.", 404)
        name, hj, hm = self.isian_kue(modal_lama=lama["price_cost"])
        warna = self.isian_warna(lama["color"])
        aktif = 1 if self.f.get("active") else 0
        try:
            with self.db:
                self.db.execute("UPDATE cakes SET name = ?, price_sell = ?, price_cost = ?, active = ?, color = ? WHERE id = ?",
                                (name, hj, hm, aktif, warna, self.id))
                catat(self.db, self.user["id"], "ubah kue",
                      f"{lama['name']} (jual {lama['price_sell']}, modal {lama['price_cost']}, aktif {lama['active']}) -> "
                      f"{name} (jual {hj}, modal {hm}, aktif {aktif})")
        except sqlite3.IntegrityError:
            raise Gagal("Nama kue sudah ada.")
        foto = self.simpan_foto(self.id, lama["photo"])
        if foto != lama["photo"]:
            with self.db:
                self.db.execute("UPDATE cakes SET photo = ? WHERE id = ?", (foto, self.id))
                catat(self.db, self.user["id"], "ubah foto kue", f"{name}: {'foto baru' if foto else 'foto dihapus'}")
        self.redirect("/kue", f"{name} disimpan.")

    # --- pengguna dan akses (khusus admin) ---

    def kotak_akses(self, punya, awal=""):
        kotak = "".join(
            f"""<label class="centang"><input type="checkbox" name="akses" value="{k}"{' checked' if k in punya else ''}>
              <span>{t}</span></label>""" for k, t in PERMS)
        kotak += '<label class="centang mati"><input type="checkbox" disabled><span>Kelola user <small>(khusus admin)</small></span></label>'
        return f'<fieldset class="akses"><legend class="lbl">Akses</legend>{kotak}</fieldset>'

    def isian_gaji(self, u, pre):
        """Sakelar Menerima gaji + persen. Terpisah dari centang 'Lihat gaji sendiri'."""
        cek = " checked" if u and u["digaji"] else ""
        pct = u["pct"] if u else GAJI_PERSEN_DEFAULT
        return f"""<fieldset class="akses"><legend class="lbl">Gaji</legend>
          <label class="sakelar"><input type="checkbox" name="digaji" value="1" role="switch"{cek}>
            <span>Menerima gaji: <b class="ya">Ya</b><b class="tidak">Tidak</b></span></label>
          <p class="bantu" style="margin-top:2px"><b>Menerima gaji</b> = dihitung dan dibayar gajinya.
            <b>Lihat gaji sendiri</b> (di Akses) = boleh melihat angkanya; tidak berpengaruh kalau tidak menerima gaji.</p>
          <label for="pg{pre}">Persen gaji</label>
          <input id="pg{pre}" name="pct" value="{pct}" inputmode="numeric" style="max-width:120px">
          <p class="bantu">Dipakai kalau menerima gaji: persen × harga jual kue yang terjual.</p></fieldset>"""

    def pengguna(self):
        rows = self.db.execute("SELECT * FROM users ORDER BY role, name").fetchall()
        akses = {}
        for r in self.db.execute("SELECT user_id, perm FROM user_perms"):
            akses.setdefault(r[0], set()).add(r[1])
        isi = f"""<details class="tambah"><summary><span class="tombol garis sedang">{ikon("plus")}Tambah karyawan</span></summary>
          <div class="panel"><form method="post" action="/pengguna">
          <label for="pn">Nama</label><input id="pn" name="name" required>
          <label for="pu">Username</label><input id="pu" name="username" required autocapitalize="none">
          <label for="pp">Password</label><input id="pp" name="password" type="password" required minlength="6" autocomplete="new-password">
          <p class="bantu">Minimal 6 huruf. Berikan langsung ke karyawan.</p>
          {self.kotak_akses(set())}
          <button type="button" class="biasa" data-preset="{",".join(AKSES_KAKAK)}">Centang akses pembuat kue</button>
          {self.isian_gaji(None, "baru")}
          <p class="bantu">Karyawan baru mulai tanpa akses dan tidak menerima gaji sampai kamu atur.</p>
          <button class="tombol penuh" style="margin-top:16px">Simpan karyawan</button></form></div></details>
          <ul class="daftar" style="margin-top:16px">"""
        for u in rows:
            punya = akses.get(u["id"], set())
            if u["role"] == "admin":
                ringkas = "admin · semua akses · tidak dihitung gaji"
                form_akses = ('<p class="bantu">Admin selalu punya semua akses, termasuk kelola user. '
                              'Gaji admin tidak dihitung (menunggu keputusan pemilik).</p>')
            else:
                label = [t for k, t in PERMS if k in punya]
                gaji_teks = f"digaji {u['pct']}%" if u["digaji"] else "tidak digaji"
                ringkas = (", ".join(label) if label else "belum ada akses") + f" · {gaji_teks}"
                form_akses = f"""<form method="post" action="/pengguna/{u['id']}/akses">{self.isian_gaji(u, u['id'])}{self.kotak_akses(punya)}
                  <button class="tombol penuh" style="margin-top:16px">Simpan</button></form>"""
            isi += f"""<li><details><summary><div class="baris"><span><span class="nama">{e(u['name'])}</span>
              <span class="info" style="margin:2px 0 0">{e(u['username'])} · {e(ringkas)}</span></span>
              <span class="kanan biasa">Atur</span></div></summary>
              <div class="panel">{form_akses}
              <form method="post" action="/pengguna/{u['id']}/password">
              <label for="pw{u['id']}">Password baru untuk {e(u['name'])}</label>
              <input id="pw{u['id']}" name="password" type="password" minlength="6" required autocomplete="new-password">
              <button class="tombol garis penuh" style="margin-top:16px">Ganti password</button></form></div></details></li>"""
        isi += "</ul>"
        self.page("Pengguna dan akses", isi)

    def isian_akses(self):
        diminta = set(self.fl.get("akses", []))
        if diminta - PERM_KEYS:
            raise Gagal("Akses tidak dikenal: " + ", ".join(sorted(diminta - PERM_KEYS)) + ". Kelola user khusus admin.")
        pct = angka(self.f.get("pct") or str(GAJI_PERSEN_DEFAULT), "Persen gaji")
        if pct > 100:
            raise Gagal("Persen gaji maksimal 100.")
        digaji = 1 if self.f.get("digaji") == "1" else 0   # sakelar tidak dicentang = Tidak
        return diminta, pct, digaji

    def simpan_akses(self, uid, akses, pct, digaji):
        self.db.execute("DELETE FROM user_perms WHERE user_id = ?", (uid,))
        self.db.executemany("INSERT INTO user_perms(user_id, perm) VALUES(?,?)", [(uid, p) for p in sorted(akses)])
        self.db.execute("UPDATE users SET pct = ?, digaji = ? WHERE id = ?", (pct, digaji, uid))

    def pengguna_tambah(self):
        akses, pct, digaji = self.isian_akses()
        uid = self.buat_user("karyawan")
        with self.db:
            self.simpan_akses(uid, akses, pct, digaji)
            catat(self.db, self.user["id"], "ubah akses", f"user #{uid}: {', '.join(sorted(akses)) or 'tanpa akses'}; gaji {pct}%")
            catat(self.db, self.user["id"], "ubah status gaji", f"user #{uid} (baru): menerima gaji {'Ya' if digaji else 'Tidak'}")
        self.redirect("/pengguna", "Karyawan ditambahkan.")

    def konfirmasi_nonaktif_gaji(self, u, akses, pct, g):
        """Ya -> Tidak padahal masih ada gaji belum dibayar: jangan hilangkan diam-diam, minta konfirmasi."""
        tersembunyi = "".join(f'<input type="hidden" name="akses" value="{a}">' for a in sorted(akses))
        isi = kabar("awas", f"<b>{e(u['name'])}</b> masih punya gaji belum dibayar <b>{rupiah(g['jumlah'])}</b> "
                            f"({tgl_indo(g['mulai'])} – {tgl_indo(g['sampai'])}). Kalau dinonaktifkan, angka ini tidak dihitung lagi.")
        isi += f"""<p>Bayar dulu atau tetap dinonaktifkan?</p>
          <a class="tombol penuh" href="/gaji">Bayar dulu di halaman Gaji</a>
          <form method="post" action="/pengguna/{u['id']}/akses" style="margin-top:12px">{tersembunyi}
            <input type="hidden" name="pct" value="{pct}"><input type="hidden" name="digaji" value="0">
            <input type="hidden" name="konfirmasi_nonaktif" value="1">
            <button class="tombol garis penuh">Tetap nonaktifkan gaji</button></form>
          <p style="margin-top:12px"><a href="/pengguna">Batal</a></p>"""
        self.page("Gaji belum dibayar", isi, status=409)

    def pengguna_akses(self):
        u = self.db.execute("SELECT * FROM users WHERE id = ?", (self.id,)).fetchone()
        if not u:
            raise Gagal("Pengguna tidak ditemukan.", 404)
        if u["role"] == "admin":
            raise Gagal("Akses admin tidak bisa diubah: admin selalu punya semua akses.")
        akses, pct, digaji = self.isian_akses()
        lama = {r[0] for r in self.db.execute("SELECT perm FROM user_perms WHERE user_id = ?", (u["id"],))}
        catatan_gaji = ""
        if u["digaji"] and not digaji:
            g = gaji_info(self.db, u)
            if g["jumlah"] > 0:
                if self.f.get("konfirmasi_nonaktif") != "1":
                    return self.konfirmasi_nonaktif_gaji(u, akses, pct, g)
                catatan_gaji = f"; gaji belum dibayar {rupiah(g['jumlah'])} ({g['mulai']}..{g['sampai']}) tidak dihitung lagi"
        with self.db:
            self.simpan_akses(u["id"], akses, pct, digaji)
            if digaji and not u["digaji"]:
                # mulai dihitung lagi dari hari ini, bukan dari masa ketika tidak digaji
                self.db.execute("UPDATE users SET gaji_mulai = ? WHERE id = ?", (today(), u["id"]))
            catat(self.db, self.user["id"], "ubah akses",
                  f"{u['username']}: {', '.join(sorted(lama)) or '-'} -> {', '.join(sorted(akses)) or '-'}; gaji {u['pct']}% -> {pct}%")
            if digaji != u["digaji"]:
                catat(self.db, self.user["id"], "ubah status gaji",
                      f"{u['username']}: menerima gaji {'Ya' if u['digaji'] else 'Tidak'} -> {'Ya' if digaji else 'Tidak'}{catatan_gaji}")
        self.redirect("/pengguna", f"Pengaturan {u['name']} disimpan.")

    def pengguna_password(self):
        u = self.db.execute("SELECT * FROM users WHERE id = ?", (self.id,)).fetchone()
        if not u:
            raise Gagal("Pengguna tidak ditemukan.", 404)
        pw = self.f.get("password", "")
        if len(pw) < 6:
            raise Gagal("Password minimal 6 huruf.")
        with self.db:
            self.db.execute("UPDATE users SET pw = ? WHERE id = ?", (hash_pw(pw), u["id"]))
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
            catat(self.db, self.user["id"], "ganti password", u["username"])
        if u["id"] == self.user["id"]:
            return self.redirect("/login", "Password diganti. Silakan masuk lagi.")
        self.redirect("/pengguna", f"Password {u['username']} diganti.")

    # --- riwayat perubahan (admin) ---

    def riwayat(self):
        rows = self.db.execute("""SELECT a.*, u.name FROM audit a LEFT JOIN users u ON u.id = a.user_id
            ORDER BY a.id DESC LIMIT 300""").fetchall()
        isi = '<ul class="daftar">' + "".join(
            f"""<li><div class="baris"><span class="nama" style="font-size:16px">{e(r['action'])}</span>
              <span class="kanan kecil">{r['at'][5:16].replace('-', '/', 1)}</span></div>
              <div class="info"><span>{e(r['name'] or '-')}</span><span>{e(r['detail'])}</span></div></li>""" for r in rows)
        self.page("Riwayat perubahan", isi + "</ul>")


def main():
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), App)
    print(f"Toko Kue jalan di http://{HOST}:{PORT}  (data: {DB_PATH})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
