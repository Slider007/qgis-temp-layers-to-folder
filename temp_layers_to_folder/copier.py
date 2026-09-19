"""Сборка со структурой папок: исходные файлы слоёв копируются в data_all как
есть — со всеми сопутствующими файлами, контейнер (GeoPackage, SQLite, GDB,
NetCDF…) один раз на все свои слои, — и слои переключаются на копии.

Куда ложится файл (PROJECT_DIR — папка файла проекта):
1. PROJECT_DIR/data/…        → data_all/<путь внутри data>
2. прямо в PROJECT_DIR       → data_all/<имя файла>
3. другая папка проекта      → data_all/<путь от PROJECT_DIR>
4. вне PROJECT_DIR           → data_all/external_links/<обрезанный путь>/<имя файла>

Обрезка внешних путей: путь считается от домашней папки, а вне неё — от корня
диска, сетевой папки или тома (имя диска становится первой папкой: «D», «YD»,
«_server_share»); системные папки (Windows, AppData, /usr…) выбрасываются;
остаются последние MAX_EXTERNAL_DEPTH папок.

Разделение по типу данных (SPLIT_BY_TYPE): растры, облака точек и сетки
раскладываются по тем же правилам, но в своих папках — data_all/raster/…,
data_all/pointcloud/…, data_all/mesh/…. Контейнер, в котором есть и растр, и
вектор (GeoPackage, NetCDF, GDB), или который читают слои разных типов, — в
общем дереве. Уже лежащее в data_all остаётся на своём месте.
"""

import ntpath
import os
import re
import shutil

from qgis.core import QgsProviderRegistry

from . import saver

DATA_SUBDIR = "data"
EXTERNAL_DIR = "external_links"
MAX_EXTERNAL_DEPTH = 3
SPLIT_BY_TYPE = True
# папки типов данных внутри data_all; вектор — в общем дереве
TYPE_DIRS = {"raster": "raster", "pointcloud": "pointcloud", "mesh": "mesh"}
# облака точек и сетки не переводятся в другой формат — только копируются как есть
AS_IS_ONLY = ("pointcloud", "mesh")
SYSTEM_DIRS = {"windows", "program files", "program files (x86)", "programdata", "appdata",
               "usr", "etc", "opt", "bin", "sbin", "private", "var", "tmp", "system", "library"}

# провайдеры, у которых источник — файл или папка на диске (vpc — чтобы назвать причину отказа)
COPY_PROVIDERS = ("ogr", "gdal", "delimitedtext", "spatialite", "pdal", "copc", "ept", "vpc", "mdal")
# из архива на диске читать можно; /vsicurl/, /vsis3/ и т.п. — это интернет
_LOCAL_VSI = ("", "/vsizip/", "/vsitar/", "/vsigzip/")
# NETCDF:"файл":переменная, HDF5:"файл"://путь
_SUBDATASET = re.compile(r'^([A-Za-z0-9_]+):"(.+)":(.*)$')

# сопутствующие файлы: «<имя без расширения>.<расширение>»
_SIDECARS = {
    ".shp": {".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx", ".qix", ".fix",
             ".aih", ".ain", ".atx", ".ixs", ".mxs"},
    ".tab": {".dat", ".id", ".map", ".ind"},
    ".mif": {".mid"},
    ".csv": {".csvt", ".prj"},
}
_RASTER_SIDECARS = {".tfw", ".tifw", ".tfwx", ".jgw", ".jpgw", ".jpw", ".pgw", ".pngw", ".gfw",
                    ".gifw", ".bpw", ".bmpw", ".j2w", ".wld", ".rrd", ".aux", ".prj", ".hdr",
                    ".blw", ".stx", ".clr"}
_STYLE_SIDECARS = {".qml", ".qmd"}
# источник, который ссылается на другие файлы, — как есть не скопировать: VRT
# переводится в формат из окна, виртуальное облако точек (.vpc) не собирается
_NOT_COPYABLE = {".vrt": "ссылается на другие файлы",
                 ".vpc": "виртуальное облако точек ссылается на другие файлы — как есть не скопировать"}
# файлы, где могут лежать и растры, и векторы
CONTAINERS = {".gpkg", ".sqlite", ".nc", ".gdb"}


# ------------------------------------------------------------ куда класть

def _parts(rel):
    return [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]


