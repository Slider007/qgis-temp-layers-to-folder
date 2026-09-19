"""Поиск и сохранение слоёв проекта QGIS (без зависимостей от интерфейса)."""

import os
import re
import shutil

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDataProvider,
    QgsFields,
    QgsMapLayerStyle,
    QgsProcessingUtils,
    QgsProviderRegistry,
    QgsRasterLayer,
    QgsVectorFileWriter,
    QgsVectorLayer,
)


def _enum(owner, enum_name, member):
    """Значение перечисления и в новом (scoped), и в старом стиле Qt/QGIS."""
    scoped = getattr(owner, enum_name, None)
    if scoped is not None and hasattr(scoped, member):
        return getattr(scoped, member)
    return getattr(owner, member)


CREATE_FILE = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteFile")
CREATE_LAYER = _enum(QgsVectorFileWriter, "ActionOnExistingFile", "CreateOrOverwriteLayer")
WRITER_OK = _enum(QgsVectorFileWriter, "WriterError", "NoError")
try:
    WKT_GDAL = Qgis.CrsWktVariant.PreferredGdal
except AttributeError:
    WKT_GDAL = QgsCoordinateReferenceSystem.WKT_PREFERRED_GDAL
try:
    DERIVED_ORIGINS = (Qgis.FieldOrigin.Join, Qgis.FieldOrigin.Expression)
except AttributeError:
    DERIVED_ORIGINS = (_enum(QgsFields, "FieldOrigin", "OriginJoin"),
                       _enum(QgsFields, "FieldOrigin", "OriginExpression"))


FORMATS = [  # первый — формат по умолчанию
    {"key": "gpkg", "label": "GeoPackage — отдельный файл на каждый слой",
     "driver": "GPKG", "ext": "gpkg", "single": False},
    {"key": "gpkg_single", "label": "GeoPackage — все слои в одном файле",
     "driver": "GPKG", "ext": "gpkg", "single": True},
    {"key": "shp", "label": "ESRI Shapefile",
     "driver": "ESRI Shapefile", "ext": "shp", "single": False},
    {"key": "geojson", "label": "GeoJSON",
     "driver": "GeoJSON", "ext": "geojson", "single": False},
]
FORMATS_BY_KEY = {f["key"]: f for f in FORMATS}

# Спутниковые файлы Shapefile: при проверке «файл уже существует» смотрим на все.
_SHP_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qml")

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- поиск слоёв

def layer_path(layer):
    """Путь к файлу слоя (без |layername=… и т.п.) или пустая строка."""
    try:
        parts = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
        path = parts.get("path") or ""
    except Exception:
        path = ""
    if not path:
        path = layer.source().split("|")[0]
    return path if os.path.isfile(path) else ""


def _uri_path(layer):
    try:
        return QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source()).get("path") or ""
    except Exception:
        return ""


def missing_file(layer):
    """Путь к файлу слоя, которого уже нет на диске, иначе пустая строка.

    Такой слой может выглядеть исправным: удалённый файл читается через
    открытый дескриптор, пока проект открыт. Растры и картинки при этом
    теряются, поэтому сохранять и собирать такой слой нельзя."""
    path = _uri_path(layer)
    if not path or path.startswith("/vsi") or not os.path.isabs(path):
        return ""
    return "" if os.path.exists(path) else path


def _processing_temp_dir():
    try:
        return os.path.realpath(QgsProcessingUtils.tempFolder())
    except Exception:
        return ""


def _inside(path, folder):
    if not path or not folder:
        return False
    try:
        return os.path.commonpath([os.path.realpath(path), folder]) == folder
    except ValueError:  # разные диски в Windows
        return False


def _is_temporary(layer, processing_dir):
    try:
        if layer.isTemporary():
            return True
    except Exception:  # noqa: BLE001 — нет метода в старых версиях
        pass
    return _inside(layer_path(layer), processing_dir)


_ONLINE_PROVIDERS = {"wms": "WMS/XYZ", "wcs": "WCS", "arcgismapserver": "ArcGIS"}

# Облака точек и сетки в другие форматы не переводятся: их файлы только
# копируются как есть — при сборке со структурой папок (copier).
_AS_IS_TYPES = {"QgsPointCloudLayer": ("pointcloud", "облако точек"),
                "QgsMeshLayer": ("mesh", "сетка (mesh)")}

