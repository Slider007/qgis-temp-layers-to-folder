"""Поиск и сохранение временных слоёв проекта QGIS (без зависимостей от интерфейса)."""

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


FORMATS = [
    {"key": "gpkg_single", "label": "GeoPackage — все слои в одном файле",
     "driver": "GPKG", "ext": "gpkg", "single": True},
    {"key": "gpkg", "label": "GeoPackage — отдельный файл на каждый слой",
     "driver": "GPKG", "ext": "gpkg", "single": False},
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


def temp_kind(layer, processing_dir=None):
    """'memory', 'vector' или 'raster' для временного слоя, иначе None."""
    if processing_dir is None:
        processing_dir = _processing_temp_dir()

    if isinstance(layer, QgsVectorLayer):
        if layer.providerType() == "memory":
            return "memory"
    elif not isinstance(layer, QgsRasterLayer):
        return None  # меш, облака точек, векторные тайлы — не поддерживаются

    is_temp = False
    if hasattr(layer, "isTemporary"):
        try:
            is_temp = layer.isTemporary()
        except Exception:
            is_temp = False
    path = layer_path(layer)
    if not is_temp:
        is_temp = _inside(path, processing_dir)
    if not is_temp or not path:
        return None
    return "vector" if isinstance(layer, QgsVectorLayer) else "raster"


def find_temporary_layers(project):
    """Список (layer, kind) в порядке панели «Слои»."""
    processing_dir = _processing_temp_dir()
    ordered = [n.layer() for n in project.layerTreeRoot().findLayers() if n.layer()]
    seen = {l.id() for l in ordered}
    ordered += [l for l in project.mapLayers().values() if l.id() not in seen]

    result = []
    for layer in ordered:
        kind = temp_kind(layer, processing_dir)
        if kind:
            result.append((layer, kind))
    return result


# ---------------------------------------------------------------- имена

def safe_name(name):
    name = _INVALID_CHARS.sub("_", name or "").strip().strip(".")
    return (name or "layer")[:120]


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

    res = QgsVectorFileWriter.writeAsVectorFormatV3(layer, path, transform_context, opts)
    error, message = res[0], res[1]
    new_file = res[2] if len(res) > 2 and res[2] else path
    new_layer = res[3] if len(res) > 3 and res[3] else layer_name
    if error != WRITER_OK:
        raise RuntimeError(message or "ошибка записи (код {})".format(error))
    return new_file, new_layer


def _raster_stem(dest_folder, base, ext, overwrite, taken):
    def is_taken(name):
        if name.lower() in taken:
            return True
        return not overwrite and os.path.exists(os.path.join(dest_folder, name + ext))

    return _unique(base, is_taken)


def _copy_raster(src, dest_folder, base, overwrite, taken):
    """Копирует растр со всеми «спутниками» (.aux.xml, .ovr, .tfw, .prj…)."""
    src_dir, src_file = os.path.split(src)
    src_stem, ext = os.path.splitext(src_file)
    stem = _raster_stem(dest_folder, base, ext, overwrite, taken)
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


def _warp_raster(src, dest_folder, base, overwrite, taken, crs):
    """Перепроецирует растр в GeoTIFF (ресемплинг — ближайший сосед)."""
    from osgeo import gdal

    stem = _raster_stem(dest_folder, base, ".tif", overwrite, taken)
    dest = os.path.join(dest_folder, stem + ".tif")
    # gdal.Warp в существующий файл дописывает в него, а не заменяет — удаляем заранее
    for old in (dest, dest + ".aux.xml", dest + ".ovr"):
        if os.path.exists(old):
            os.remove(old)
    options = gdal.WarpOptions(
        format="GTiff",
        dstSRS=crs.toWkt(WKT_GDAL),
        resampleAlg="near",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    )
    ds = gdal.Warp(dest, src, options=options)
    if ds is None:
        raise RuntimeError("GDAL: " + (gdal.GetLastErrorMsg() or "не удалось перепроецировать растр"))
    ds = None
    return dest, stem


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
                progress=None, is_cancelled=None):
    """Сохраняет слои items = [(layer, kind), ...] в папку folder.

    crs — система координат для сохранения; None / недействительная — как у слоя.

    Возвращает список словарей {"name", "ok", "path", "message"}.
    """
    fmt = FORMATS_BY_KEY[fmt_key]
    driver, ext = fmt["driver"], fmt["ext"]
    os.makedirs(folder, exist_ok=True)
    tc = project.transformContext()

    single_path = ""
    taken = set()  # занятые в этом запуске имена (в нижнем регистре)
    if fmt["single"]:
        gpkg_file = safe_name(gpkg_name)
        if not gpkg_file.lower().endswith(".gpkg"):
            gpkg_file += ".gpkg"
        single_path = os.path.join(folder, gpkg_file)
        if not overwrite:
            taken |= _existing_gpkg_layers(single_path)

    results = []
    total = len(items)
    for i, (layer, kind) in enumerate(items):
        if is_cancelled and is_cancelled():
            raise Cancelled()
        name = layer.name()
        if progress:
            progress(i, total, name)
        base = safe_name(name)
        res = {"name": name, "ok": False, "path": "", "message": ""}
        notes = []
        try:
            if isinstance(layer, QgsVectorLayer) and layer.isEditable():
                if not layer.commitChanges():
                    raise RuntimeError("не удалось завершить редактирование: "
                                       + "; ".join(layer.commitErrors()))
                notes.append("правки слоя были применены")

            target = _target_crs(layer, crs, notes)
            ct = QgsCoordinateTransform(layer.crs(), target, tc) if target else None

            if kind == "raster":
                if target:
                    path, stem = _warp_raster(layer_path(layer), folder, base, overwrite, taken, target)
                else:
                    path, stem = _copy_raster(layer_path(layer), folder, base, overwrite, taken)
                taken.add(stem.lower())
                if save_styles:
                    layer.saveNamedStyle(os.path.splitext(path)[0] + ".qml")
                if replace and not _repoint(layer, path, "gdal", project, target):
                    raise RuntimeError("файл сохранён, но слой не удалось переключить на него")
                res.update(ok=True, path=path)

            elif fmt["single"]:
                layer_name = _unique(base, lambda c: c.lower() in taken)
                taken.add(layer_name.lower())
                action = CREATE_LAYER if os.path.exists(single_path) else CREATE_FILE
                new_file, new_layer = _write_vector(layer, single_path, driver, layer_name, action, tc, ct,
                                                    own_fields_only=replace)
                uri = "{}|layername={}".format(new_file, new_layer)
                if save_styles:
                    try:
                        err = _copy_style_to_gpkg(layer, uri, new_layer)
                    except Exception as e:  # noqa: BLE001 — стиль не должен ронять сохранение данных
                        err = str(e)
                    if err:
                        notes.append("стиль не сохранён: " + err)
                if replace and not _repoint(layer, uri, "ogr", project, target):
                    raise RuntimeError("данные сохранены, но слой не удалось переключить на них")
                res.update(ok=True, path="{} → {}".format(new_file, new_layer))

            else:
                def is_taken(c):
                    if c.lower() in taken:
                        return True
                    if overwrite:
                        return False
                    parts = _SHP_PARTS if driver == "ESRI Shapefile" else ("." + ext,)
                    return any(os.path.exists(os.path.join(folder, c + p)) for p in parts)

                stem = _unique(base, is_taken)
                taken.add(stem.lower())
                path = os.path.join(folder, stem + "." + ext)
                new_file, new_layer = _write_vector(layer, path, driver, stem, CREATE_FILE, tc, ct,
                                                    own_fields_only=replace)
                if not os.path.exists(new_file):
                    # Shapefile без геометрии записывается только как .dbf
                    dbf = os.path.splitext(new_file)[0] + ".dbf"
                    if os.path.exists(dbf):
                        new_file = dbf
                uri = "{}|layername={}".format(new_file, new_layer) if driver == "GPKG" else new_file
                if save_styles:
                    msg, ok = layer.saveNamedStyle(os.path.splitext(new_file)[0] + ".qml")
                    if not ok:
                        notes.append("стиль не сохранён: " + msg)
                if replace and not _repoint(layer, uri, "ogr", project, target):
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