def _rel_inside(path, anchor, pm):
    """Путь path относительно anchor (как список папок) или None, если он вне."""
    if pm is os.path:
        rel = saver.relative_inside(path, anchor)
        return None if rel is None else _parts(rel)
    a, f = pm.normpath(anchor), pm.normpath(path)
    try:
        if pm.normcase(pm.commonpath([a, f])) != pm.normcase(a):
            return None
    except ValueError:  # разные диски
        return None
    return _parts(pm.relpath(f, a))


def external_subdir(src_dir, home=None, max_depth=MAX_EXTERNAL_DEPTH, pm=os.path):
    """Подпапка в external_links для файла из папки src_dir вне проекта.

    «C:\\Users\\roman\\Desktop\\ЛЭП\\узел7» (домашняя C:\\Users\\roman) → «Desktop/ЛЭП/узел7»;
    «D:\\GIS» → «D/GIS»; «\\\\server\\share\\a» → «_server_share/a»; на macOS
    «/Volumes/YD/GIS» → «YD/GIS». pm — модуль путей (ntpath для путей Windows).
    """
    home = home if home is not None else os.path.expanduser("~")
    prefix = []
    parts = _rel_inside(src_dir, home, pm) if home else None
    if parts is None:
        drive, tail = pm.splitdrive(pm.normpath(src_dir))
        parts = _parts(tail)
        if drive.startswith(("\\\\", "//")):
            prefix = ["_" + "_".join(_parts(drive))]
        elif drive:
            prefix = [drive.rstrip(":")]
        elif parts[:1] == ["Volumes"] and len(parts) > 1:  # macOS: /Volumes/<диск>
            prefix, parts = [parts[1]], parts[2:]
        elif parts[:1] == ["mnt"] and len(parts) > 1:  # Linux: /mnt/<диск>
            prefix, parts = [parts[1]], parts[2:]
        elif parts[:1] == ["media"] and len(parts) > 2:  # Linux: /media/<пользователь>/<диск>
            prefix, parts = [parts[2]], parts[3:]
    parts = [p for p in parts if p.lower() not in SYSTEM_DIRS]
    if max_depth is not None and len(parts) > max_depth:
        parts = parts[len(parts) - max_depth:]
    return "/".join(prefix + parts)


def target_subdir(src_dir, project_dir, data_dir=None, home=None, max_depth=MAX_EXTERNAL_DEPTH,
                  pm=os.path, kind=""):
    """Подпапка data_all для файла из папки src_dir (пустая строка — сама data_all).
    kind — тип данных («raster», «pointcloud», «mesh»): такие файлы ложатся в
    свою папку типа; пустой или «vector» — в общее дерево."""
    if data_dir:
        parts = _rel_inside(src_dir, data_dir, pm)
        if parts is not None:  # уже в data_all — остаётся на своём месте
            return "/".join(parts)
    parts = _rel_inside(src_dir, project_dir, pm)
    if parts is not None:
        if parts[:1] and parts[0].lower() == DATA_SUBDIR:
            parts = parts[1:]  # data/… → data_all/…
    else:
        parts = [EXTERNAL_DIR] + _parts(external_subdir(src_dir, home, max_depth, pm))
    return "/".join([TYPE_DIRS[kind]] + parts if kind in TYPE_DIRS else parts)


# ------------------------------------------------------------ источник слоя

class Source:
    """Файл или папка на диске, из которой читает слой, и как собрать адрес
    слоя с другим путём (остальные параметры адреса не меняются)."""

    def __init__(self, layer, path, rebuild):
        self.layer = layer
        self.path = path
        self._rebuild = rebuild

    def uri(self, new_path):
        return self._rebuild(new_path)


_NOT_A_FILE = "источник слоя — не файл на диске"


