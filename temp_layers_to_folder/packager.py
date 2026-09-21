"""Сборка проекта в одну папку и архив для передачи заказчику.

1. Слои проекта копируются в папку data_all рядом с файлом проекта, свои
   картинки (SVG-значки, логотипы в макетах) — в data_all/images, шрифты со
   свободной лицензией — в data_all/fonts. Слои проекта переключаются на эти
   копии, пути делаются относительными, проект сохраняется — после этого
   исходные файлы можно удалить.
2. По желанию рядом с проектом собирается архив
   «<проект>_архив_<ГГГГ-ММ-ДД>.zip»: проект, data_all и «Состав.txt».
"""

import datetime
import os
import shutil
import sqlite3
import zipfile

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFontMarkerSymbolLayer,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPicture,
    QgsLegendStyle,
    QgsPalLayerSettings,
    QgsProject,
    QgsProviderRegistry,
    QgsRasterFillSymbolLayer,
    QgsRenderContext,
    QgsRuleBasedLabeling,
    QgsSVGFillSymbolLayer,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

from . import copier, fonts, saver

DATA_DIR, IMAGES_DIR, FONTS_DIR, README = "data_all", "images", "fonts", "Состав.txt"
SYMBOLS_DIR = "symbols"  # картинки при сборке со структурой папок


def project_base_name(project):
    return saver.safe_name(project.baseName() or project.title() or "Проект")


def archive_name(project, today=None):
    return "{}_архив_{}".format(project_base_name(project), (today or datetime.date.today()).isoformat())


def data_dir(project):
    return os.path.join(os.path.dirname(project.fileName()), DATA_DIR)


def _unique_path(folder, stem, ext):
    return os.path.join(folder, saver._unique(stem, lambda c: os.path.exists(os.path.join(folder, c + ext))) + ext)


# ------------------------------------------------------------ картинки

def _needs_copy(path, target_dir):
    """Свой файл на диске: не встроенный, не из интернета, не из поставки QGIS
    и ещё не лежащий в data_all."""
    if not path or path.startswith(("base64:", "http://", "https://")) or not os.path.isfile(path):
        return False
    if saver._inside(path, os.path.realpath(QgsApplication.pkgDataPath())):
        return False
    return not saver._inside(path, os.path.realpath(target_dir))


def _image_accessors(symbol_layer):
    """(получить путь, задать путь) для слоёв символа с картинкой."""
    if isinstance(symbol_layer, QgsSVGFillSymbolLayer):
        return symbol_layer.svgFilePath, symbol_layer.setSvgFilePath
    if isinstance(symbol_layer, QgsRasterFillSymbolLayer):
        return symbol_layer.imageFilePath, symbol_layer.setImageFilePath
    if hasattr(symbol_layer, "path") and hasattr(symbol_layer, "setPath"):  # SVG/растровый маркер, линия
        return symbol_layer.path, symbol_layer.setPath
    return None


def _walk_symbol_layers(symbol):
    for sl in symbol.symbolLayers():
        yield sl
        sub = sl.subSymbol()
        if sub is not None:
            yield from _walk_symbol_layers(sub)


def _layer_symbols(layer):
    renderer = layer.renderer() if isinstance(layer, QgsVectorLayer) else None
    if renderer is None:
        return []
    try:
        return list(renderer.symbols(QgsRenderContext()))
    except Exception:  # noqa: BLE001
        return []


def _relink_symbol(symbol, fix):
    changed = False
    for sl in _walk_symbol_layers(symbol):
        acc = _image_accessors(sl)
        new = fix(acc[0]()) if acc else None
        if new:
            acc[1](new)
            changed = True
    return changed


