"""Проверки плагина без интерфейса QGIS. Запуск: tests/run_tests.sh

Скрипт не трогает профиль QGIS: настройки и пользовательская СК
пишутся во временный профиль tests/_profile, результаты — в tests/_out.
"""

import glob
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402

PROFILE = os.path.join(HERE, "_profile")
OUT = os.path.join(HERE, "_out")
shutil.rmtree(PROFILE, ignore_errors=True)
shutil.rmtree(OUT, ignore_errors=True)
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("temp-layers-tests")
QCoreApplication.setApplicationName("temp-layers-tests")

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsLayoutSize,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingUtils,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QEvent, QMetaType  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402
from qgis.PyQt.QtWidgets import QMenu  # noqa: E402

app = QgsApplication([], True, PROFILE)
if os.environ.get("QGIS_PREFIX_PATH"):
    app.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
app.initQgis()
# initQgis() переносит настройки в ~/Library/Application Support/<организация>/…/profiles/default,
# которую никто не чистит: возвращаем их во временный профиль.
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
assert QSettings().fileName().startswith(PROFILE), QSettings().fileName()
_PLUGIN_MENU = QMenu("Модули")  # меню «Модули» для поддельного iface

from osgeo import gdal, ogr, osr  # noqa: E402

from temp_layers_to_folder import saver  # noqa: E402

UTM37 = QgsCoordinateReferenceSystem("EPSG:32637")
_msk = QgsCoordinateReferenceSystem.fromProj(
    "+proj=tmerc +lat_0=0 +lon_0=39.03333333333 +k=1 +x_0=2250000 +y_0=-5714743.504 "
    "+ellps=krass +towgs84=23.57,-140.95,-79.8,0,0.35,0.79,-0.22 +units=m +no_defs")
_rid = QgsApplication.coordinateReferenceSystemRegistry().addUserCrs(_msk, "МСК (тест)")
MSK = QgsCoordinateReferenceSystem("USER:%d" % _rid)


# ------------------------------------------------------------------ данные

def make_project():
    """Проект: точки (с дублями fid и в режиме редактирования), два слоя
    с одинаковым именем, таблица без геометрии, временный растр и постоянный слой."""
    p = QgsProject.instance()
    p.clear()

    pts = QgsVectorLayer("Point?crs=EPSG:4326&field=fid:integer&field=имя:string(50)",
                         "Точки: скважины/2", "memory")
    for i, (x, y) in enumerate([(39.1, 48.5), (39.2, 48.6), (39.3, 48.7)]):
        f = QgsFeature(pts.fields())
        f.setAttributes([1, "т%d" % i])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        pts.dataProvider().addFeature(f)
    pts.startEditing()
    f = QgsFeature(pts.fields())
    f.setAttributes([5, "новая"])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.4, 48.8)))
    pts.addFeature(f)
    pts.renderer().symbol().setColor(QColor("#ff0000"))

    poly = QgsVectorLayer("Polygon?crs=EPSG:32637&field=area:double", "Участки", "memory")
    f = QgsFeature(poly.fields())
    f.setAttributes([1.5])
    f.setGeometry(QgsGeometry.fromWkt("POLYGON((500000 5380000,500100 5380000,500100 5380100,500000 5380000))"))
    poly.dataProvider().addFeature(f)

    line = QgsVectorLayer("LineString?crs=EPSG:4326", "Участки", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromWkt("LINESTRING(39 48,39.1 48.1)"))
    line.dataProvider().addFeature(f)

    table = QgsVectorLayer("None?field=a:string(20)", "Таблица", "memory")
    f = QgsFeature(table.fields())
    f.setAttributes(["x"])
    table.dataProvider().addFeature(f)

    d = os.path.join(QgsProcessingUtils.tempFolder(), "tests_raster")
    os.makedirs(d, exist_ok=True)
    tif = os.path.join(d, "OUTPUT.tif")
    ds = gdal.GetDriverByName("GTiff").Create(tif, 20, 20, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([39.0, 0.01, 0, 48.8, 0, -0.01])
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(4326)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).Fill(3.5)
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds = None
    with open(tif + ".aux.xml", "w") as fh:
        fh.write("<PAMDataset/>")
    ras = QgsRasterLayer(tif, "Уклон", "gdal")

    os.makedirs(OUT, exist_ok=True)
    perm_path = os.path.join(OUT, "_permanent.geojson")
    with open(perm_path, "w") as fh:
        fh.write('{"type":"FeatureCollection","features":[]}')
    perm = QgsVectorLayer(perm_path, "Постоянный", "ogr")

    p.addMapLayers([pts, poly, line, table, ras, perm])
    return p


def by_name(items, name):
    return [l for l, *_ in items if l.name() == name]


def save(fmt, sub, **kw):
    p = make_project()
    items = saver.find_temporary_layers(p)
    folder = os.path.join(OUT, sub)
    res = saver.save_layers(p, items, folder, fmt, gpkg_name="слои", **kw)
    return p, items, folder, res