def _source(layer):
    """(Source или None, почему файл слоя не скопировать как есть)."""
    provider = layer.providerType()
    if provider not in COPY_PROVIDERS:
        return None, _NOT_A_FILE
    reg = QgsProviderRegistry.instance()
    try:
        parts = reg.decodeUri(provider, layer.source())
    except Exception:
        return None, _NOT_A_FILE
    path = parts.get("path") or ""
    if (parts.get("vsiPrefix") or "") not in _LOCAL_VSI:
        return None, _NOT_A_FILE
    inner = ""  # файл внутри копируемой папки: ept.json облака точек EPT
    m = _SUBDATASET.match(path)
    if m:
        driver, path, rest = m.groups()

        def rebuild(new):
            return reg.encodeUri(provider, dict(parts, path='{}:"{}":{}'.format(driver, new, rest)))
    else:
        if provider == "ept":  # облако EPT — папка с ept.json и данными
            path, inner = os.path.split(path)

        def rebuild(new):
            return reg.encodeUri(provider, dict(parts, path=os.path.join(new, inner) if inner else new))
    if not path or not os.path.isabs(path) or not os.path.exists(path):
        return None, _NOT_A_FILE
    if os.path.isfile(path) and os.path.splitext(path)[1].lower() in _NOT_COPYABLE:
        return None, _NOT_COPYABLE[os.path.splitext(path)[1].lower()]
    if provider == "mdal" and layer.dataProvider() is not None and layer.dataProvider().extraDatasets():
        # QGIS 3.40 не даёт заменить подключённые к сетке файлы: они остались бы по старому пути
        return None, "к сетке подключены дополнительные наборы данных — QGIS не даёт переключить их на копии"
    return Source(layer, path, rebuild), ""


def layer_source(layer):
    """Source для слоя из локального файла или папки, иначе None."""
    return _source(layer)[0]


def copy_obstacle(layer):
    """Почему файл слоя нельзя скопировать как есть; пустая строка — можно."""
    return _source(layer)[1]


def data_type(kind):
    """Тип данных слоя для раскладки по kind из saver.classify: «vector» —
    вектор (и слой в памяти), «raster», «pointcloud», «mesh»."""
    return "vector" if kind in ("vector", "memory") else kind


def container_types(path):
    """Типы данных внутри контейнера (GeoPackage, NetCDF, GDB, SQLite):
    {"raster", "vector"} или их часть; для других файлов — пустое множество."""
    ext = os.path.splitext(os.path.normpath(path))[1].lower()
    if ext not in CONTAINERS:
        return set()
    from osgeo import gdal

    found = set()
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        for flag, kind in ((gdal.OF_RASTER, "raster"), (gdal.OF_VECTOR, "vector")):
            try:
                ds = gdal.OpenEx(path, flag | gdal.OF_READONLY)
            except Exception:  # noqa: BLE001 — GDAL с исключениями: не открылся в этом режиме
                ds = None
            if ds is None:
                continue
            if kind == "raster" and (ds.RasterCount or ds.GetMetadata("SUBDATASETS")):
                found.add(kind)
            elif kind == "vector" and ds.GetLayerCount():
                found.add(kind)
            ds = None
    finally:
        gdal.PopErrorHandler()
    return found


def unit_type(types, path):
    """Папка типа для файла, который читают слои с типами данных types: у
    одного типа — его папка, у разных типов (контейнер с растром и вектором) —
    общее дерево. Растр из контейнера, где есть и вектор, — тоже в общее дерево."""
    if len(types) != 1:
        return ""
    kind = next(iter(types))
    if kind == "raster" and "vector" in container_types(path):
        return ""
    return kind if kind in TYPE_DIRS else ""


def companion_names(path, raster=False, pointcloud=False):
    """Имена файлов рядом с path, которые копируются вместе с ним: сам файл,
    «<файл>.*» (x.tif.aux.xml, x.shp.xml, x.tif.ovr, x.tif.vat.dbf), журналы
    SQLite (-wal, -shm), сопутствующие файлы формата и стили (.qml, .qmd), у
    облака точек — индекс QGIS «<имя>.copc.laz»."""
    folder, main = os.path.split(path)
    stem, ext = os.path.splitext(main)
    ext = ext.lower()
    extra = set(_SIDECARS.get(ext, ())) | _STYLE_SIDECARS
    whole = {stem.lower() + ".aux.xml"}
    if raster:
        extra |= _RASTER_SIDECARS
    if pointcloud:
        whole.add(stem.lower() + ".copc.laz")
    names = []
    for name in sorted(os.listdir(folder)):
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        low = name.lower()
        if name == main or name.startswith(main + ".") or name in (main + "-wal", main + "-shm",
                                                                    main + "-journal"):
            names.append(name)
        elif low.startswith(stem.lower() + ".") and (os.path.splitext(low)[1] in extra or low in whole):
            names.append(name)
    return names


def _renamed(name, main, new_main):
    """Имя сопутствующего файла для нового имени основного: x.shp → x_2.shp, x.dbf → x_2.dbf."""
    if name.startswith(main):
        return new_main + name[len(main):]
    stem, new_stem = os.path.splitext(main)[0], os.path.splitext(new_main)[0]
    return new_stem + name[len(stem):]


# ------------------------------------------------------------ копирование