def _relink_labels(layer, fix):
    """Картинки фона подписей (SVG и значок): fix(путь) → новый путь или None."""
    labeling = layer.labeling() if isinstance(layer, QgsVectorLayer) else None

    def fix_settings(settings):
        fmt = settings.format()
        bg = fmt.background()
        changed = False
        new = fix(bg.svgFile())
        if new:
            bg.setSvgFile(new)
            changed = True
        if bg.markerSymbol() is not None and _relink_symbol(bg.markerSymbol(), fix):
            changed = True
        if changed:
            fmt.setBackground(bg)
            settings.setFormat(fmt)
        return changed

    changed = False
    if isinstance(labeling, QgsVectorLayerSimpleLabeling):
        settings = QgsPalLayerSettings(labeling.settings())
        if fix_settings(settings):
            labeling.setSettings(settings)
            changed = True
    elif isinstance(labeling, QgsRuleBasedLabeling):
        for rule in labeling.rootRule().descendants():
            if rule.settings() is None:
                continue
            settings = QgsPalLayerSettings(rule.settings())
            if fix_settings(settings):
                rule.setSettings(settings)
                changed = True
    return changed


def _picture_format(path):
    svg = path.lower().endswith(".svg")
    try:
        return Qgis.PictureFormat.SVG if svg else Qgis.PictureFormat.Raster
    except AttributeError:
        return QgsLayoutItemPicture.FormatSVG if svg else QgsLayoutItemPicture.FormatRaster


def collect_images(project, images_dir, layer_ids, data_folder=None):
    """Копирует свои картинки из символов и фона подписей слоёв layer_ids и из
    макетов в images_dir и переключает на них проект. Картинки, уже лежащие в
    data_folder (по умолчанию images_dir), не копируются. Возвращает
    {исходный путь: копия}."""
    copied = {}
    data_folder = data_folder or images_dir

    def local(path):
        if path not in copied:
            os.makedirs(images_dir, exist_ok=True)
            stem, ext = os.path.splitext(os.path.basename(path))
            dest = _unique_path(images_dir, saver.safe_name(stem), ext)
            shutil.copy2(path, dest)
            copied[path] = dest
        return copied[path]

    for layer in project.mapLayers().values():
        if layer.id() not in layer_ids:
            continue
        def fix(path):
            return local(path) if _needs_copy(path, data_folder) else None

        changed = False
        for symbol in _layer_symbols(layer):
            changed = _relink_symbol(symbol, fix) or changed
        changed = _relink_labels(layer, fix) or changed
        if changed:
            layer.triggerRepaint()
    for layout in project.layoutManager().layouts():
        for item in layout.items():
            if isinstance(item, QgsLayoutItemPicture) and _needs_copy(item.picturePath(), data_folder):
                path = item.picturePath()
                item.setPicturePath(local(path), _picture_format(path))
    return copied


# ------------------------------------------------------------ система координат

def fit_layout_maps(project, old_crs, new_crs):
    """После смены СК проекта карты макетов без своей СК показали бы старые
    координаты в новой СК. Пересчитываем их охват, масштаб сохраняем."""
    ct = QgsCoordinateTransform(old_crs, new_crs, project.transformContext())
    for layout in project.layoutManager().layouts():
        for item in layout.items():
            if not isinstance(item, QgsLayoutItemMap) or item.presetCrs().isValid():
                continue
            scale = item.scale()
            try:
                extent = ct.transformBoundingBox(item.extent())
            except QgsCsException:
                continue
            item.zoomToExtent(extent)
            item.setScale(scale)


def _set_relative_paths(project):
    try:
        project.setFilePathStorage(Qgis.FilePathType.Relative)
    except AttributeError:
        project.writeEntryBool("Paths", "/Absolute", False)


# ------------------------------------------------------------ шрифты

def _family(text_format):
    try:
        return text_format.font().family()
    except Exception:  # noqa: BLE001
        return ""


def _legend_component(name):
    try:
        return getattr(Qgis.LegendComponent, name)
    except AttributeError:
        return saver._enum(QgsLegendStyle, "Style", name)