# ------------------------------------------------------------------ проверки

def test_find():
    p = make_project()
    kinds = sorted((l.name(), k) for l, k, _ in saver.find_temporary_layers(p))
    assert kinds == sorted([("Точки: скважины/2", "memory"), ("Участки", "memory"),
                            ("Участки", "memory"), ("Таблица", "memory"),
                            ("Уклон", "raster")]), kinds


def check_saved(p, items, res, provider_ok=("ogr", "gdal")):
    bad = [r for r in res if not r["ok"]]
    assert not bad, bad
    for l, *_ in items:
        assert l.isValid() and l.providerType() in provider_ok, (l.name(), l.providerType())
    assert saver.find_temporary_layers(p) == [], "остались временные слои"
    pts = by_name(items, "Точки: скважины/2")[0]
    assert pts.featureCount() == 4, "правки из буфера редактирования не сохранились"
    assert pts.renderer().symbol().color().name() == "#ff0000", "стиль потерян"
    assert not pts.isEditable()


def test_gpkg_single():
    p, items, folder, res = save("gpkg_single", "gpkg_single")
    check_saved(p, items, res)
    assert os.path.exists(os.path.join(folder, "слои.gpkg"))
    assert os.path.exists(os.path.join(folder, "Уклон.tif.aux.xml"))
    # повтор без перезаписи — новые имена слоёв внутри того же файла
    save("gpkg_single", "gpkg_single", replace=False)
    ds = ogr.Open(os.path.join(folder, "слои.gpkg"))
    names = {ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())}
    ds = None
    assert {"Точки_ скважины_2", "Участки", "Участки_2", "Точки_ скважины_2_2", "Участки_3"} <= names, names
    # стиль из GeoPackage подхватывается при открытии, fid вынесен в отдельную колонку
    vl = QgsVectorLayer(os.path.join(folder, "слои.gpkg") + "|layername=Точки_ скважины_2", "x", "ogr")
    assert vl.renderer().symbol().color().name() == "#ff0000"
    assert [f.name() for f in vl.fields()][:2] == ["fid_gpkg", "fid"]


def test_gpkg_per_layer():
    p, items, folder, res = save("gpkg", "gpkg")
    check_saved(p, items, res)
    assert os.path.exists(os.path.join(folder, "Участки_2.gpkg"))
    # стиль внутри GeoPackage, без отдельного .qml
    assert not os.path.exists(os.path.join(folder, "Точки_ скважины_2.qml"))
    vl = QgsVectorLayer(os.path.join(folder, "Точки_ скважины_2.gpkg"), "x", "ogr")
    assert vl.renderer().symbol().color().name() == "#ff0000"


def test_shapefile():
    p, items, folder, res = save("shp", "shp")
    check_saved(p, items, res)
    files = os.listdir(folder)
    assert "Точки_ скважины_2.prj" in files and "Точки_ скважины_2.qml" in files
    assert "Таблица.dbf" in files  # слой без геометрии
    # повтор с перезаписью — те же файлы, без суффиксов _2
    _, _, _, res2 = save("shp", "shp", replace=False, overwrite=True)
    assert [r["path"] for r in res2] == [r["path"] for r in res], res2


def test_geojson():
    p, items, folder, res = save("geojson", "geojson")
    check_saved(p, items, res)
    vl = QgsVectorLayer(os.path.join(folder, "Точки_ скважины_2.geojson"), "x", "ogr")
    assert vl.renderer().symbol().color().name() == "#ff0000", ".qml не подхватился"


def test_crs_epsg():
    p, items, folder, res = save("gpkg_single", "crs_utm", crs=UTM37)
    check_saved(p, items, res)
    for l, *_ in items:
        if l.isSpatial():
            assert l.crs() == UTM37, (l.name(), l.crs().authid())
    pt = next(by_name(items, "Точки: скважины/2")[0].getFeatures()).geometry().asPoint()
    assert 400000 < pt.x() < 600000 and 5300000 < pt.y() < 5500000, pt
    info = gdal.Info(os.path.join(folder, "Уклон.tif"), format="json")
    assert info["bands"][0].get("noDataValue") == -9999


def test_crs_user_msk():
    p, items, folder, res = save("shp", "crs_msk", crs=MSK)
    check_saved(p, items, res)
    for l, *_ in items:
        if l.isSpatial():
            assert l.crs() == MSK and l.crs().description() == "МСК (тест)", l.crs().description()


def test_crs_not_set():
    p, items, folder, res = save("gpkg", "crs_none", crs=QgsCoordinateReferenceSystem())
    check_saved(p, items, res)
    assert by_name(items, "Точки: скважины/2")[0].crs().authid() == "EPSG:4326"
    assert by_name(items, "Уклон")[0].width() == 20  # растр скопирован, не перепроецирован