def _key(path):
    return os.path.normcase(os.path.realpath(path))


def copy_layers(project, items, data_dir, project_dir, progress=None, is_cancelled=None,
                home=None, max_depth=MAX_EXTERNAL_DEPTH, split_by_type=SPLIT_BY_TYPE):
    """Копирует файлы слоёв items = [(layer, kind, temporary)] в data_dir по
    правилам раскладки и переключает на них слои. Слои без Source сюда не
    передаются. split_by_type — растры, облака точек и сетки в свои папки
    (raster/, pointcloud/, mesh/). Возвращает результаты в формате saver.save_layers."""
    real_data = os.path.realpath(data_dir)
    sources, types = {}, {}  # {id слоя: Source}, {исходный файл/папка: типы данных его слоёв}
    for layer, kind, _temporary in items:
        src = sources[layer.id()] = layer_source(layer)
        if src is not None:
            types.setdefault(_key(src.path), set()).add(data_type(kind))
    copies = {}  # {исходный файл/папка: (копия, замечание)}
    taken = {}  # {папка: занятые в этом запуске имена (в нижнем регистре)}
    results = []
    total = len(items)
    for i, (layer, kind, _temporary) in enumerate(items):
        if is_cancelled and is_cancelled():
            raise saver.Cancelled()
        if progress:
            progress(i, total, layer.name())
        res = {"id": layer.id(), "name": layer.name(), "ok": False, "path": "", "message": "",
               "uri": "", "provider": layer.providerType(), "crs": None}
        try:
            src = sources[layer.id()]
            if src is None:
                raise RuntimeError(_NOT_A_FILE)
            key = _key(src.path)
            if key not in copies:
                copies[key] = _copy_unit(src.path, types[key], data_dir, real_data, project_dir, taken,
                                         home, max_depth,
                                         unit_type(types[key], src.path) if split_by_type else "")
            dest, note = copies[key]
            uri = src.uri(dest)
            old, crs = layer.source(), layer.crs()
            # СК, назначенная слою вручную (в файле её нет: LAS, растр без .prj), —
            # та же и у копии: QGIS перечитал бы её из файла
            if not saver._repoint(layer, uri, layer.providerType(), project, crs if crs.isValid() else None):
                saver._repoint(layer, old, layer.providerType(), project, crs if crs.isValid() else None)
                raise RuntimeError("файл скопирован, но слой не открылся из копии — оставлен как был")
            res.update(ok=True, path=dest, uri=uri, message=note)
        except saver.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 — продолжаем с остальными слоями
            res["message"] = str(e)
        results.append(res)
    if progress:
        progress(total, total, "")
    if any(r["ok"] for r in results):
        project.setDirty(True)
    return results


def _copy_unit(path, types, data_dir, real_data, project_dir, taken, home, max_depth, kind):
    """Копирует файл со спутниками или папку целиком в подпапку по правилам
    раскладки (kind — папка типа данных или пустая строка); возвращает (путь
    копии, замечание). types — типы данных слоёв, читающих файл."""
    is_dir = os.path.isdir(path)
    if is_dir and saver.relative_inside(real_data, path) is not None:
        raise RuntimeError("папка-источник содержит саму data_all — скопировать её нельзя")
    sub = target_subdir(os.path.dirname(os.path.normpath(path)), project_dir, data_dir, home, max_depth,
                        kind=kind)
    dest_dir = os.path.join(data_dir, *sub.split("/")) if sub else data_dir
    main = os.path.basename(os.path.normpath(path))
    names = [] if is_dir else companion_names(path, "raster" in types, "pointcloud" in types)
    busy = taken.setdefault(os.path.normcase(os.path.abspath(dest_dir)), set())
    stem, ext = (main, "") if is_dir else os.path.splitext(main)

    def is_taken(candidate):
        new_main = candidate + ext
        if new_main.lower() in busy:
            return True
        return any(os.path.exists(os.path.join(dest_dir, _renamed(n, main, new_main)))
                   for n in (names or [main]))

    new_main = saver._unique(stem, is_taken) + ext
    busy.add(new_main.lower())
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, new_main)
    if is_dir:
        shutil.copytree(path, dest)
    else:
        for name in names:
            shutil.copy2(os.path.join(os.path.dirname(path), name),
                         os.path.join(dest_dir, _renamed(name, main, new_main)))
    note = "имя «{}» уже занято — скопирован как «{}»".format(main, new_main) if new_main != main else ""
    return dest, note
