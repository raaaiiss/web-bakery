#!/usr/bin/env python3
"""Membuat ikon aplikasi (PNG) tanpa pustaka tambahan: gambar donat sederhana di atas warna toko.

Jalankan dari folder proyek:  python3 tools/buat_ikon.py
Hasil: static/ikon/ikon-192.png, ikon-512.png, ikon-maskable-512.png, apple-touch-icon-180.png
Mau logo sendiri? Ganti file PNG di static/ikon/ dengan ukuran yang sama.
"""
import math
import os
import struct
import zlib

LATAR = (0x92, 0x40, 0x0E)      # cokelat kulit roti (tombol utama aplikasi)
ADONAN = (0xE3, 0xA8, 0x57)     # adonan donat
GULA = (0xFF, 0xF4, 0xE3)       # lapisan gula krem
TABUR = [(0xE0, 0x7A, 0x8B), (0x6F, 0xA3, 0x5A), (0xF2, 0xC9, 0x4C), (0xFF, 0xFF, 0xFF)]
SAMPEL = 3                      # 3x3 titik per piksel supaya tepi halus


def warna_di(x, y):
    """x, y dalam 0..1 (pusat 0.5). Semua bentuk di dalam lingkaran radius 0.33: aman untuk ikon maskable (0.40)."""
    dx, dy = x - 0.5, y - 0.5
    r = math.hypot(dx, dy)
    sudut = math.atan2(dy, dx)
    if 0.125 <= r <= 0.33:
        tepi_gula = 0.295 + 0.014 * math.sin(9 * sudut)
        if 0.145 <= r <= tepi_gula:
            for i in range(14):
                a = i * 2 * math.pi / 14 + 0.3 * (i % 3)
                rr = 0.185 + 0.06 * ((i * 7) % 3) / 2
                tx, ty = 0.5 + rr * math.cos(a), 0.5 + rr * math.sin(a)
                # tabur berbentuk batang pendek miring
                ux, uy = math.cos(a + 1.1), math.sin(a + 1.1)
                px, py = x - tx, y - ty
                sepanjang = px * ux + py * uy
                melintang = -px * uy + py * ux
                if abs(sepanjang) <= 0.022 and abs(melintang) <= 0.007:
                    return TABUR[i % len(TABUR)]
            return GULA
        return ADONAN
    return LATAR


def gambar(ukuran):
    baris = []
    n = SAMPEL * SAMPEL
    for py in range(ukuran):
        b = bytearray([0])  # filter PNG: none
        for px in range(ukuran):
            r = g = bl = 0
            for sy in range(SAMPEL):
                for sx in range(SAMPEL):
                    c = warna_di((px + (sx + 0.5) / SAMPEL) / ukuran, (py + (sy + 0.5) / SAMPEL) / ukuran)
                    r += c[0]; g += c[1]; bl += c[2]
            b += bytes((r // n, g // n, bl // n, 255))
        baris.append(bytes(b))
    return png(ukuran, ukuran, b"".join(baris))


def png(w, h, data):
    def potong(jenis, isi):
        return struct.pack(">I", len(isi)) + jenis + isi + struct.pack(">I", zlib.crc32(jenis + isi) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + potong(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + potong(b"IDAT", zlib.compress(data, 9)) + potong(b"IEND", b""))


def main():
    folder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "ikon")
    os.makedirs(folder, exist_ok=True)
    besar = gambar(512)
    for nama, isi in [("ikon-192.png", gambar(192)), ("ikon-512.png", besar),
                      ("ikon-maskable-512.png", besar), ("apple-touch-icon-180.png", gambar(180))]:
        with open(os.path.join(folder, nama), "wb") as fh:
            fh.write(isi)
        print(nama, len(isi), "byte")


if __name__ == "__main__":
    main()