def make_permanent_sources():
    """Постоянные данные на диске: GeoJSON с точками и растр VRT поверх GeoTIFF."""
    src = os.path.join(OUT, "src")
    os.makedirs(src, exist_ok=True)
    gj = os.path.join(src, "Опоры.geojson")
    with open(gj, "w") as fh:
        fh.write('{"type":"FeatureCollection","features":['
                 '{"type":"Feature","properties":{"n":1},"geometry":{"type":"Point","coordinates":[39.1,48.5]}},'
                 '{"type":"Feature","properties":{"n":2},"geometry":{"type":"Point","coordinates":[39.2,48.6]}}]}')
    tif = os.path.join(src, "dem.tif")
    ds = gdal.GetDriverByName("GTiff").Create(tif, 10, 10, 1, gdal.GDT_Int16)
    ds.SetGeoTransform([39.0, 0.01, 0, 48.8, 0, -0.01])
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(4326)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).Fill(120)
    ds = None
    vrt = os.path.join(src, "dem.vrt")
    gdal.BuildVRT(vrt, [tif]).FlushCache()
    return src, gj, vrt


def all_layers_project():
    p = QgsProject.instance()
    p.clear()
    src, gj, vrt = make_permanent_sources()
    pts = QgsVectorLayer(gj, "Опоры", "ogr")
    dem = QgsRasterLayer(vrt, "Рельеф", "gdal")
    mem = QgsVectorLayer("Point?crs=EPSG:4326", "Черновик", "memory")
    p.addMapLayers([pts, dem, mem])
    try:
        from qgis.core import QgsVectorTileLayer
        vt = QgsVectorTileLayer("type=xyz&url=http://localhost/{z}/{x}/{y}.pbf&zmin=0&zmax=14", "Тайлы")
        p.addMapLayer(vt)
    except ImportError:
        pass
    return p, src, gj, pts, dem, mem


def test_all_layers_mode():
    p, src, gj, pts, dem, mem = all_layers_project()
    items, skipped = saver.find_layers(p, temporary_only=False)
    assert [(l.name(), k, t) for l, k, t in items] == [
        ("Опоры", "vector", False), ("Рельеф", "raster", False), ("Черновик", "memory", True)], items
    assert [l.name() for l, _ in skipped] == ["Тайлы"] and "тайлы" in skipped[0][1], skipped
    assert [l.name() for l, *_ in saver.find_layers(p, temporary_only=True)[0]] == ["Черновик"]
    assert saver.describe(pts, "vector", False) == "файл .geojson"

    # несохранённая правка в постоянном слое попадает только в копию
    pts.startEditing()
    f = QgsFeature(pts.fields())
    f.setAttributes([3])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.3, 48.7)))
    pts.addFeature(f)
    before = open(gj, encoding="utf-8").read()

    folder = os.path.join(OUT, "all_utm")
    res = saver.save_layers(p, items, folder, "gpkg", crs=UTM37, replace=False)
    assert all(r["ok"] for r in res), res
    assert open(gj, encoding="utf-8").read() == before, "исходный файл изменён"
    assert pts.isEditable() and pts.isModified() and pts.providerType() == "ogr" and pts.source() == gj
    assert "в исходные данные не записаны" in res[0]["message"]
    copy = QgsVectorLayer(os.path.join(folder, "Опоры.gpkg"), "x", "ogr")
    assert copy.featureCount() == 3 and copy.crs() == UTM37
    info = gdal.Info(os.path.join(folder, "Рельеф.tif"), format="json")
    assert "37N" in info["coordinateSystem"]["wkt"], "растр не перепроецирован"
    assert dem.source() == os.path.join(src, "dem.vrt"), "без замены проект не должен меняться"
    pts.rollBack()


def test_all_layers_vrt_without_crs():
    p, src, gj, pts, dem, mem = all_layers_project()
    folder = os.path.join(OUT, "all_plain")
    res = saver.save_layers(p, [(dem, "raster", False)], folder, "gpkg", replace=False)
    assert res[0]["ok"] and res[0]["path"].endswith("Рельеф.tif"), res  # VRT → GeoTIFF, а не копия .vrt
    assert gdal.Info(res[0]["path"], format="json")["bands"][0]["type"] == "Int16"


def test_source_protection():
    """Запись в папку с исходниками и перезапись не затирают файлы, из которых читают слои."""
    p, src, gj, pts, dem, mem = all_layers_project()
    before = open(gj, encoding="utf-8").read()
    items = [(pts, "vector", False)]
    res = saver.save_layers(p, items, src, "geojson", overwrite=True, replace=False)
    assert res[0]["ok"] and res[0]["path"].endswith("Опоры_2.geojson"), res
    assert open(gj, encoding="utf-8").read() == before
    # GeoPackage, из которого читает слой проекта: его таблица не перезаписывается
    folder = os.path.join(OUT, "protect_gpkg")
    res = saver.save_layers(p, items, folder, "gpkg_single", gpkg_name="data", replace=True)
    assert pts.source().endswith("data.gpkg|layername=Опоры"), pts.source()
    res = saver.save_layers(p, items, folder, "gpkg_single", gpkg_name="data", overwrite=True, replace=False)
    assert res[0]["path"].endswith("→ Опоры_2"), res
    assert pts.featureCount() == 2


