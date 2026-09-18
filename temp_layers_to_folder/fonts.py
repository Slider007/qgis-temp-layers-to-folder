"""Поиск файлов шрифтов по названию семейства и проверка их лицензии
(без внешних библиотек).

Имена семейств и текст лицензии читаются прямо из таблиц name и OS/2
файлов TTF/OTF/TTC.
"""

import os
import re
import struct
import sys

FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")


def font_dirs():
    """Папки со шрифтами на этом компьютере."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        dirs = ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    elif sys.platform.startswith("win"):
        dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
        if os.environ.get("LOCALAPPDATA"):
            dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts"))
    else:
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts",
                os.path.join(home, ".local", "share", "fonts"), os.path.join(home, ".fonts")]
    try:  # шрифты, установленные через QGIS (3.28+)
        from qgis.core import QgsApplication
        dirs.append(QgsApplication.fontManager().userFontDirectory())
    except Exception:  # noqa: BLE001
        pass
    return [d for d in dirs if d and os.path.isdir(d)]


# name ID: 1 — семейство, 16 — «типографское» семейство, 0 — копирайт,
# 13 — описание лицензии, 14 — адрес лицензии
_FAMILY_IDS, _LICENSE_IDS = (1, 16), (0, 13, 14)


def _name_table(fh, offset, ids):
    fh.seek(offset)
    _fmt, count, string_offset = struct.unpack(">HHH", fh.read(6))
    records = [struct.unpack(">HHHHHH", fh.read(12)) for _ in range(min(count, 2000))]
    names = {}
    for platform, _enc, _lang, name_id, length, rec_offset in records:
        if name_id not in ids:
            continue
        fh.seek(offset + string_offset + rec_offset)
        raw = fh.read(length)
        if platform in (0, 3):
            text = raw.decode("utf-16-be", "ignore")
        elif platform == 1:
            text = raw.decode("mac_roman", "ignore")
        else:
            continue
        text = text.strip("\x00 ").strip()
        if text:
            names.setdefault(name_id, set()).add(text)
    return names


def read_font(path):
    """(семейства, лицензия, копирайт, fsType) из файла шрифта."""
    families, license_text, copyright_text, fs_type = set(), [], [], 0
    try:
        with open(path, "rb") as fh:
            tag = fh.read(4)
            if tag in (b"ttcf",):
                fh.read(4)
                count = struct.unpack(">I", fh.read(4))[0]
                starts = struct.unpack(">%dI" % min(count, 256), fh.read(4 * min(count, 256)))
            else:
                starts = (0,)
            for start in starts:
                fh.seek(start + 4)
                num_tables = struct.unpack(">H", fh.read(2))[0]
                fh.seek(start + 12)
                tables = {}
                for _ in range(min(num_tables, 512)):
                    table, _checksum, table_offset, _length = struct.unpack(">4sIII", fh.read(16))
                    tables[table] = table_offset
                if b"name" in tables:
                    names = _name_table(fh, tables[b"name"], _FAMILY_IDS + _LICENSE_IDS)
                    for i in _FAMILY_IDS:
                        families |= names.get(i, set())
                    license_text += sorted(names.get(13, ())) + sorted(names.get(14, ()))
                    copyright_text += sorted(names.get(0, ()))
                if b"OS/2" in tables:
                    fh.seek(tables[b"OS/2"] + 8)
                    fs_type |= struct.unpack(">H", fh.read(2))[0]
    except (OSError, struct.error):
        return set(), "", "", 0
    return families, " ".join(license_text), " ".join(copyright_text), fs_type


def read_families(path):
    """Названия семейств, записанные в файле шрифта."""
    return read_font(path)[0]


FREE, PAID, UNKNOWN = "free", "paid", "unknown"

# Открытые лицензии, разрешающие передавать шрифт
_FREE_LICENSE = re.compile(
    r"open font licen[cs]e|\bofl\b|scripts\.sil\.org|openfontlicense\.org|apache licen[cs]e|"
    r"apache\.org/licenses|ubuntu font licen[cs]e|gnu general public licen[cs]e|\bgnu gpl\b|"
    r"public domain|\bcc0\b|creativecommons\.org/publicdomain|\bmit licen[cs]e\b|"
    r"bitstream vera|gust font licen[cs]e|ipa font licen[cs]e|arphic public licen[cs]e",
    re.IGNORECASE)


def license_kind(license_text, copyright_text, fs_type):
    """FREE — открытая лицензия (в тексте лицензии или копирайта); PAID — в файле
    лицензия с ограничениями или запрет на передачу (fsType: 2 — ограничено,
    4 — только просмотр и печать); UNKNOWN — лицензия в файле не указана."""
    if _FREE_LICENSE.search("{} {}".format(license_text, copyright_text)):
        return FREE
    if license_text.strip() or fs_type & 0x0006:
        return PAID
    return UNKNOWN


def _key(family):
    """«ALS Arc», «ALSArc» и «als-arc» — одно семейство."""
    return "".join(ch for ch in family.lower() if ch not in " -_")


def find_fonts(families, dirs=None):
    """Раскладывает семейства по лицензии.

    Возвращает {"free": {семейство: [файлы]}, "paid": [...], "unknown": [...],
    "missing": [...]}: копировать можно только free.
    """
    wanted = {_key(f): f for f in families if f}
    files = {}  # ключ семейства → [(путь, вид лицензии)]
    for folder in (dirs if dirs is not None else font_dirs()):
        for root, _subdirs, names in os.walk(folder):
            for name in names:
                if not name.lower().endswith(FONT_EXTS):
                    continue
                path = os.path.join(root, name)
                fams, license_text, copyright_text, fs_type = read_font(path)
                keys = {_key(f) for f in fams} & set(wanted)
                if keys:
                    kind = license_kind(license_text, copyright_text, fs_type)
                    for key in keys:
                        files.setdefault(key, []).append((path, kind))
    result = {"free": {}, "paid": [], "unknown": [], "missing": []}
    for key, family in sorted(wanted.items(), key=lambda kv: kv[1].lower()):
        found = files.get(key, [])
        free = sorted({p for p, kind in found if kind == FREE})
        if free:
            result["free"][family] = free
        elif any(kind == PAID for _p, kind in found):
            result["paid"].append(family)
        elif found:
            result["unknown"].append(family)
        else:
            result["missing"].append(family)
    return result