def used_font_families(project, layer_ids=None):
    """Семейства шрифтов из подписей и шрифтовых значков слоёв (layer_ids — только
    этих слоёв) и из надписей, легенд, масштабных линеек, сеток и таблиц макетов."""
    fams = set()
    for layer in project.mapLayers().values():
        if layer_ids is not None and layer.id() not in layer_ids:
            continue
        if isinstance(layer, QgsVectorLayer) and layer.labeling() is not None:
            labeling = layer.labeling()
            for provider in labeling.subProviders():
                settings = labeling.settings(provider)
                if settings is not None:
                    fams.add(_family(settings.format()))
        for symbol in _layer_symbols(layer):
            for sl in _walk_symbol_layers(symbol):
                if isinstance(sl, QgsFontMarkerSymbolLayer):
                    fams.add(sl.fontFamily())

    for layout in project.layoutManager().layouts():
        for item in layout.items():
            if hasattr(item, "textFormat"):  # надпись, масштабная линейка
                try:
                    fams.add(_family(item.textFormat()))
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(item, QgsLayoutItemLegend):
                for name in ("Title", "Group", "Subgroup", "SymbolLabel"):
                    try:
                        # style() — временный объект: держим его, пока читаем шрифт,
                        # иначе textFormat() ссылается на удалённое и QGIS 3.44 падает
                        style = item.style(_legend_component(name))
                        fams.add(_family(style.textFormat()))
                    except Exception:  # noqa: BLE001
                        pass
            if isinstance(item, QgsLayoutItemMap):  # сетки
                try:
                    for grid in item.grids().asList():
                        fams.add(_family(grid.annotationTextFormat()))
                except Exception:  # noqa: BLE001
                    pass
        for frame in layout.multiFrames():  # таблицы атрибутов
            for getter in ("headerTextFormat", "contentTextFormat"):
                if hasattr(frame, getter):
                    try:
                        fams.add(_family(getattr(frame, getter)()))
                    except Exception:  # noqa: BLE001
                        pass
    return {f for f in fams if f and not f.startswith(".")}


def copy_fonts(font_info, fonts_dir):
    """Копирует файлы свободных шрифтов в fonts_dir; возвращает список копий."""
    copied = []
    for paths in font_info["free"].values():
        for path in paths:
            os.makedirs(fonts_dir, exist_ok=True)
            dest = os.path.join(fonts_dir, os.path.basename(path))
            if os.path.exists(dest) and os.path.getsize(dest) != os.path.getsize(path):
                stem, ext = os.path.splitext(os.path.basename(path))
                dest = _unique_path(fonts_dir, stem, ext)  # другой шрифт с тем же именем файла
            if not os.path.exists(dest):
                shutil.copy2(path, dest)
            copied.append(dest)
    return copied


# ------------------------------------------------------------ архив

def _checkpoint(db_path):
    """Переносит всё из журнала -wal в сам GeoPackage. QGIS держит файлы открытыми,
    и последние записи (например, стиль слоя) могут лежать только в журнале."""
    try:
        con = sqlite3.connect(db_path, timeout=10)
        try:
            busy, _log, _done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        return busy == 0
    except sqlite3.Error:
        return False


def _archive_project_copy(project_file, keep_ids, dest):
    """Копия проекта для архива — только слои из data_all и онлайн-слои. Пишется
    рядом с исходным проектом, чтобы относительные пути ./data_all остались верными."""
    copy = QgsProject()
    try:
        flags = Qgis.ProjectReadFlags(Qgis.ProjectReadFlag.DontResolveLayers)
    except AttributeError:
        flags = QgsProject.ReadFlags(QgsProject.FlagDontResolveLayers)
    if not copy.read(project_file, flags):
        raise RuntimeError("не удалось прочитать проект: " + copy.error())
    drop = [lid for lid in copy.mapLayers() if lid not in keep_ids]
    if drop:
        copy.removeMapLayers(drop)
    if not copy.write(dest):
        raise RuntimeError("не удалось записать копию проекта: " + copy.error())
    copy.clear()


