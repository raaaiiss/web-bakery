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

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#fdf8f3;color:#3b2a1a;font-size:17px}
header{background:#8a4b1f;color:#fff;padding:10px 14px;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}
header b{margin-right:auto}header a,header button{color:#fff;text-decoration:none;background:none;border:0;font:inherit;padding:6px 0;cursor:pointer}
main{max-width:860px;margin:auto;padding:14px}h1{font-size:1.4em;margin:.3em 0 .6em}
.kartu{background:#fff;border:1px solid #ead9c6;border-radius:12px;padding:14px;margin-bottom:14px}
label{display:block;margin:.5em 0 .2em;font-weight:600}
input,select{width:100%;padding:12px;font-size:1em;border:1px solid #c9b29a;border-radius:8px;background:#fff}
button.utama,button.kecil{background:#8a4b1f;color:#fff;border:0;border-radius:8px;padding:13px 18px;font-size:1em;cursor:pointer;margin-top:10px}
button.kecil{padding:8px 12px;margin:0;font-size:.9em}button.hapus{background:#b3261e}
table{width:100%;border-collapse:collapse}th,td{padding:8px 6px;border-bottom:1px solid #ead9c6;text-align:left;vertical-align:middle}
td.n,th.n{text-align:right;white-space:nowrap}.ringkas{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.ringkas div{background:#fff;border:1px solid #ead9c6;border-radius:12px;padding:12px}.ringkas small{display:block;color:#7a6350}
.ringkas strong{font-size:1.25em}.pesan{background:#e6f4ea;border:1px solid #9bd3ae;padding:10px;border-radius:8px;margin-bottom:12px}
.galat{background:#fdecea;border:1px solid #f1a9a0;padding:10px;border-radius:8px;margin-bottom:12px}
.peringatan{background:#fff4d6;border:1px solid #f0d080;padding:10px;border-radius:8px;margin-bottom:12px}
.baris{display:flex;gap:8px;align-items:center}.baris input{width:90px}.muted{color:#7a6350;font-size:.9em}
.scroll{overflow-x:auto}
"""


def form_tanggal(aksi, tgl):
    kemarin = (date.fromisoformat(tgl) - timedelta(days=1)).isoformat()
    besok = (date.fromisoformat(tgl) + timedelta(days=1)).isoformat()
    return f"""<form class="kartu baris" method="get" action="{aksi}">
      <a href="{aksi}?tanggal={kemarin}">&larr;</a>
      <input type="date" name="tanggal" value="{e(tgl)}" style="width:auto;flex:1" onchange="this.form.submit()">
      <a href="{aksi}?tanggal={besok}">&rarr;</a></form>"""


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
    ("GET", "/kue"): ("kue", "admin"),
    ("POST", "/kue"): ("kue_tambah", "admin"),
    ("POST", "/kue/:id/ubah"): ("kue_ubah", "admin"),
    ("GET", "/pengguna"): ("pengguna", "admin"),
    ("POST", "/pengguna"): ("pengguna_tambah", "admin"),
    ("POST", "/pengguna/:id/password"): ("pengguna_password", "admin"),
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
            back = '<p><a href="javascript:history.back()">&larr; Kembali</a></p>'
            self.page("Ada masalah", f'<div class="galat">{e(g)}</div>{back}', g.status)
        finally:
            self.db.close()

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

    def page(self, judul, isi, status=200):
        u = getattr(self, "user", None)
        nav = ""
        if u:
            links = [("/produksi", "Produksi")]
            if u["role"] == "admin":
                links += [("/sisa", "Sisa"), ("/laporan", "Laporan"), ("/kue", "Kue"),
                          ("/pengguna", "Pengguna"), ("/riwayat", "Riwayat")]
            nav = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
            nav += f'<form method="post" action="/logout" style="margin:0"><button>Keluar ({e(u["name"])})</button></form>'
        pesan = f'<div class="pesan">{e(self.q["pesan"])}</div>' if self.q.get("pesan") else ""
        self.send(status, f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(judul)} · Toko Kue</title>
<style>{CSS}</style></head><body><header><b>🍩 Toko Kue</b>{nav}</header>
<main><h1>{e(judul)}</h1>{pesan}{isi}</main></body></html>""")

    def admin(self):
        return self.user["role"] == "admin"

    # --- setup & login ---

    def admin_pertama_form(self):
        self.page("Buat akun admin pertama", """<form class="kartu" method="post">
          <p class="muted">Halaman ini hanya muncul sekali, saat belum ada pengguna.</p>
          <label>Nama</label><input name="name" required>
          <label>Username</label><input name="username" required autocapitalize="none">
          <label>Password (min. 6 huruf)</label><input name="password" type="password" required minlength="6">
          <button class="utama">Buat admin</button></form>""")

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
        self.page("Masuk", """<form class="kartu" method="post">
          <label>Username</label><input name="username" required autocapitalize="none">
          <label>Password</label><input name="password" type="password" required>
          <button class="utama">Masuk</button>
          <p class="muted">Lupa password? Minta admin buatkan password baru.</p></form>""")

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

    def home(self):
        self.redirect("/laporan" if self.admin() else "/produksi")

    # --- produksi ---

    def boleh_ubah(self, row):
        return self.admin() or (row["user_id"] == self.user["id"] and row["date"] == today())

    def produksi(self):
        tgl = tanggal(self.q["tanggal"]) if self.admin() and self.q.get("tanggal") else today()
        kue = self.db.execute("SELECT * FROM cakes WHERE active = 1 ORDER BY name").fetchall()
        opsi = "".join(f'<option value="{k["id"]}">{e(k["name"])}</option>' for k in kue)
        isi = form_tanggal("/produksi", tgl) if self.admin() else f'<p class="muted">{tgl_indo(tgl)}</p>'
        if kue:
            isi += f"""<form class="kartu" method="post" action="/produksi">
              <input type="hidden" name="tanggal" value="{tgl}">
              <label>Kue</label><select name="cake_id">{opsi}</select>
              <label>Jumlah dibuat (batch ini)</label><input name="qty" inputmode="numeric" required>
              <button class="utama">Simpan batch</button></form>"""
        else:
            isi += '<div class="peringatan">Belum ada kue. Admin perlu menambah kue dulu di menu Kue.</div>'
        total = self.db.execute("""SELECT c.name, SUM(p.qty) q FROM production p JOIN cakes c ON c.id = p.cake_id
            WHERE p.date = ? GROUP BY c.id ORDER BY c.name""", (tgl,)).fetchall()
        if total:
            isi += '<div class="kartu"><b>Total hari ini</b><table>' + "".join(
                f'<tr><td>{e(r["name"])}</td><td class="n">{r["q"]}</td></tr>' for r in total) + "</table></div>"
        rows = self.db.execute("""SELECT p.*, c.name kue, u.name oleh FROM production p
            JOIN cakes c ON c.id = p.cake_id JOIN users u ON u.id = p.user_id
            WHERE p.date = ? ORDER BY p.id DESC""", (tgl,)).fetchall()
        if rows:
            isi += '<div class="kartu scroll"><b>Batch</b><table><tr><th>Jam</th><th>Kue</th><th>Oleh</th><th class="n">Jumlah</th></tr>'
            for r in rows:
                if self.boleh_ubah(r):
                    aksi = f"""<form method="post" action="/produksi/{r['id']}/ubah" class="baris">
                      <input name="qty" value="{r['qty']}" inputmode="numeric"><button class="kecil">Ubah</button></form>
                      <form method="post" action="/produksi/{r['id']}/hapus" onsubmit="return confirm('Hapus batch ini?')">
                      <button class="kecil hapus">Hapus</button></form>"""
                else:
                    aksi = str(r["qty"])
                isi += f'<tr><td>{r["created"][11:16]}</td><td>{e(r["kue"])}</td><td>{e(r["oleh"])}</td><td class="n">{aksi}</td></tr>'
            isi += "</table></div>"
        self.page("Input produksi", isi)

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
        if not rows:
            isi += '<div class="peringatan">Belum ada produksi di tanggal ini.</div>'
        else:
            isi += f'<form class="kartu" method="post" action="/sisa"><input type="hidden" name="tanggal" value="{tgl}">'
            isi += '<table><tr><th>Kue</th><th class="n">Dibuat</th><th class="n">Sisa</th></tr>'
            for r in rows:
                val = "" if r["sisa"] is None else r["sisa"]
                isi += f"""<tr><td>{e(r['name'])}</td><td class="n">{r['dibuat']}</td>
                  <td class="n"><input name="sisa_{r['id']}" value="{val}" inputmode="numeric" placeholder="0" style="width:90px"></td></tr>"""
            isi += '</table><p class="muted">Kosong dihitung 0. Sisa dibagikan, jadi dicatat sebagai kerugian.</p>'
            isi += '<button class="utama">Simpan sisa</button></form>'
        self.page("Input sisa saat tutup", isi)

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
        isi = form_tanggal("/laporan", tgl) + f'<p class="muted">{tgl_indo(tgl)}</p>'
        if not lp["items"]:
            return self.page("Laporan harian", isi + '<div class="peringatan">Belum ada produksi di tanggal ini.</div>')
        if lp["sisa_belum"]:
            isi += (f'<div class="peringatan">Sisa belum diisi untuk: {e(", ".join(lp["sisa_belum"]))}. '
                    f'Sampai diisi, semua kue dianggap terjual. <a href="/sisa?tanggal={tgl}">Isi sisa</a></div>')
        laku = f'{e(lp["laku"]["nama"])} ({lp["laku"]["terjual"]})' if lp["laku"] else "-"
        bsisa = f'{e(lp["banyak_sisa"]["nama"])} ({lp["banyak_sisa"]["sisa"]})' if lp["banyak_sisa"] else "-"
        kartu = [("Omzet", rupiah(t["omzet"])), ("Untung*", rupiah(t["untung"])),
                 ("Kerugian dari sisa", rupiah(t["kerugian"])), ("Untung setelah sisa*", rupiah(t["untung_bersih"])),
                 ("Total terjual", t["terjual"]), ("Total sisa", t["sisa"]),
                 ("Paling laku", laku), ("Paling banyak sisa", bsisa)]
        isi += '<div class="ringkas">' + "".join(f"<div><small>{k}</small><strong>{v}</strong></div>" for k, v in kartu)
        isi += "</div>"
        isi += '<p class="muted">*Untung = omzet &minus; (terjual &times; harga modal). Untung setelah sisa = untung &minus; kerugian dari sisa. Rumus untung masih menunggu konfirmasi pemilik.</p>'
        isi += """<div class="kartu scroll"><table><tr><th>Kue</th><th class="n">Dibuat</th><th class="n">Sisa</th>
          <th class="n">Terjual</th><th class="n">Harga jual</th><th class="n">Omzet</th><th class="n">Untung</th>
          <th class="n">Kerugian</th></tr>"""
        for i in lp["items"]:
            s = i["sisa"] if i["sisa_diisi"] else "?"
            isi += f"""<tr><td>{e(i['nama'])}</td><td class="n">{i['dibuat']}</td><td class="n">{s}</td>
              <td class="n">{i['terjual']}</td><td class="n">{rupiah(i['harga_jual'])}</td><td class="n">{rupiah(i['omzet'])}</td>
              <td class="n">{rupiah(i['untung'])}</td><td class="n">{rupiah(i['kerugian'])}</td></tr>"""
        isi += f"""<tr><th>Total</th><th class="n">{t['dibuat']}</th><th class="n">{t['sisa']}</th><th class="n">{t['terjual']}</th>
          <th></th><th class="n">{rupiah(t['omzet'])}</th><th class="n">{rupiah(t['untung'])}</th>
          <th class="n">{rupiah(t['kerugian'])}</th></tr></table></div>"""
        self.page("Laporan harian", isi)

    # --- kue (admin) ---

    def kue(self):
        rows = self.db.execute("SELECT * FROM cakes ORDER BY active DESC, name").fetchall()
        isi = """<form class="kartu" method="post" action="/kue"><b>Tambah kue</b>
          <label>Nama</label><input name="name" required>
          <label>Harga jual</label><input name="price_sell" inputmode="numeric" required placeholder="mis. 5000">
          <label>Harga modal</label><input name="price_cost" inputmode="numeric" required placeholder="mis. 3000">
          <button class="utama">Tambah kue</button></form>"""
        for k in rows:
            cek = "checked" if k["active"] else ""
            isi += f"""<form class="kartu" method="post" action="/kue/{k['id']}/ubah">
              <label>Nama</label><input name="name" value="{e(k['name'])}" required>
              <label>Harga jual</label><input name="price_sell" value="{k['price_sell']}" inputmode="numeric" required>
              <label>Harga modal</label><input name="price_cost" value="{k['price_cost']}" inputmode="numeric" required>
              <label style="font-weight:400"><input type="checkbox" name="active" value="1" {cek} style="width:auto"> Masih dijual</label>
              <button class="kecil">Simpan perubahan</button>
              <span class="muted">Ganti harga hanya berlaku untuk batch berikutnya.</span></form>"""
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
        isi = """<form class="kartu" method="post" action="/pengguna"><b>Tambah karyawan</b>
          <label>Nama</label><input name="name" required>
          <label>Username</label><input name="username" required autocapitalize="none">
          <label>Password (min. 6 huruf)</label><input name="password" type="password" required minlength="6">
          <button class="utama">Tambah karyawan</button></form>"""
        isi += '<div class="kartu scroll"><table><tr><th>Nama</th><th>Username</th><th>Peran</th><th>Password baru</th></tr>'
        for u in rows:
            isi += f"""<tr><td>{e(u['name'])}</td><td>{e(u['username'])}</td><td>{u['role']}</td><td>
              <form method="post" action="/pengguna/{u['id']}/password" class="baris">
              <input name="password" type="password" minlength="6" required placeholder="baru" style="width:110px">
              <button class="kecil">Ganti</button></form></td></tr>"""
        isi += '</table><p class="muted">Versi 1: karyawan hanya bisa input produksi. Hak akses per orang menyusul di fase 2.</p></div>'
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
        isi = '<div class="kartu scroll"><table><tr><th>Waktu</th><th>Oleh</th><th>Aksi</th><th>Detail</th></tr>'
        isi += "".join(f'<tr><td>{r["at"][:16]}</td><td>{e(r["name"] or "-")}</td><td>{e(r["action"])}</td>'
                       f'<td>{e(r["detail"])}</td></tr>' for r in rows)
        self.page("Riwayat perubahan", isi + "</table></div>")


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