_UNSUPPORTED_TYPES = {
    "QgsVectorTileLayer": "векторные тайлы — сохранить нельзя",
    "QgsTiledSceneLayer": "3D-сцена — сохранить нельзя",
}


_FILE_GONE = "файл слоя удалён или перемещён — сохранить нельзя"


def classify(layer, processing_dir=None, copy_as_is=False):
    """(kind, temporary, reason): kind — 'memory', 'vector' или 'raster', а с
    copy_as_is (сборка со структурой папок) ещё 'pointcloud' и 'mesh' — их
    файлы копируются как есть; если слой сохранить нельзя, kind = None, а
    reason объясняет почему."""
    if processing_dir is None:
        processing_dir = _processing_temp_dir()
    if isinstance(layer, QgsVectorLayer):
        if layer.providerType() == "memory":
            return "memory", True, ""
        if not layer.isValid():
            return None, False, "слой недоступен — источник не найден"
        if missing_file(layer):
            return None, False, _FILE_GONE
        return "vector", _is_temporary(layer, processing_dir), ""
    if isinstance(layer, QgsRasterLayer):
        if not layer.isValid():
            return None, False, "слой недоступен — источник не найден"
        if layer.providerType() != "gdal":
            prov = _ONLINE_PROVIDERS.get(layer.providerType(), layer.providerType().upper())
            return None, False, "онлайн-слой {} — сохранить нельзя".format(prov)
        if missing_file(layer):
            return None, False, _FILE_GONE
        return "raster", _is_temporary(layer, processing_dir), ""
    as_is = _AS_IS_TYPES.get(type(layer).__name__)
    if as_is:
        kind, label = as_is
        if not layer.isValid():
            return None, False, "слой недоступен — источник не найден"
        if re.match(r"(?i)^https?://", _uri_path(layer)):
            return None, False, "онлайн-слой ({}) — сохранить нельзя".format(label)
        if missing_file(layer):
            return None, False, _FILE_GONE
        if not copy_as_is:
            return None, False, "{} — только со структурой папок".format(label)
        return kind, False, ""
    return None, False, _UNSUPPORTED_TYPES.get(type(layer).__name__, "этот тип слоя не поддерживается")


def find_layers(project, temporary_only=True, copy_as_is=False):
    """Слои проекта в порядке панели «Слои».

    Возвращает (items, skipped): items = [(layer, kind, temporary)] — что можно
    сохранить; skipped = [(layer, причина)] — что сохранить нельзя (только для
    режима «все слои»). copy_as_is — см. classify.
    """
    processing_dir = _processing_temp_dir()
    ordered = [n.layer() for n in project.layerTreeRoot().findLayers() if n.layer()]
    seen = {l.id() for l in ordered}
    ordered += [l for l in project.mapLayers().values() if l.id() not in seen]

    items, skipped = [], []
    for layer in ordered:
        kind, temporary, reason = classify(layer, processing_dir, copy_as_is)
        if temporary_only:
            if kind and temporary:
                items.append((layer, kind, temporary))
        elif kind:
            items.append((layer, kind, temporary))
        else:
            skipped.append((layer, reason))
    return items, skipped


def find_temporary_layers(project):
    """[(layer, kind, True)] — временные слои проекта."""
    return find_layers(project, temporary_only=True)[0]


_PROVIDER_LABELS = {
    "postgres": "PostGIS", "spatialite": "SpatiaLite", "wfs": "WFS", "oapif": "WFS",
    "delimitedtext": "CSV", "virtual": "виртуальный слой", "mssql": "MS SQL",
    "oracle": "Oracle", "arcgisfeatureserver": "ArcGIS", "gpx": "GPX",
}


def describe(layer, kind, temporary):
    """Короткая подпись источника для списка слоёв."""
    if kind == "memory":
        return "в памяти"
    if temporary:
        return "временный растр" if kind == "raster" else "временный файл"
    ext = os.path.splitext(layer_path(layer))[1].lower()
    if kind == "pointcloud":
        return "облако точек " + ("EPT" if layer.providerType() == "ept" else ext)
    if kind == "mesh":
        return "сетка " + ext
    if kind == "raster":
        return "растр " + ext if ext else "растр"
    if layer.providerType() == "ogr":
        return "файл " + ext if ext else "файл"
    return _PROVIDER_LABELS.get(layer.providerType(), layer.providerType())


