#!/usr/bin/env python3
"""Pencatatan Toko Kue, prototype versi 1.

Hanya memakai pustaka bawaan Python 3.9+ (http.server + sqlite3), jadi tidak perlu install apa pun.
Jalankan:  python3 app.py      lalu buka http://<ip-server>:8000 dari HP di wifi toko.
"""
import hashlib
import html
import os
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


# ---------- tampilan ----------
# Arah desain (disetujui pemilik): "etalase roti pagi hari". Krem kertas, tinta kakao, tombol kulit roti,
# garis tipis (tanpa bayangan), ikon garis SVG, angka penting besar, aksi utama di zona jempol.

FONTS = {"karla.woff2", "playfair.woff2"}

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
.atas{display:flex;align-items:center;gap:12px;padding:14px 20px 0;max-width:760px;margin:0 auto}
.merek{font:700 19px/1 var(--serif);color:var(--crust);text-decoration:none;letter-spacing:.2px}
.atas .siapa{margin-left:auto;font-size:14px;color:var(--ink-2)}
.atas form{margin:0}
main{max-width:760px;margin:0 auto;padding:8px 20px calc(var(--nav-h) + 32px + env(safe-area-inset-bottom))}
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
  font:700 18px/1 var(--sans);text-decoration:none;cursor:pointer;transition:background-color .12s}
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
.masuk{max-width:440px;margin:0 auto;padding:48px 24px}
.masuk .merek{font-size:34px;display:block;margin-bottom:6px}