def test_all_layers_replace():
    p, src, gj, pts, dem, mem = all_layers_project()
    items, _ = saver.find_layers(p, temporary_only=False)
    res = saver.save_layers(p, items, os.path.join(OUT, "all_replace"), "gpkg", crs=UTM37, replace=True)
    assert all(r["ok"] for r in res), res
    for layer in (pts, dem, mem):
        assert layer.isValid() and layer.crs() == UTM37 and "all_replace" in layer.source(), layer.source()
    assert os.path.exists(gj), "исходный файл должен остаться на месте"


def _font_by_license(kind):
    """Файл системного шрифта с нужным видом лицензии и его семейство."""
    from temp_layers_to_folder import fonts

    for path in sorted(glob.glob("/System/Library/Fonts/Supplemental/*.ttf")):
        fams, lic, copyright_text, fs_type = fonts.read_font(path)
        if fams and fonts.license_kind(lic, copyright_text, fs_type) == kind:
            return path, sorted(fams)[0]
    raise AssertionError("нет шрифта с лицензией " + kind)


def _lep_project():
    """Проект «Проект ЛЭП/Проект ЛЭП.qgz»: слой в памяти со своим SVG и подписью
    свободным шрифтом, файл GeoJSON с платным шрифтом, лишний слой, подложка,
    макет с картинкой, надписью шрифтом без лицензии и картой."""
    from qgis.core import (QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutItemPicture,
                           QgsMarkerSymbol, QgsPalLayerSettings, QgsPrintLayout, QgsRectangle,
                           QgsSingleSymbolRenderer, QgsSvgMarkerSymbolLayer, QgsTextFormat,
                           QgsVectorLayerSimpleLabeling)
    from qgis.PyQt.QtGui import QFont

    from temp_layers_to_folder import fonts

    font_dir = os.path.join(OUT, "fontdir")
    os.makedirs(font_dir, exist_ok=True)
    fams = {}
    for kind in (fonts.FREE, fonts.PAID):
        path, fams[kind] = _font_by_license(kind)
        shutil.copy(path, font_dir)

    def labels(layer, family):
        pal = QgsPalLayerSettings()
        pal.fieldName = "'x'"
        pal.isExpression = True
        fmt = QgsTextFormat()
        fmt.setFont(QFont(family))
        pal.setFormat(fmt)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        layer.setLabelsEnabled(True)

    svg = os.path.join(OUT, "assets", "знак.svg")
    os.makedirs(os.path.dirname(svg), exist_ok=True)
    with open(svg, "w") as fh:
        fh.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                 '<circle cx="5" cy="5" r="4" fill="red"/></svg>')

    p = QgsProject.instance()
    p.clear()
    p.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
    src, gj, vrt = make_permanent_sources()
    mem = QgsVectorLayer("Point?crs=EPSG:4326", "Черновик", "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.2, 48.6)))
    mem.dataProvider().addFeature(f)
    symbol = QgsMarkerSymbol()
    symbol.changeSymbolLayer(0, QgsSvgMarkerSymbolLayer(svg))
    mem.setRenderer(QgsSingleSymbolRenderer(symbol))
    labels(mem, fams[fonts.FREE])
    pts = QgsVectorLayer(gj, "Опоры", "ogr")
    labels(pts, fams[fonts.PAID])
    extra = QgsVectorLayer(gj, "Лишний", "ogr")
    xyz = QgsRasterLayer("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19&zmin=0",
                         "OpenStreetMap", "wms")
    p.addMapLayers([mem, pts, extra, xyz])
    layout = QgsPrintLayout(p)
    layout.initializeDefaults()
    layout.setName("Лист 1")
    pic = QgsLayoutItemPicture(layout)
    pic.setPicturePath(svg)
    layout.addLayoutItem(pic)
    label = QgsLayoutItemLabel(layout)
    fmt = QgsTextFormat()
    fmt.setFont(QFont("НетТакогоШрифта"))
    label.setTextFormat(fmt)
    layout.addLayoutItem(label)
    lmap = QgsLayoutItemMap(layout)
    lmap.attemptResize(QgsLayoutSize(100, 100))
    lmap.setExtent(QgsRectangle(39.0, 48.4, 39.4, 48.8))
    layout.addLayoutItem(lmap)
    p.layoutManager().addLayout(layout)
    folder = os.path.join(OUT, "Проект ЛЭП")
    os.makedirs(folder, exist_ok=True)
    return p, folder, font_dir, fams, svg, src, gj, (mem, pts, extra, xyz)


def test_font_license_kind():
    from temp_layers_to_folder.fonts import FREE, PAID, UNKNOWN, license_kind

    assert license_kind("Licensed under the Open Font License, version 1.1", "", 0) == FREE
    assert license_kind("", "Copyright 2015 ... with Reserved Font Name X. SIL Open Font License", 0) == FREE
    assert license_kind("http://www.apache.org/licenses/LICENSE-2.0", "", 8) == FREE
    assert license_kind("You may use this font as permitted by the EULA", "", 8) == PAID
    assert license_kind("", "Copyright (c) Foundry", 4) == PAID  # «только просмотр и печать»
    assert license_kind("", "Copyright (c) Someone", 0) == UNKNOWN


def test_consolidate_project():
    """Сборка: data_all рядом с проектом, проект переключён и сохранён, архив со
    свободными шрифтами; после удаления исходников проект открывается."""
    import datetime
    import zipfile

    from qgis.core import QgsLayoutItemMap, QgsLayoutItemPicture

    from temp_layers_to_folder import fonts, packager

    p, folder, font_dir, fams, svg, src, gj, (mem, pts, extra, xyz) = _lep_project()
    try:
        packager.consolidate_project(p, [], "gpkg")
        raise AssertionError("ожидалась ошибка для несохранённого проекта")
    except RuntimeError as e:
        assert "сохраните проект" in str(e)

    proj = os.path.join(folder, "Проект ЛЭП.qgz")
    assert p.write(proj)
    day = datetime.date(2026, 9, 18)
    res = packager.consolidate_project(p, [(mem, "memory", True), (pts, "vector", False)], "gpkg",
                                       crs=UTM37, font_dirs=[font_dir], today=day)

    # 1. data_all рядом с проектом, проект переключён, пересчитан и сохранён
    data = os.path.join(folder, "data_all")
    assert res["data_dir"] == data
    for rel in ("Черновик.gpkg", "Опоры.gpkg", "images/знак.svg"):
        assert os.path.isfile(os.path.join(data, rel)), rel
    for layer in (mem, pts):
        assert layer.source().startswith(data) and layer.crs() == UTM37, layer.source()
    assert extra.source() == gj, "неотмеченный слой не трогаем"
    assert p.crs() == UTM37 and not p.isDirty() and p.fileName() == proj
    lmap = [i for i in p.layoutManager().layoutByName("Лист 1").items() if isinstance(i, QgsLayoutItemMap)][0]
    assert 400000 < lmap.extent().center().x() < 600000, lmap.extent().toString()
    assert mem.renderer().symbol().symbolLayer(0).path() == os.path.join(data, "images", "знак.svg")

    # «удаляем всё остальное» — проект продолжает работать
    shutil.move(src, src + "_удалено")
    shutil.move(os.path.dirname(svg), os.path.dirname(svg) + "_удалено")
    p3 = QgsProject()
    assert p3.read(proj)
    for layer in (mem, pts):
        l3 = p3.mapLayer(layer.id())
        assert l3.isValid() and l3.featureCount() > 0 and l3.crs() == UTM37, l3.source()
    assert os.path.isfile(p3.mapLayer(mem.id()).renderer().symbol().symbolLayer(0).path())
    pic3 = [i for i in p3.layoutManager().layoutByName("Лист 1").items() if isinstance(i, QgsLayoutItemPicture)][0]
    assert not pic3.isMissingImage()
    shutil.move(src + "_удалено", src)
    shutil.move(os.path.dirname(svg) + "_удалено", os.path.dirname(svg))

    # 2. архив рядом с проектом
    zip_path = os.path.join(folder, "Проект ЛЭП_архив_2026-09-18.zip")
    assert res["zip"] == zip_path and os.path.isfile(zip_path)
    names = zipfile.ZipFile(zip_path).namelist()
    root = "Проект ЛЭП_архив_2026-09-18/"
    for rel in ("Проект ЛЭП.qgz", "data_all/Черновик.gpkg", "data_all/Опоры.gpkg",
                "data_all/images/знак.svg", "Состав.txt"):
        assert root + rel in names, (rel, names)
    font_files = [n for n in names if "/fonts/" in n]
    assert len(font_files) == 1 and font_files[0].startswith(root + "data_all/fonts/"), font_files
    assert len(os.listdir(os.path.join(data, "fonts"))) == 1  # только свободный, и на диске тоже
    assert list(res["fonts"]["free"]) == [fams[fonts.FREE]]
    assert res["fonts"]["paid"] == [fams[fonts.PAID]] and res["fonts"]["missing"] == ["НетТакогоШрифта"]
    assert not [n for n in names if n.endswith(("-wal", "-shm"))], names
    assert not [n for n in os.listdir(folder) if n.startswith(".")], "временная копия проекта не удалена"
    assert res["excluded"] == ["Лишний"] and res["online"] == ["OpenStreetMap"]
    readme = zipfile.ZipFile(zip_path).read(root + "Состав.txt").decode("utf-8-sig")
    assert "Опоры — data_all/Опоры.gpkg" in readme and "платные" in readme and "EPSG:32637" in readme

    # архив открывается в другом месте; лишнего слоя в нём нет, подложка есть
    unpacked = os.path.join(OUT, "unzipped")
    zipfile.ZipFile(zip_path).extractall(unpacked)
    p4 = QgsProject()
    assert p4.read(os.path.join(unpacked, root, "Проект ЛЭП.qgz"))
    assert p4.mapLayer(extra.id()) is None and p4.mapLayer(xyz.id()) is not None
    for layer in (mem, pts):
        l4 = p4.mapLayer(layer.id())
        assert l4.isValid() and "unzipped" in l4.source() and l4.labelsEnabled(), l4.source()

    # повторная сборка: слои уже в data_all — не копируются заново; архив _2
    res2 = packager.consolidate_project(p, [(mem, "vector", False), (pts, "vector", False)], "gpkg",
                                        crs=UTM37, include_fonts=False, today=day)
    assert res2["results"] == [] and sorted(res2["already"]) == ["Опоры", "Черновик"]
    assert res2["zip"].endswith("Проект ЛЭП_архив_2026-09-18_2.zip")
    assert not [n for n in os.listdir(data) if "_2." in n], os.listdir(data)


def test_dialog_package():
    """Режим сборки в окне: предлагает сохранить проект, спрашивает подтверждение,
    собирает data_all и архив рядом с проектом."""
    from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox

    from temp_layers_to_folder import dialog as dlg_mod

    p = make_project()
    folder = os.path.join(OUT, "Диалог")
    os.makedirs(folder, exist_ok=True)
    saved_as = os.path.join(folder, "Диалог.qgz")

    class Bar:
        messages = []

        def pushMessage(self, *a, **k):
            self.messages.append(a)

    class SaveAction:
        def trigger(self):
            p.write(saved_as)

    class Iface:
        def __init__(self):
            self.w, self.bar = QMainWindow(), Bar()

        def mainWindow(self): return self.w
        def messageBar(self): return self.bar
        def layerTreeView(self): return None
        def actionSaveProject(self): return SaveAction()

    iface = Iface()
    d = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    d.mode_package.setChecked(True)
    assert d.btn_save.text() == "Собрать" and d.layers_box.title() == "Слои проекта"
    assert d.replace.isHidden() and d.overwrite.isHidden() and d.folder.isHidden()
    assert "ещё не сохранён" in d.package_hint.text() and d.btn_open.isHidden()
    assert len(list(d._items())) == 6  # все слои проекта
    d.pkg_fonts.setChecked(False)

    # «Нет» на вопросе — ничего не происходит
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    d.run()
    assert not p.fileName() and not os.path.exists(os.path.join(folder, "data_all"))

    asked = []

    def yes(*a, **k):
        asked.append(a[2] if len(a) > 2 else "")
        return QMessageBox.StandardButton.Yes

    QMessageBox.question = staticmethod(yes)
    if os.environ.get("SCREENSHOT_PACKAGE"):
        p.write(saved_as)
        d._update_package_hint()
        d.package_hint.setText(d.package_hint.text().replace(OUT, "/Users/me/Documents"))
        d.resize(620, 720)
        d.show()
        QgsApplication.processEvents()
        d.grab().save(os.environ["SCREENSHOT_PACKAGE"])
    d.run()
    assert p.fileName() == saved_as and not p.isDirty()
    assert any("data_all" in q for q in asked), asked  # подтверждение перед сборкой
    assert os.path.isdir(os.path.join(folder, "data_all"))
    zips = [n for n in os.listdir(folder) if n.endswith(".zip")]
    assert len(zips) == 1 and zips[0].startswith("Диалог_архив_"), (zips, d.log.toPlainText())
    assert "Архив:" in d.log.toPlainText() and not d.btn_open.isHidden()
    assert iface.bar.messages[-1][0] == "Сборка проекта"
    assert saver.find_temporary_layers(p) == [], "временные слои должны переехать в data_all"


def test_dialog_package_without_archive():
    """«Создать архив» выключен: слои собраны в data_all, проект сохранён, архива нет."""
    from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox

    from temp_layers_to_folder import dialog as dlg_mod

    p = make_project()
    folder = os.path.join(OUT, "Только data_all")
    os.makedirs(folder, exist_ok=True)
    proj = os.path.join(folder, "Сборка.qgz")
    assert p.write(proj)

    class Bar:
        messages = []

        def pushMessage(self, *a, **k):
            self.messages.append(a)

    class SaveAction:
        def trigger(self):
            p.write()

    class Iface:
        def __init__(self):
            self.w, self.bar = QMainWindow(), Bar()

        def mainWindow(self): return self.w
        def messageBar(self): return self.bar
        def actionSaveProject(self): return SaveAction()

    iface = Iface()
    d = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    d.mode_package.setChecked(True)
    d.pkg_archive.setChecked(False)
    d.pkg_fonts.setChecked(False)
    assert "архив" not in d.package_hint.text(), d.package_hint.text()
    asked = []

    def yes(*a, **k):
        asked.append(a[2] if len(a) > 2 else "")
        return QMessageBox.StandardButton.Yes

    QMessageBox.question = staticmethod(yes)
    d.run()
    data = os.path.join(folder, "data_all")
    assert os.path.isfile(os.path.join(data, "Постоянный.gpkg")), d.log.toPlainText()
    assert not [n for n in os.listdir(folder) if n.endswith(".zip")], os.listdir(folder)
    assert not [n for n in os.listdir(folder) if n.startswith(".")], "временная копия проекта"
    assert "архив" not in asked[-1] and "Архив:" not in d.log.toPlainText(), (asked, d.log.toPlainText())
    assert iface.bar.messages[-1][1] == "Готово: слои собраны в data_all", iface.bar.messages[-1]
    assert not p.isDirty() and saver.find_temporary_layers(p) == []
    p2 = QgsProject()
    assert p2.read(proj)
    assert all(l.source().startswith(data) for l in p2.mapLayers().values()), \
        [l.source() for l in p2.mapLayers().values()]
    # выбор запоминается
    d2 = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    assert d2.mode_package.isChecked() and not d2.pkg_archive.isChecked()
    d2.pkg_archive.setChecked(True)
    d2.mode_temp.setChecked(True)
    d2._save_settings()


def test_joins_relations_expressions():
    """Объединения, связи, выражения и подписи переживают замену и не задваиваются."""
    from qgis.core import (QgsField, QgsPalLayerSettings, QgsRelation,
                           QgsVectorLayerJoinInfo, QgsVectorLayerSimpleLabeling)

    p = QgsProject.instance()
    p.clear()
    parent = QgsVectorLayer("Point?crs=EPSG:4326&field=id:integer&field=name:string(20)", "Опоры", "memory")
    child = QgsVectorLayer("None?field=pid:integer&field=note:string(20)", "Заметки", "memory")
    for i in (1, 2):
        f = QgsFeature(parent.fields())
        f.setAttributes([i, "оп%d" % i])
        f.setGeometry(QgsGeometry.fromWkt("POINT(39 48)"))
        parent.dataProvider().addFeature(f)
        f = QgsFeature(child.fields())
        f.setAttributes([i, "заметка %d" % i])
        child.dataProvider().addFeature(f)
    p.addMapLayers([parent, child])
    join = QgsVectorLayerJoinInfo()
    join.setJoinLayer(child)
    join.setJoinFieldName("pid")
    join.setTargetFieldName("id")
    join.setJoinFieldNamesSubset(["note"])
    parent.addJoin(join)
    parent.addExpressionField('"id" * 10', QgsField("id10", QMetaType.Type.Int))
    rel = QgsRelation()
    rel.setId("r1")
    rel.setName("r1")
    rel.setReferencingLayer(child.id())
    rel.setReferencedLayer(parent.id())
    rel.addFieldPair("pid", "id")
    p.relationManager().addRelation(rel)
    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    parent.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    parent.setLabelsEnabled(True)

    folder = os.path.join(OUT, "joins")
    res = saver.save_layers(p, saver.find_temporary_layers(p), folder, "gpkg_single")
    assert all(r["ok"] for r in res), res

    names = [f.name() for f in parent.fields()]
    assert names.count("Заметки_note") == 1 and names.count("id10") == 1, names
    feat = next(parent.getFeatures())
    assert feat["Заметки_note"] == "заметка 1" and feat["id10"] == 10
    r = p.relationManager().relation("r1")
    assert r.isValid() and [f["note"] for f in r.getRelatedFeatures(feat)] == ["заметка 1"]
    assert parent.labelsEnabled() and parent.labeling().settings().fieldName == "name"
    saved = QgsVectorLayer(os.path.join(folder, "слои.gpkg") + "|layername=Опоры", "x", "ogr")
    assert "Заметки_note" not in saved.fields().names() and "id10" not in saved.fields().names()


def test_toolbar_buttons():
    """Кнопка на общей панели «Альтан-Эко» и на панели «Слои» появляется по одной
    и убирается при выгрузке."""
    from qgis.PyQt.QtWidgets import QDockWidget, QMainWindow, QToolBar

    import temp_layers_to_folder

    win = QMainWindow()
    dock = QDockWidget("Слои", win)
    dock.setObjectName("Layers")
    bar = QToolBar(dock)
    bar.addAction("Развернуть все")
    dock.setWidget(bar)

    menus = []

    class Iface:
        def mainWindow(self): return win
        def layerTreeView(self): return None
        def addPluginToMenu(self, m, a): menus.append((m, a.text()))
        def pluginMenu(self): return _PLUGIN_MENU
        def removePluginMenu(self, m, a): menus.remove((m, a.text()))

        def addToolBar(self, name):
            return win.addToolBar(name)

    def own_toolbars():
        return [t for t in win.findChildren(QToolBar) if t.objectName() == "AltanEcoToolbar"]

    for _ in range(2):  # повторная загрузка не должна дублировать кнопки
        plugin = temp_layers_to_folder.classFactory(Iface())
        plugin.initGui()
        texts = [a.text() for a in bar.actions()]
        assert texts[-1] == "Сохранить временные слои…" and texts.count(texts[-1]) == 1, texts
        assert bar.actions()[-2].isSeparator()
        own = own_toolbars()
        assert len(own) == 1 and own[0].windowTitle() == "Альтан-Эко", own
        assert menus == [("&Альтан-Эко", "Сохранить временные слои…")], menus  # общее подменю компании
        assert [a.text() for a in own[0].actions()] == ["Сохранить временные слои…"]
        assert own[0].isMovable() and own[0].isFloatable()
        plugin.unload()
        QgsApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # выполнить deleteLater
        assert [a.text() for a in bar.actions()] == ["Развернуть все"], [a.text() for a in bar.actions()]
        assert own_toolbars() == [] and menus == []


def test_plugin_window_non_modal():
    """Окно немодальное и одно: повторное нажатие поднимает тот же экземпляр,
    выгрузка плагина закрывает окно."""
    from qgis.PyQt.QtWidgets import QMainWindow

    import temp_layers_to_folder

    make_project()
    win = QMainWindow()

    class Bar:
        def pushMessage(self, *a, **k): pass

    class Iface:
        def mainWindow(self): return win
        def messageBar(self): return Bar()
        def layerTreeView(self): return None
        def addToolBar(self, name): return win.addToolBar(name)
        def addPluginToMenu(self, m, a): pass
        def pluginMenu(self): return _PLUGIN_MENU
        def removePluginMenu(self, m, a): pass

    plugin = temp_layers_to_folder.classFactory(Iface())
    plugin.initGui()
    plugin.run()
    d = plugin.dialog
    assert d.isVisible() and not d.isModal()
    plugin.run()
    assert plugin.dialog is d and d.isVisible()
    plugin.unload()
    assert plugin.dialog is None and not d.isVisible()


def test_dialog():
    from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox

    import temp_layers_to_folder
    from temp_layers_to_folder import dialog as dlg_mod

    class Bar:
        messages = []

        def pushMessage(self, *a, **k):
            self.messages.append(a)

    class Iface:
        def __init__(self):
            self.w, self.bar = QMainWindow(), Bar()

        def mainWindow(self):
            return self.w

        def messageBar(self):
            return self.bar

        def layerTreeView(self):
            return None

        def addToolBar(self, name):
            return self.w.addToolBar(name)

        def addPluginToMenu(self, m, a): pass

        def pluginMenu(self): return _PLUGIN_MENU

        def removePluginMenu(self, m, a): pass

    make_project()
    iface = Iface()
    plugin = temp_layers_to_folder.classFactory(iface)
    plugin.initGui()
    d = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    d.folder.setFilePath(os.path.join(OUT, "dialog"))
    d.crs.setCrs(UTM37)
    # два режима: временные слои и сборка в data_all; «Создать архив» — только у сборки
    assert sorted(d._radios) == [dlg_mod.MODE_PACKAGE, dlg_mod.MODE_TEMP] and not hasattr(d, "mode_all")
    assert not hasattr(d, "structure")
    d.mode_package.setChecked(True)
    assert d.layers_box.title() == "Слои проекта" and d.pkg_archive.isEnabled()
    assert [it.text().split("   ")[0] for it in d._items()][-1] == "Постоянный"
    assert len(list(d._items())) == 6
    d.mode_temp.setChecked(True)
    assert d.replace.isChecked() and not d.pkg_archive.isEnabled()
    items = list(d._items())
    assert len(items) == 5
    items[0].setCheckState(dlg_mod.UNCHECKED)
    if os.environ.get("SCREENSHOT"):  # снимок окна для README
        d.folder.setFilePath("/Users/me/Documents/Проект/data")
        d.crs.setCrs(QgsCoordinateReferenceSystem("EPSG:32637"))
        d.resize(620, 700)
        d.show()
        QgsApplication.processEvents()
        d.grab().save(os.environ["SCREENSHOT"])
        d.folder.setFilePath(os.path.join(OUT, "dialog"))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    d.run()
    assert "4 из 4" in iface.bar.messages[-1][1], iface.bar.messages
    assert len(list(d._items())) == 1  # остался только неотмеченный слой
    plugin.unload()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok   ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL ", name)
            traceback.print_exc()
    print("\n{} из {} проверок пройдено".format(len(tests) - failed, len(tests)))
    # Папка профиля, созданная initQgis() для тестовой организации (qgis.db, стили), — только наша.
    leftover = os.path.dirname(os.path.dirname(os.path.dirname(
        QgsApplication.qgisSettingsDirPath().rstrip("/"))))
    app.exitQgis()
    if os.path.basename(leftover) == "temp-layers-tests":
        shutil.rmtree(leftover, ignore_errors=True)
    sys.exit(1 if failed else 0)