def _source_files(project):
    """Файлы, из которых читают слои проекта, — их нельзя перезаписывать."""
    return {os.path.realpath(p) for p in (layer_path(l) for l in project.mapLayers().values()) if p}


def relative_inside(folder, anchor):
    """Путь folder относительно anchor, если folder лежит внутри anchor, иначе None.
    Сначала как записано (ссылки на папки не раскрываются), затем через realpath:
    на macOS /var и /private/var — одна папка."""
    for norm in (lambda p: os.path.normpath(os.path.abspath(p)), os.path.realpath):
        a, f = norm(anchor), norm(folder)
        try:
            if os.path.normcase(os.path.commonpath([a, f])) == os.path.normcase(a):
                return os.path.relpath(f, a)
        except ValueError:  # разные диски в Windows
            pass
    return None


# ---------------------------------------------------------------- имена

def safe_name(name):
    name = _INVALID_CHARS.sub("_", name or "").strip().strip(".")
    return (name or "layer")[:120]


# GDAL не создаёт в GeoPackage таблицу, имя которой начинается с этих знаков
# («- поворотные точки»): «The layer name may not contain special characters».
_GPKG_BAD_START = "`~!@#$%^&*()+-={}|[]\\:\";'<>?,./ "


def gpkg_layer_name(name):
    """Имя таблицы в GeoPackage: без знаков препинания в начале."""
    return name.lstrip(_GPKG_BAD_START) or "layer"


def _existing_gpkg_layers(path):
    if not os.path.exists(path):
        return set()
    try:
        from osgeo import ogr
        ds = ogr.Open(path)
        if ds is None:
            return set()
        names = {ds.GetLayer(i).GetName().lower() for i in range(ds.GetLayerCount())}
        ds = None
        return names
    except Exception:
        return set()


def _unique(base, is_taken):
    candidate, n = base, 2
    while is_taken(candidate):
        candidate = "{}_{}".format(base, n)
        n += 1
    return candidate


# ---------------------------------------------------------------- запись

def _fid_layer_options(layer, driver):
    """GPKG требует уникальный целочисленный fid. Если поле «fid» в слое
    такому не соответствует, выносим FID GeoPackage в отдельную колонку."""
    if driver != "GPKG":
        return []
    names = [f.name() for f in layer.fields()]
    fid_names = [n for n in names if n.lower() == "fid"]
    if not fid_names:
        return []
    idx = layer.fields().indexOf(fid_names[0])
    values = layer.uniqueValues(idx)
    ok = (len(values) == layer.featureCount()
          and all(isinstance(v, int) and not isinstance(v, bool) for v in values))
    if ok:
        return []
    return ["FID={}".format(_unique("fid_gpkg", lambda c: c.lower() in {n.lower() for n in names}))]


def _write_vector(layer, path, driver, layer_name, action, transform_context, ct=None,
                  own_fields_only=False):
    if driver == "GPKG":
        layer_name = gpkg_layer_name(layer_name)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    opts.fileEncoding = "UTF-8"
    opts.layerName = layer_name
    opts.actionOnExistingFile = action
    if ct is not None:
        opts.ct = ct
    if own_fields_only:
        # Слой останется в проекте со своими объединениями и выражениями —
        # их поля в файл не пишем, иначе после переключения они задвоятся.
        fields = layer.fields()
        own = [i for i in range(fields.count()) if fields.fieldOrigin(i) not in DERIVED_ORIGINS]
        if len(own) < fields.count():
            if own:
                opts.attributes = own
            else:
                opts.skipAttributeCreation = True
    layer_options = _fid_layer_options(layer, driver)
    if layer_options:
        opts.layerOptions = layer_options

    created = action == CREATE_FILE and not os.path.exists(path)
    res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, transform_context, opts)
    error, message = res[0], res[1]
    new_file = res[2] if len(res) > 2 and res[2] else path
    new_layer = res[3] if len(res) > 3 and res[3] else layer_name
    if error != WRITER_OK:
        if created:  # пустой файл от неудачной записи не оставляем
            _remove_file_set(path, driver)
        raise RuntimeError(message or "ошибка записи (код {})".format(error))
    return new_file, new_layer


