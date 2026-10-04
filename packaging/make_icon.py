"""Render battbench/resources/battbench.svg into packaging/battbench.ico (16-256 px, PNG-compressed entries)."""
import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG = os.path.join(ROOT, 'battbench', 'resources', 'battbench.svg')
ICO = os.path.join(ROOT, 'packaging', 'battbench.ico')
SIZES = (16, 24, 32, 48, 64, 128, 256)


def png(renderer, size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, 'PNG')
    return bytes(data)


def main():
    QGuiApplication(sys.argv)
    renderer = QSvgRenderer(SVG)
    images = [(size, png(renderer, size)) for size in SIZES]
    offset = 6 + 16 * len(images)
    out = struct.pack('<HHH', 0, 1, len(images))
    for size, data in images:
        out += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    out += b''.join(data for _, data in images)
    with open(ICO, 'wb') as f:
        f.write(out)
    print(f'{ICO}: {len(images)} sizes')


if __name__ == '__main__':
    main()