def write_archive(zip_path, root, project_copy, project_name, data_path, readme):
    clean = set()
    for folder, _dirs, files in os.walk(data_path):
        for name in files:
            if name.lower().endswith(".gpkg"):
                path = os.path.join(folder, name)
                if _checkpoint(path):
                    clean.add(path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(project_copy, "{}/{}.qgz".format(root, project_name))
        for folder, _dirs, files in os.walk(data_path):
            for name in sorted(files):
                path = os.path.join(folder, name)
                if name.endswith(("-wal", "-shm")) and path[:-4] in clean:
                    continue  # журнал уже перенесён в сам GeoPackage
                rel = os.path.relpath(path, os.path.dirname(data_path)).replace(os.sep, "/")
                zf.write(path, "{}/{}".format(root, rel))
        zf.writestr("{}/{}".format(root, README), "﻿" + readme)  # BOM — для Блокнота Windows


def _crs_text(crs):
    if crs is None or not crs.isValid():
        return "не менялась"
    return " — ".join(filter(None, [crs.authid(), crs.description()])) or "пользовательская"


def readme_text(name, crs, layers, online, excluded, font_info, images, images_where=None):
    lines = ["Проект: {}".format(name),
             "Собран: {}".format(datetime.date.today().strftime("%d.%m.%Y")),
             "Система координат: {}".format(_crs_text(crs)),
             "",
             "Как открыть",
             "1. Распакуйте архив целиком — папка data_all должна лежать рядом с файлом проекта."]
    step = 2
    if font_info["free"]:
        lines.append("{}. Установите шрифты из папки {}/{}/ (двойной щелчок по файлу → «Установить»).".format(
            step, DATA_DIR, FONTS_DIR))
        step += 1
    lines += ["{}. Откройте {}.qgz в QGIS 3.20 или новее.".format(step, name), ""]
    lines.append("Слои ({}):".format(len(layers)))
    lines += ["  {} — {}".format(n, rel) for n, rel in layers]
    if online:
        lines += ["", "Онлайн-слои (нужен интернет):"] + ["  " + n for n in online]
    if excluded:
        lines += ["", "Не вошли в архив:"] + ["  " + n for n in excluded]
    if any(font_info.values()):
        lines += ["", "Шрифты"]
        if font_info["free"]:
            lines.append("  в папке {}/{}/ (свободная лицензия): {}".format(
                DATA_DIR, FONTS_DIR, ", ".join(sorted(font_info["free"]))))
        if font_info["paid"]:
            lines.append("  платные, не вложены — установите по своей лицензии: " + ", ".join(font_info["paid"]))
        if font_info["unknown"]:
            lines.append("  лицензия не указана, не вложены: " + ", ".join(font_info["unknown"]))
        if font_info["missing"]:
            lines.append("  не найдены при сборке, подписи будут другим шрифтом: " + ", ".join(font_info["missing"]))
    if images:
        lines += ["", "Свои значки и картинки: {} шт. {}".format(
            len(images), images_where or "в {}/{}/".format(DATA_DIR, IMAGES_DIR))]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ всё вместе

def _needs_crs(layer, crs):
    return crs is not None and layer.isSpatial() and layer.crs().isValid() and layer.crs() != crs


def source_path(layer):
    """Файл или папка на диске, из которой читает слой (в том числе NetCDF,
    архив zip, папка шейпов), или пустая строка."""
    src = copier.layer_source(layer)
    return src.path if src is not None else saver.layer_path(layer)


def in_data_dir(layer, target):
    """Источник слоя уже лежит в папке data_all target."""
    return saver._inside(source_path(layer), os.path.realpath(target))


def find_layers(project, native_format=False):
    """Слои для сборки — как saver.find_layers(temporary_only=False). С форматом
    «оставить родные форматы файлов» в список попадают и облака точек с сетками:
    их файлы копируются как есть; те, что так не скопировать, — в skipped с причиной."""
    items, skipped = saver.find_layers(project, temporary_only=False, copy_as_is=native_format)
    ready = []
    for item in items:
        reason = copier.copy_obstacle(item[0]) if item[1] in copier.AS_IS_ONLY else ""
        if reason:
            skipped.append((item[0], reason))
        else:
            ready.append(item)
    return ready, skipped


def _file_stem(layer):
    """Имя исходного файла без расширения для слоя, который один в своём файле
    (лес.shp → лес.gpkg, мозаика.vrt → мозаика.tif). У слоя из контейнера
    (GeoPackage, NetCDF, файл с несколькими слоями) — пустая строка: остаётся
    имя слоя."""
    path = saver.layer_path(layer)
    if not path or os.path.splitext(path)[1].lower() in copier.CONTAINERS:
        return ""
    if layer.providerType() == "ogr":  # KML с папками, DXF… — слоёв в файле несколько
        try:
            if len(QgsProviderRegistry.instance().providerMetadata("ogr").querySublayers(path)) > 1:
                return ""
        except Exception:  # noqa: BLE001
            return ""
    return os.path.splitext(os.path.basename(path))[0]


def _result(layer, message):
    return {"id": layer.id(), "name": layer.name(), "ok": False, "path": "", "message": message,
            "uri": "", "provider": layer.providerType(), "crs": None}


def flat_in_data_dir(project, items):
    """Слои items, собранные прошлой сборкой в data_all без подпапок: все они
    лежат прямо в data_all, и ни один — в её подпапке. Их исходные папки
    неизвестны, разложить их по подпапкам нельзя."""
    target = os.path.realpath(data_dir(project))
    inside = [(l, os.path.dirname(os.path.realpath(source_path(l))))
              for l, *_ in items if in_data_dir(l, target)]
    flat = [l for l, d in inside if d == target]
    return flat if len(flat) == len(inside) else []


def consolidate_project(project, items, fmt_key, crs=None, save_styles=True, include_fonts=True,
                        save_project=None, progress=None, is_cancelled=None, font_dirs=None,
                        today=None, make_archive=True, keep_structure=False, split_by_type=None):
    """Собирает проект в data_all и, если make_archive, делает архив. items —
    выбранные слои [(layer, kind, temporary)]. save_project — как сохранить
    проект (в QGIS — через стандартное действие «Сохранить»), по умолчанию
    project.write(). Формат «оставить родные форматы файлов» (fmt_key «native»)
    копирует файлы слоёв как есть; остальные форматы переводят слои в них.
    keep_structure — раскладывать по подпапкам исходных файлов (data/… →
    data_all/…, файлы вне проекта — в data_all/external_links/…), картинки — в
    data_all/symbols; без него всё ложится прямо в data_all, картинки — в
    data_all/images. split_by_type (по умолчанию copier.SPLIT_BY_TYPE) — со
    структурой папок растры, облака точек и сетки ложатся в data_all/raster/,
    pointcloud/, mesh/.

    Возвращает словарь с итогами.
    """
    project_file = project.fileName()
    if not project_file or not os.path.isfile(project_file):
        raise RuntimeError("сначала сохраните проект в файл")
    target = data_dir(project)
    project_dir = os.path.dirname(project_file)
    os.makedirs(target, exist_ok=True)
    real_target = os.path.realpath(target)
    use_crs = crs if crs is not None and crs.isValid() else None
    steps = len(items) + 3

    def step(i, text):
        if progress:
            progress(i, steps, text)

    # 1. слои → data_all, проект переключается на них
    split = copier.SPLIT_BY_TYPE if split_by_type is None else split_by_type
    native = bool(saver.FORMATS_BY_KEY[fmt_key].get("native"))
    todo, already = [], []
    for item in items:
        layer, kind = item[0], item[1]
        # облака точек и сетки не перепроецируются: уже собранные остаются на месте
        reproject = _needs_crs(layer, use_crs) and kind not in copier.AS_IS_ONLY
        (todo if not in_data_dir(layer, target) or reproject else already).append(item)
    # С родными форматами файлы копируются как есть (copier), под своими именами.
    # В формат fmt_key переводятся слои без файла (в памяти, из баз), результаты
    # Processing и файлы, которые как есть не скопировать (VRT, смена СК).
    to_copy, subdirs, names = [], None, None
    if native:
        to_copy = [it for it in todo if not it[2] and copier.layer_source(it[0]) is not None
                   and (it[1] in copier.AS_IS_ONLY or not _needs_crs(it[0], use_crs))]
    copy_ids = {it[0].id() for it in to_copy}
    rest = [it for it in todo if it[0].id() not in copy_ids]
    to_convert = [it for it in rest if it[1] not in copier.AS_IS_ONLY]
    blocked = [_result(it[0], copier.copy_obstacle(it[0]) if native else
                       "облака точек и сетки собираются только с родными форматами файлов")
               for it in rest if it[1] in copier.AS_IS_ONLY]
    if keep_structure:
        subdirs, names = {}, {}
        for layer, kind, temporary in to_convert:
            kind_dir = copier.data_type(kind) if split else ""
            src = "" if temporary else source_path(layer)
            if src:  # на место исходного файла и под его именем: мозаика.vrt → мозаика.tif
                folder = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
                subdirs[layer.id()] = copier.target_subdir(folder, project_dir, target, kind=kind_dir)
                names[layer.id()] = _file_stem(layer)
            elif kind_dir in copier.TYPE_DIRS:  # временный растр — в data_all/raster
                subdirs[layer.id()] = copier.TYPE_DIRS[kind_dir]
    results = blocked + saver.save_layers(
        project, to_convert, target, fmt_key, gpkg_name=project_base_name(project), replace=True,
        save_styles=save_styles, overwrite=False, crs=use_crs,
        progress=lambda i, total, n: step(len(already) + i, n), is_cancelled=is_cancelled,
        subdirs=subdirs, names=names)
    if to_copy:
        kept_crs = {it[0].id() for it in to_copy if _needs_crs(it[0], use_crs)}  # облака точек и сетки
        copied = copier.copy_layers(
            project, to_copy, target, project_dir,
            progress=lambda i, total, n: step(len(already) + len(to_convert) + i, n),
            is_cancelled=is_cancelled, split_by_type=split, structure=keep_structure)
        for r in copied:
            if r["ok"] and r["id"] in kept_crs:
                r["message"] = "; ".join(m for m in (r["message"], "система координат не изменена — "
                                                     "облака точек и сетки копируются как есть") if m)
        results += copied
    order = {it[0].id(): n for n, it in enumerate(todo)}
    results.sort(key=lambda r: order[r["id"]])

    step(len(items), "картинки и проект")
    chosen = {layer.id() for layer, *_ in items}
    images_dir = os.path.join(target, SYMBOLS_DIR if keep_structure else IMAGES_DIR)
    images = collect_images(project, images_dir, chosen, target)
    images_where = "в {}/{}/".format(DATA_DIR, SYMBOLS_DIR) if keep_structure else None
    if use_crs is not None and project.crs() != use_crs:
        old = project.crs()
        project.setCrs(use_crs)
        if old.isValid():
            fit_layout_maps(project, old, use_crs)
    _set_relative_paths(project)
    if save_project is not None:
        save_project()
    elif not project.write():
        raise RuntimeError("не удалось сохранить проект: " + project.error())
    if project.isDirty():
        raise RuntimeError("проект не сохранён — архив не собирался")

    # 2. шрифты в data_all/fonts и архив рядом с проектом
    step(len(items) + 1, "шрифты")
    in_data = {l.id(): l for l in project.mapLayers().values()
               if in_data_dir(l, real_target)}
    _all, skipped = saver.find_layers(project, temporary_only=False)
    online = {l.id(): l.name() for l, reason in skipped if reason.startswith("онлайн")}
    excluded = [l.name() for l in project.mapLayers().values() if l.id() not in in_data and l.id() not in online]
    font_info = {"free": {}, "paid": [], "unknown": [], "missing": []}
    if include_fonts:
        font_info = fonts.find_fonts(used_font_families(project, set(in_data)), font_dirs)
        copy_fonts(font_info, os.path.join(target, FONTS_DIR))

    folder = os.path.dirname(project_file)
    zip_path = ""
    if make_archive:
        step(len(items) + 2, "архив")
        zip_path = _unique_path(folder, archive_name(project, today), ".zip")
        root = os.path.splitext(os.path.basename(zip_path))[0]
        base = project_base_name(project)
        layers = sorted((l.name(), os.path.relpath(source_path(l), folder).replace(os.sep, "/"))
                        for l in in_data.values())
        readme = readme_text(base, use_crs, layers, sorted(online.values()), sorted(excluded),
                             font_info, images, images_where)
        copy_path = os.path.join(folder, ".{}.qgz".format(root))  # временная, рядом с проектом
        try:
            _archive_project_copy(project_file, set(in_data) | set(online), copy_path)
            write_archive(zip_path, root, copy_path, base, target, readme)
        finally:
            if os.path.exists(copy_path):
                os.remove(copy_path)
    step(steps, "")
    for r in results:
        r["rel"] = os.path.relpath(r["path"].split(" → ")[0], folder) if r["ok"] else ""
    return {"data_dir": target, "zip": zip_path, "results": results,
            "already": [layer.name() for layer, *_ in already], "images": images,
            "images_where": images_where,
            "fonts": font_info, "online": sorted(online.values()), "excluded": sorted(excluded)}