def _remove_file_set(path, driver):
    if driver == "ESRI Shapefile":
        stem = os.path.splitext(path)[0]
        paths = [stem + p for p in _SHP_PARTS if p != ".qml"]
    else:
        paths = [path + s for s in ("", "-wal", "-shm", "-journal")]
    for p in paths:
        if os.path.isfile(p):
            os.remove(p)


def _raster_stem(dest_folder, base, ext, overwrite, taken, protected):
    def is_taken(name):
        if name.lower() in taken:
            return True
        path = os.path.join(dest_folder, name + ext)
        if os.path.realpath(path) in protected:
            return True  # из этого файла читает слой проекта
        return not overwrite and os.path.exists(path)

    return _unique(base, is_taken)


# Форматы, которые безопасно копировать файлом (со «спутниками» по имени).
# Остальное (VRT, растр внутри GeoPackage, NetCDF, архивы) переводим в GeoTIFF.
_COPYABLE_RASTERS = {".tif", ".tiff", ".img", ".asc", ".jp2", ".ecw", ".sdat",
                     ".bil", ".png", ".jpg", ".jpeg", ".gif", ".bmp"}


def _gdal_source(layer):
    return layer.source().split("|")[0]


def _copy_raster(src, dest_folder, base, overwrite, taken, protected):
    """Копирует растр со всеми «спутниками» (.aux.xml, .ovr, .tfw, .prj…)."""
    src_dir, src_file = os.path.split(src)
    src_stem, ext = os.path.splitext(src_file)
    stem = _raster_stem(dest_folder, base, ext, overwrite, taken, protected)
    for name in os.listdir(src_dir):
        if name == src_file or name.startswith(src_file + "."):
            new_name = stem + ext + name[len(src_file):]
        elif os.path.splitext(name)[0] == src_stem:
            new_name = stem + name[len(src_stem):]
        else:
            continue
        full = os.path.join(src_dir, name)
        if os.path.isfile(full):
            shutil.copy2(full, os.path.join(dest_folder, new_name))
    return os.path.join(dest_folder, stem + ext), stem


def _gdal_to_geotiff(src, dest_folder, base, overwrite, taken, protected, crs=None):
    """Пишет растр в GeoTIFF через GDAL: перепроецирует (crs задана) или просто
    переводит формат. Ресемплинг — ближайший сосед."""
    from osgeo import gdal

    stem = _raster_stem(dest_folder, base, ".tif", overwrite, taken, protected)
    dest = os.path.join(dest_folder, stem + ".tif")
    # gdal.Warp в существующий файл дописывает в него, а не заменяет — удаляем заранее
    for old in (dest, dest + ".aux.xml", dest + ".ovr"):
        if os.path.exists(old):
            os.remove(old)
    creation = ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"]
    if crs is not None:
        ds = gdal.Warp(dest, src, options=gdal.WarpOptions(
            format="GTiff", dstSRS=crs.toWkt(WKT_GDAL), resampleAlg="near", creationOptions=creation))
    else:
        ds = gdal.Translate(dest, src, options=gdal.TranslateOptions(format="GTiff", creationOptions=creation))
    if ds is None:
        raise RuntimeError("GDAL: " + (gdal.GetLastErrorMsg() or "не удалось записать растр"))
    ds = None
    return dest, stem


def _save_raster(layer, folder, base, overwrite, taken, protected, crs):
    src = _gdal_source(layer)
    path = layer_path(layer)
    if crs is None and path and os.path.realpath(path) == os.path.realpath(src) \
            and os.path.splitext(path)[1].lower() in _COPYABLE_RASTERS:
        return _copy_raster(path, folder, base, overwrite, taken, protected)
    return _gdal_to_geotiff(src, folder, base, overwrite, taken, protected, crs)


def _crs_label(crs):
    return crs.authid() or crs.description() or "пользовательская СК"


def _target_crs(layer, crs, notes):
    """Целевая СК для слоя или None, если перепроецировать не нужно / нельзя."""
    if crs is None or not crs.isValid() or not layer.isSpatial():
        return None
    if not layer.crs().isValid():
        notes.append("у слоя не задана система координат — сохранён без перепроецирования")
        return None
    if layer.crs() == crs:
        return None
    notes.append("{} → {}".format(_crs_label(layer.crs()), _crs_label(crs)))
    return crs