@media (min-width:768px){
  :root{--rail:104px}
  .nav{top:0;right:auto;width:var(--rail);flex-direction:column;justify-content:flex-start;padding-top:20px;border-top:0;border-right:1px solid var(--line)}
  .nav a,.nav button{flex:none;min-height:80px}
  .nav a[aria-current]::before{top:16%;bottom:16%;left:0;right:auto;width:3px;height:auto;border-radius:0 3px 3px 0}
  body:not(.tanpa-nav) .atas,body:not(.tanpa-nav) main{margin-left:calc(var(--rail) + max(0px,(100vw - var(--rail) - 760px)/2))}
  body:not(.tanpa-nav) .bar{left:var(--rail)}
  .bar{bottom:0;padding:12px 32px calc(12px + env(safe-area-inset-bottom))}
  main,body.ada-bar main{padding-bottom:calc(var(--bar-h) + 40px)}
  .atas,main{padding-left:32px;padding-right:32px}
  h1{font-size:36px}
  .ubin{grid-template-columns:repeat(3,1fr)}
  .utama-blok .angka,.besar .angka{font-size:56px}
  .duo .nilai{font-size:32px}
  .bar .isi{max-width:520px;margin:0}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = """document.addEventListener('click',function(ev){var b=ev.target.closest('[data-langkah]');if(!b)return;
var i=b.parentNode.querySelector('input');var v=parseInt(i.value||'0',10)||0;v=Math.max(0,v+parseInt(b.dataset.langkah,10));
if(i.dataset.maks)v=Math.min(v,parseInt(i.dataset.maks,10));i.value=v;});"""

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


def stepper(nama, nilai, label, maks=None, lebar=False):
    m = f' data-maks="{maks}"' if maks is not None else ""
    return f"""<div class="stepper{' lebar' if lebar else ''}">
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

ROUTES = {
    ("GET", "/setup"): ("admin_pertama_form", None),
    ("POST", "/setup"): ("admin_pertama", None),
    ("GET", "/login"): ("login_form", None),
    ("POST", "/login"): ("login", None),
    ("POST", "/logout"): ("logout", "login"),
    ("GET", "/"): ("home", "login"),
    ("GET", "/produksi"): ("produksi", "login"),
    ("POST", "/produksi"): ("produksi_tambah", "login"),
    ("POST", "/produksi/:id/ubah"): ("produksi_ubah", "login"),
    ("POST", "/produksi/:id/hapus"): ("produksi_hapus", "login"),
    ("GET", "/sisa"): ("sisa", "admin"),
    ("POST", "/sisa"): ("sisa_simpan", "admin"),
    ("GET", "/laporan"): ("laporan", "admin"),
    ("GET", "/lainnya"): ("lainnya", "admin"),
    ("GET", "/kue"): ("kue", "admin"),
    ("POST", "/kue"): ("kue_tambah", "admin"),
    ("POST", "/kue/:id/ubah"): ("kue_ubah", "admin"),
    ("GET", "/pengguna"): ("pengguna", "admin"),
    ("POST", "/pengguna"): ("pengguna_tambah", "admin"),
    ("POST", "/pengguna/:id/password"): ("pengguna_password", "admin"),
    ("GET", "/riwayat"): ("riwayat", "admin"),
}

NAV_ADMIN = [("/", "Beranda", "beranda"), ("/produksi", "Produksi", "produksi"), ("/sisa", "Sisa", "sisa"),
             ("/laporan", "Laporan", "laporan"), ("/lainnya", "Lainnya", "lainnya")]


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
        self.q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.f = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace")
            self.f = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
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
            if syarat == "admin" and self.user["role"] != "admin":
                raise Gagal("Halaman ini khusus admin.", 403)
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
        admin = bool(u and u["role"] == "admin")
        kelas = [] if admin else ["tanpa-nav"]
        if bar:
            kelas.append("ada-bar")
        nav = ""
        if admin:
            path = urlparse(self.path).path
            bagian = {"/kue": "/lainnya", "/pengguna": "/lainnya", "/riwayat": "/lainnya"}.get(path, path)
            nav = '<nav class="nav" aria-label="Menu utama">' + "".join(
                f'<a href="{h}"{" aria-current=page" if bagian == h else ""}>{ikon(i)}{t}</a>'
                for h, t, i in NAV_ADMIN) + "</nav>"
        atas = ""
        if u:
            keluar = "" if admin else ('<form method="post" action="/logout"><button class="biasa">Keluar</button></form>')
            atas = f'<div class="atas"><a class="merek" href="/">Toko Kue</a><span class="siapa">{e(u["name"])}</span>{keluar}</div>'
        pesan = kabar("ok", e(self.q["pesan"])) if self.q.get("pesan") else ""
        alis = f'<p class="alis">{alis}</p>' if alis else ""
        self.send(status, f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#F7F1E6"><title>{e(judul)} · Toko Kue</title>
<link rel="preload" href="/static/fonts/karla.woff2" as="font" type="font/woff2" crossorigin>
<style>{CSS}</style></head><body class="{' '.join(kelas)}">{atas}
<main>{alis}<h1>{h1 or e(judul)}</h1>{pesan}{isi}</main>{bar}{nav}<script>{JS}</script></body></html>""")

    def admin(self):
        return self.user["role"] == "admin"

    # --- setup & login ---

    def halaman_masuk(self, judul, isi, status=200):
        self.send(status, f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#F7F1E6"><title>{e(judul)} · Toko Kue</title><style>{CSS}</style></head>
<body class="tanpa-nav"><div class="masuk"><span class="merek">Toko Kue</span>
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
                cur = self.db.execute("INSERT INTO users(username, name, pw, role) VALUES(?,?,?,?)",
                                      (username, name, hash_pw(pw), role))
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

    # --- beranda (admin) ---

    def home(self):
        if not self.admin():
            return self.redirect("/produksi")
        tgl = today()
        lp = laporan_harian(self.db, tgl)
        t = lp["total"]
        isi = f"""<section class="utama-blok" aria-label="Omzet hari ini">
          <div class="label">Omzet hari ini</div>
          <div class="angka">{rp_besar(t['omzet'])}</div>
          <div class="sub">{t['terjual']} kue terjual dari {t['dibuat']} dibuat</div></section>
          <div class="duo">
            <div><div class="label">Untung</div><div class="nilai untung">{rupiah(t['untung'])}</div></div>
            <div><div class="label">Rugi dari sisa</div><div class="nilai rugi">{rupiah(t['kerugian'])}</div></div></div>"""
        if not lp["items"]:
            isi += '<h2>Status</h2>' + kabar("awas", "Belum ada produksi hari ini.")
        elif lp["sisa_belum"]:
            isi += '<h2>Status</h2>' + kabar(
                "awas", f'Sisa belum diisi untuk {len(lp["sisa_belum"])} kue. Sampai diisi, semua dianggap terjual.')
        else:
            isi += '<h2>Status</h2>' + kabar("ok", "Sisa hari ini sudah diisi.")
        if lp["items"]:
            isi += '<h2>Per kue</h2><ul class="daftar">'
            for i in lp["items"]:
                s = i["sisa"] if i["sisa_diisi"] else "belum"
                isi += f"""<li class="baris"><div><div class="nama">{e(i['nama'])}</div>
                  <div class="info"><span>dibuat {i['dibuat']}</span><span>sisa {s}</span></div></div>
                  <div class="kanan"><div class="qty">{i['terjual']}</div><div class="kecil">terjual</div></div></li>"""
            isi += "</ul>"
        isi += f'<p style="margin-top:20px"><a href="/laporan?tanggal={tgl}">Laporan harian lengkap</a></p>'
        bar = bar_aksi('<a class="tombol" href="/produksi">Catat produksi</a>',
                       f'<a class="tombol garis" href="/sisa">Catat sisa tutup</a>')
        self.page("Beranda", isi, alis=tgl_indo(tgl), bar=bar, h1="Hari ini")

    def lainnya(self):
        menu = [("/kue", "kue", "Daftar kue dan harga", "Tambah kue, ubah harga jual dan modal"),
                ("/pengguna", "orang", "Pengguna", "Tambah karyawan, buatkan password baru"),
                ("/riwayat", "jam", "Riwayat perubahan", "Siapa mengubah apa dan kapan")]
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
            ubin = "".join(
                f"""<input type="radio" name="cake_id" id="k{k['id']}" value="{k['id']}" required{' checked' if n == 0 else ''}>
                <label for="k{k['id']}">{e(k['name'])}<span>hari ini {k['hari_ini'] or 0}</span></label>"""
                for n, k in enumerate(kue))
            isi += f"""<form id="f-produksi" method="post" action="/produksi">
              <input type="hidden" name="tanggal" value="{tgl}">
              <fieldset style="border:0;padding:0;margin:0"><legend class="lbl" style="margin-top:4px">1. Pilih kue</legend>
              <div class="ubin">{ubin}</div></fieldset>
              <span class="lbl">2. Jumlah dibuat di batch ini</span>
              {stepper("qty", "", "Jumlah dibuat", lebar=True)}
              <p class="bantu">Bikin lagi nanti? Simpan sebagai batch baru, otomatis dijumlahkan.</p></form>"""
            bar = bar_aksi('<button class="tombol" form="f-produksi">Simpan batch</button>')
        else:
            isi += kabar("awas", "Belum ada kue. Admin perlu menambah kue dulu di menu Lainnya, Daftar kue.")
        rows = self.db.execute("""SELECT p.*, c.name kue, u.name oleh FROM production p
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
                isi += f"""<li><div class="baris"><span class="nama">{e(r['kue'])}</span><span class="kanan qty">{r['qty']}</span></div>
                  <div class="info"><span>{r['created'][11:16]}</span><span>{e(r['oleh'])}</span></div>{ubah}</li>"""
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
        tgl = tanggal(self.q.get("tanggal") or today())
        rows = self.db.execute("""SELECT c.id, c.name, SUM(p.qty) dibuat,
              (SELECT qty FROM leftover l WHERE l.cake_id = c.id AND l.date = :d) sisa
            FROM production p JOIN cakes c ON c.id = p.cake_id WHERE p.date = :d
            GROUP BY c.id ORDER BY c.name""", {"d": tgl}).fetchall()
        isi = form_tanggal("/sisa", tgl)
        bar = ""
        if not rows:
            isi += kabar("awas", "Belum ada produksi di tanggal ini.")
        else:
            isi += '<p class="kecil" style="margin:8px 0 12px">Isi jumlah kue yang tidak terjual. Kosong dihitung 0. Sisa dibagikan, jadi dicatat sebagai kerugian.</p>'
            isi += f'<form id="f-sisa" method="post" action="/sisa"><input type="hidden" name="tanggal" value="{tgl}"><ul class="daftar">'
            for r in rows:
                val = "" if r["sisa"] is None else r["sisa"]
                isi += f"""<li class="baris"><div><div class="nama">{e(r['name'])}</div>
                  <div class="info" style="margin-top:2px">dibuat {r['dibuat']}</div></div>
                  <div class="kanan">{stepper(f"sisa_{r['id']}", val, f"Sisa {r['name']}", maks=r['dibuat'])}</div></li>"""
            isi += "</ul></form>"
            bar = bar_aksi('<button class="tombol" form="f-sisa">Simpan sisa</button>')
        self.page("Sisa saat tutup", isi, bar=bar, alis=tgl_indo(tgl))

    def sisa_simpan(self):
        tgl = tanggal(self.f.get("tanggal"))
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
        self.redirect(f"/laporan?tanggal={tgl}", "Sisa tersimpan.")

    # --- laporan (admin) ---

    def laporan(self):
        tgl = tanggal(self.q.get("tanggal") or today())
        lp = laporan_harian(self.db, tgl)
        t = lp["total"]
        isi = form_tanggal("/laporan", tgl)
        if not lp["items"]:
            return self.page("Laporan harian", isi + kabar("awas", "Belum ada produksi di tanggal ini."), alis=tgl_indo(tgl))
        if lp["sisa_belum"]:
            isi += kabar("awas", f'Sisa belum diisi untuk: {e(", ".join(lp["sisa_belum"]))}. Sampai diisi, semua dianggap terjual. '
                                 f'<a href="/sisa?tanggal={tgl}">Isi sisa</a>')
        laku = f'{e(lp["laku"]["nama"])} <span class="kecil">· {lp["laku"]["terjual"]} terjual</span>' if lp["laku"] else "-"
        bsisa = (f'{e(lp["banyak_sisa"]["nama"])} <span class="kecil">· sisa {lp["banyak_sisa"]["sisa"]}</span>'
                 if lp["banyak_sisa"] else "-")
        isi += f"""<section class="besar" style="margin-top:12px"><div class="label">Omzet</div>
          <div class="angka">{rp_besar(t['omzet'])}</div></section>
          <div class="duo">
            <div><div class="label">Untung*</div><div class="nilai untung">{rupiah(t['untung'])}</div></div>
            <div><div class="label">Rugi dari sisa</div><div class="nilai rugi">{rupiah(t['kerugian'])}</div></div></div>
          <div class="duo">
            <div><div class="label">Terjual</div><div class="nilai">{t['terjual']}</div></div>
            <div><div class="label">Sisa</div><div class="nilai">{t['sisa']}</div></div></div>
          <ul class="daftar" style="border-top:0">
            <li class="baris"><span class="kecil" style="min-width:150px">Untung setelah sisa*</span><span class="kanan" style="font-weight:800">{rupiah(t['untung_bersih'])}</span></li>
            <li class="baris"><span class="kecil" style="min-width:150px">Paling laku</span><span class="kanan" style="font-weight:700">{laku}</span></li>
            <li class="baris"><span class="kecil" style="min-width:150px">Paling banyak sisa</span><span class="kanan" style="font-weight:700">{bsisa}</span></li></ul>
          <p class="bantu">*Untung = omzet − (terjual × harga modal). Untung setelah sisa = untung − rugi dari sisa.
          Rumus untung masih menunggu konfirmasi pemilik.</p>"""
        isi += '<h2>Per kue</h2><ul class="daftar">'
        for i in lp["items"]:
            s = i["sisa"] if i["sisa_diisi"] else "belum diisi"
            isi += f"""<li><div class="baris"><span class="nama">{e(i['nama'])}</span><span class="kanan" style="font-weight:800">{rupiah(i['omzet'])}</span></div>
              <div class="info"><span>dibuat {i['dibuat']}</span><span>sisa {s}</span><span>terjual {i['terjual']}</span>
              <span>@ {rupiah(i['harga_jual'])}</span></div>
              <div class="info"><span class="untung">untung {rupiah(i['untung'])}</span><span style="color:var(--terra-text)">rugi sisa {rupiah(i['kerugian'])}</span></div></li>"""
        isi += f"""<li><div class="baris"><span class="nama">Total</span><span class="kanan" style="font-weight:800">{rupiah(t['omzet'])}</span></div>
          <div class="info"><span>dibuat {t['dibuat']}</span><span>sisa {t['sisa']}</span><span>terjual {t['terjual']}</span></div></li></ul>"""
        self.page("Laporan harian", isi, alis=tgl_indo(tgl))

    # --- kue (admin) ---

    def kue(self):
        rows = self.db.execute("SELECT * FROM cakes ORDER BY active DESC, name").fetchall()
        buka = "" if rows else " open"
        isi = f"""<details class="tambah"{buka}><summary><span class="tombol garis sedang">{ikon("plus")}Tambah kue</span></summary>
          <div class="panel"><form method="post" action="/kue">
          <label for="kn">Nama kue</label><input id="kn" name="name" required>
          <label for="kj">Harga jual</label><input id="kj" name="price_sell" inputmode="numeric" required placeholder="5000">
          <label for="km">Harga modal</label><input id="km" name="price_cost" inputmode="numeric" required placeholder="3000">
          <button class="tombol penuh" style="margin-top:20px">Simpan kue</button></form></div></details>"""
        if rows:
            isi += '<ul class="daftar" style="margin-top:16px">'
        for k in rows:
            cek = "checked" if k["active"] else ""
            status = "" if k["active"] else ' <span class="kecil">· tidak dijual</span>'
            isi += f"""<li><details><summary><div class="baris"><span><span class="nama">{e(k['name'])}</span>{status}
                <span class="info" style="margin:2px 0 0">jual {rupiah(k['price_sell'])} · modal {rupiah(k['price_cost'])}</span></span>
                <span class="kanan biasa">Ubah</span></div></summary>
              <div class="panel"><form method="post" action="/kue/{k['id']}/ubah">
              <label for="n{k['id']}">Nama kue</label><input id="n{k['id']}" name="name" value="{e(k['name'])}" required>
              <label for="j{k['id']}">Harga jual</label><input id="j{k['id']}" name="price_sell" value="{k['price_sell']}" inputmode="numeric" required>
              <label for="m{k['id']}">Harga modal</label><input id="m{k['id']}" name="price_cost" value="{k['price_cost']}" inputmode="numeric" required>
              <label style="display:flex;gap:10px;align-items:center;font-weight:500"><input type="checkbox" name="active" value="1" {cek} style="width:24px;height:24px;accent-color:var(--crust)"> Masih dijual</label>
              <p class="bantu">Harga baru hanya berlaku untuk batch berikutnya. Catatan lama tetap.</p>
              <button class="tombol penuh" style="margin-top:12px">Simpan perubahan</button></form></div></details></li>"""
        if rows:
            isi += "</ul>"
        self.page("Daftar kue dan harga", isi)

    def isian_kue(self):
        name = self.f.get("name", "").strip()
        if not name:
            raise Gagal("Nama kue wajib diisi.")
        return name, angka(self.f.get("price_sell"), "Harga jual"), angka(self.f.get("price_cost"), "Harga modal")

    def kue_tambah(self):
        name, hj, hm = self.isian_kue()
        try:
            with self.db:
                self.db.execute("INSERT INTO cakes(name, price_sell, price_cost) VALUES(?,?,?)", (name, hj, hm))
                catat(self.db, self.user["id"], "tambah kue", f"{name} jual {hj} modal {hm}")
        except sqlite3.IntegrityError:
            raise Gagal("Nama kue sudah ada.")
        self.redirect("/kue", f"{name} ditambahkan.")

    def kue_ubah(self):
        lama = self.db.execute("SELECT * FROM cakes WHERE id = ?", (self.id,)).fetchone()
        if not lama:
            raise Gagal("Kue tidak ditemukan.", 404)
        name, hj, hm = self.isian_kue()
        aktif = 1 if self.f.get("active") else 0
        try:
            with self.db:
                self.db.execute("UPDATE cakes SET name = ?, price_sell = ?, price_cost = ?, active = ? WHERE id = ?",
                                (name, hj, hm, aktif, self.id))
                catat(self.db, self.user["id"], "ubah kue",
                      f"{lama['name']} (jual {lama['price_sell']}, modal {lama['price_cost']}, aktif {lama['active']}) -> "
                      f"{name} (jual {hj}, modal {hm}, aktif {aktif})")
        except sqlite3.IntegrityError:
            raise Gagal("Nama kue sudah ada.")
        self.redirect("/kue", f"{name} disimpan.")

    # --- pengguna (admin) ---

    def pengguna(self):
        rows = self.db.execute("SELECT * FROM users ORDER BY role, name").fetchall()
        isi = f"""<details class="tambah"><summary><span class="tombol garis sedang">{ikon("plus")}Tambah karyawan</span></summary>
          <div class="panel"><form method="post" action="/pengguna">
          <label for="pn">Nama</label><input id="pn" name="name" required>
          <label for="pu">Username</label><input id="pu" name="username" required autocapitalize="none">
          <label for="pp">Password</label><input id="pp" name="password" type="password" required minlength="6" autocomplete="new-password">
          <p class="bantu">Minimal 6 huruf. Berikan langsung ke karyawan.</p>
          <button class="tombol penuh" style="margin-top:16px">Simpan karyawan</button></form></div></details>
          <ul class="daftar" style="margin-top:16px">"""
        for u in rows:
            isi += f"""<li><details><summary><div class="baris"><span><span class="nama">{e(u['name'])}</span>
              <span class="info" style="margin:2px 0 0">{e(u['username'])} · {u['role']}</span></span>
              <span class="kanan biasa">Password baru</span></div></summary>
              <div class="panel"><form method="post" action="/pengguna/{u['id']}/password">
              <label for="pw{u['id']}">Password baru untuk {e(u['name'])}</label>
              <input id="pw{u['id']}" name="password" type="password" minlength="6" required autocomplete="new-password">
              <button class="tombol penuh" style="margin-top:16px">Ganti password</button></form></div></details></li>"""
        isi += '</ul><p class="bantu">Versi 1: karyawan hanya bisa input produksi. Hak akses per orang menyusul di fase 2.</p>'
        self.page("Pengguna", isi)

    def pengguna_tambah(self):
        self.buat_user("karyawan")
        self.redirect("/pengguna", "Karyawan ditambahkan.")

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