def _copy_style_to_gpkg(layer, uri, name):
    target = QgsVectorLayer(uri, name, "ogr")
    if not target.isValid():
        return "не удалось открыть сохранённый слой для записи стиля"
    style = QgsMapLayerStyle()
    style.readFromLayer(layer)
    style.writeToLayer(target)
    try:
        err = target.saveStyleToDatabase(name, "", True, "")
    except AttributeError:  # на случай удаления устаревшего метода в новых версиях
        res = target.saveStyleToDatabaseV2(name, "", True, "")
        err = res[-1] if isinstance(res, tuple) else ""
    return err or ""


def _repoint(layer, uri, provider, project, crs=None):
    """Переключает слой проекта на сохранённый файл — стиль, id и место в дереве сохраняются."""
    options = QgsDataProvider.ProviderOptions()
    options.transformContext = project.transformContext()
    layer.setDataSource(uri, layer.name(), provider, options)  # без загрузки стиля по умолчанию
    if not layer.isValid():
        return False
    # Данные уже записаны в crs. Назначаем её явно: иначе пользовательская СК (МСК)
    # после чтения из .prj/GeoPackage может отображаться как «unknown».
    if crs is not None and layer.crs() != crs:
        layer.setCrs(crs)
    return True


def save_layers(project, items, folder, fmt_key, gpkg_name="temporary_layers",
                replace=True, save_styles=True, overwrite=False, crs=None,
                progress=None, is_cancelled=None, own_fields_only=None, subdirs=None, names=None):
    """Сохраняет слои items = [(layer, kind, temporary), ...] в папку folder.

    subdirs — {id слоя: подпапка внутри folder} (см. copier.target_subdir); слои
    без подпапки и общий GeoPackage ложатся в саму folder. names — {id слоя: имя
    файла без расширения} вместо названия слоя.
    crs — система координат для сохранения; None / недействительная — как у слоя.
    Исходные данные постоянных слоёв не меняются: правки из режима
    редактирования попадают только в копию, а файлы, из которых читают слои
    проекта, не перезаписываются даже при overwrite=True.

    own_fields_only — писать только собственные поля слоя (без объединений и
    выражений); по умолчанию так делается при replace=True.

    Возвращает список словарей {"id", "name", "ok", "path", "message", "uri",
    "provider", "crs"}: uri/provider — как открыть сохранённое (для копии
    проекта), crs — СК, в которую слой перепроецирован, или None.
    """
    if own_fields_only is None:
        own_fields_only = replace
    fmt = FORMATS_BY_KEY[fmt_key]
    driver, ext = fmt["driver"], fmt["ext"]
    os.makedirs(folder, exist_ok=True)
    tc = project.transformContext()
    protected = _source_files(project)

    subdirs = subdirs or {}
    taken_by_dir = {}  # {папка: занятые в этом запуске имена (в нижнем регистре)}

    def taken_in(dest):
        return taken_by_dir.setdefault(os.path.normcase(os.path.abspath(dest)), set())

    single_path = ""
    if fmt["single"]:
        gpkg_file = safe_name(gpkg_name)
        if not gpkg_file.lower().endswith(".gpkg"):
            gpkg_file += ".gpkg"
        single_path = os.path.join(folder, gpkg_file)
        if not overwrite or os.path.realpath(single_path) in protected:
            # в этот GeoPackage смотрят слои проекта — его таблицы не трогаем
            taken_in(folder).update(_existing_gpkg_layers(single_path))

    results = []
    total = len(items)
    for i, (layer, kind, temporary) in enumerate(items):
        if is_cancelled and is_cancelled():
            raise Cancelled()
        name = layer.name()
        if progress:
            progress(i, total, name)
        base = safe_name((names or {}).get(layer.id()) or name)
        sub = subdirs.get(layer.id())
        dest = os.path.join(folder, sub) if sub else folder
        taken = taken_in(dest)
        res = {"id": layer.id(), "name": name, "ok": False, "path": "", "message": "",
               "uri": "", "provider": "", "crs": None}
        notes = []
        can_repoint = replace
        own_only = own_fields_only and (can_repoint or not replace)
        try:
            if isinstance(layer, QgsVectorLayer) and layer.isEditable():
                if temporary:
                    if not layer.commitChanges():
                        raise RuntimeError("не удалось завершить редактирование: "
                                           + "; ".join(layer.commitErrors()))
                    notes.append("правки слоя были применены")
                elif layer.isModified():
                    # исходные данные не трогаем: правки попадут только в копию
                    notes.append("несохранённые правки попали в копию, в исходные данные не записаны")
                    if replace:
                        can_repoint = False
                        notes.append("слой в режиме редактирования — в проекте не переключён")
                elif replace:
                    layer.rollBack()  # правок нет — выходим из редактирования, чтобы переключить слой

            target = _target_crs(layer, crs, notes)
            ct = QgsCoordinateTransform(layer.crs(), target, tc) if target else None
            res["crs"] = target

            if kind == "raster":
                os.makedirs(dest, exist_ok=True)
                path, stem = _save_raster(layer, dest, base, overwrite, taken, protected, target)
                taken.add(stem.lower())
                if save_styles:
                    layer.saveNamedStyle(os.path.splitext(path)[0] + ".qml")
                res.update(uri=path, provider="gdal")
                if can_repoint and not _repoint(layer, path, "gdal", project, target):
                    raise RuntimeError("файл сохранён, но слой не удалось переключить на него")
                res.update(ok=True, path=path)

            elif fmt["single"]:
                tables = taken_in(folder)  # общий GeoPackage — в корне, без подпапок
                layer_name = _unique(gpkg_layer_name(base), lambda c: c.lower() in tables)
                tables.add(layer_name.lower())
                action = CREATE_LAYER if os.path.exists(single_path) else CREATE_FILE
                new_file, new_layer = _write_vector(layer, single_path, driver, layer_name, action, tc, ct,
                                                    own_fields_only=own_only)
                uri = "{}|layername={}".format(new_file, new_layer)
                res.update(uri=uri, provider="ogr")
                if save_styles:
                    try:
                        err = _copy_style_to_gpkg(layer, uri, new_layer)
                    except Exception as e:  # noqa: BLE001 — стиль не должен ронять сохранение данных
                        err = str(e)
                    if err:
                        notes.append("стиль не сохранён: " + err)
                if can_repoint and not _repoint(layer, uri, "ogr", project, target):
                    raise RuntimeError("данные сохранены, но слой не удалось переключить на них")
                res.update(ok=True, path="{} → {}".format(new_file, new_layer))

            else:
                parts = _SHP_PARTS if driver == "ESRI Shapefile" else ("." + ext,)

                def is_taken(c):
                    if c.lower() in taken:
                        return True
                    paths = [os.path.join(dest, c + p) for p in parts]
                    if any(os.path.realpath(p) in protected for p in paths):
                        return True  # из этого файла читает слой проекта
                    return not overwrite and any(os.path.exists(p) for p in paths)

                stem = _unique(base, is_taken)
                taken.add(stem.lower())
                os.makedirs(dest, exist_ok=True)
                path = os.path.join(dest, stem + "." + ext)
                new_file, new_layer = _write_vector(layer, path, driver, stem, CREATE_FILE, tc, ct,
                                                    own_fields_only=own_only)
                if not os.path.exists(new_file):
                    # Shapefile без геометрии записывается только как .dbf
                    dbf = os.path.splitext(new_file)[0] + ".dbf"
                    if os.path.exists(dbf):
                        new_file = dbf
                uri = "{}|layername={}".format(new_file, new_layer) if driver == "GPKG" else new_file
                res.update(uri=uri, provider="ogr")
                if save_styles:
                    if driver == "GPKG":  # стиль — внутрь GeoPackage, один файл на слой
                        try:
                            err = _copy_style_to_gpkg(layer, uri, new_layer)
                        except Exception as e:  # noqa: BLE001
                            err = str(e)
                    else:
                        msg, ok = layer.saveNamedStyle(os.path.splitext(new_file)[0] + ".qml")
                        err = "" if ok else msg
                    if err:
                        notes.append("стиль не сохранён: " + err)
                if can_repoint and not _repoint(layer, uri, "ogr", project, target):
                    raise RuntimeError("файл сохранён, но слой не удалось переключить на него")
                res.update(ok=True, path=new_file)

        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 — продолжаем с остальными слоями
            res["message"] = str(e)
        if notes:
            res["message"] = "; ".join(filter(None, [res["message"]] + notes))
        results.append(res)

    if progress:
        progress(total, total, "")
    if replace and any(r["ok"] for r in results):
        project.setDirty(True)
    return results
