"""Проверки плагина без интерфейса QGIS. Запуск: tests/run_tests.sh

Скрипт не трогает профиль QGIS: настройки и пользовательская СК
пишутся во временный профиль tests/_profile, результаты — в tests/_out.
"""

import glob
import os
import shutil
import sys
import traceback
import unicodedata

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


def test_layout_fonts():
    """Шрифты легенды и сетки карты в макете. В QGIS 3.44 цепочка
    legend.style(…).textFormat() роняла QGIS при сборке проекта с легендой:
    style() отдаёт временный объект, и textFormat() ссылается на удалённое."""
    from qgis.core import (QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemMapGrid,
                           QgsPrintLayout, QgsTextFormat)
    from qgis.PyQt.QtGui import QFont

    from temp_layers_to_folder import packager

    p = QgsProject.instance()
    p.clear()
    p.addMapLayer(QgsVectorLayer("Point?crs=EPSG:4326", "точки", "memory"))
    layout = QgsPrintLayout(p)
    layout.initializeDefaults()
    legend = QgsLayoutItemLegend(layout)
    comp = packager._legend_component("Title")
    style = legend.style(comp)
    tf = style.textFormat()
    tf.setFont(QFont("ЛегендаТест"))
    style.setTextFormat(tf)
    legend.setStyle(comp, style)
    layout.addLayoutItem(legend)
    lmap = QgsLayoutItemMap(layout)
    layout.addLayoutItem(lmap)
    grid = QgsLayoutItemMapGrid("сетка", lmap)
    gtf = QgsTextFormat()
    gtf.setFont(QFont("СеткаТест"))
    grid.setAnnotationTextFormat(gtf)
    lmap.grids().addGrid(grid)
    p.layoutManager().addLayout(layout)
    fams = packager.used_font_families(p)
    assert {"ЛегендаТест", "СеткаТест"} <= fams, fams


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


def _geojson(path, x=39.1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"n":1},'
                 '"geometry":{"type":"Point","coordinates":[%s,48.5]}}]}' % x)
    return path


def _svg_symbol(layer, svg):
    from qgis.core import QgsMarkerSymbol, QgsSingleSymbolRenderer, QgsSvgMarkerSymbolLayer

    os.makedirs(os.path.dirname(svg), exist_ok=True)
    with open(svg, "w") as fh:
        fh.write('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
                 '<circle cx="5" cy="5" r="4" fill="red"/></svg>')
    symbol = QgsMarkerSymbol()
    symbol.changeSymbolLayer(0, QgsSvgMarkerSymbolLayer(svg))
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))


def _tif(path, srs=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(path, 10, 10, 1, gdal.GDT_Int16)
    ds.SetGeoTransform([39.0, 0.01, 0, 48.8, 0, -0.01])
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(4326)
    if srs:
        ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).Fill(120)
    ds = None
    return path


def structure_project(root):
    """Проект в root/Проект, данные в его подпапках и во внешней папке root/Архив.
    Два слоя «Слой» с одинаковым именем — в разных подпапках. У «Опор» значок из
    папки проекта, у «Черновика» — из папки вне проекта."""
    p = QgsProject.instance()
    p.clear()
    shutil.rmtree(root, ignore_errors=True)
    proj_dir = os.path.join(root, "Проект")
    layers = {
        "opory": QgsVectorLayer(_geojson(os.path.join(proj_dir, "Данные", "Вектор", "Опоры.geojson")),
                                "Опоры", "ogr"),
        "dem": QgsRasterLayer(_tif(os.path.join(proj_dir, "Данные", "Растры", "dem.tif")), "Рельеф", "gdal"),
        "top": QgsVectorLayer(_geojson(os.path.join(proj_dir, "Карта.geojson")), "Карта", "ogr"),
        "topo": QgsVectorLayer(_geojson(os.path.join(root, "Архив", "Топо", "a.geojson")), "Слой", "ogr"),
        "soil": QgsVectorLayer(_geojson(os.path.join(root, "Архив", "Почвы", "b.geojson"), 39.2), "Слой", "ogr"),
        "mem": QgsVectorLayer("Point?crs=EPSG:4326", "Черновик", "memory"),
    }
    assert all(l.isValid() for l in layers.values())
    _svg_symbol(layers["opory"], os.path.join(proj_dir, "Данные", "Значки", "опора.svg"))
    _svg_symbol(layers["mem"], os.path.join(root, "Значки", "знак.svg"))
    p.addMapLayers(list(layers.values()))
    return p, proj_dir, layers


class _Home:
    """Домашняя папка на время проверки: внешние пути считаются от неё."""

    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old = os.environ.get("HOME")
        os.environ["HOME"] = self.path

    def __exit__(self, *exc):
        os.environ["HOME"] = self.old


def test_structure_paths():
    """Правила раскладки: data/… → data_all/…, прямо в проекте → data_all/,
    другая папка проекта → data_all/<путь>, вне проекта → external_links/<обрезанный путь>."""
    import ntpath

    from temp_layers_to_folder import copier as c

    def ext(d, **kw):
        return c.external_subdir(d, "C:\\Users\\roman", pm=ntpath, **kw)

    assert ext("C:\\Users\\roman\\Desktop\\ЛЭП\\узел7") == "Desktop/ЛЭП/узел7"
    assert ext("D:\\GIS") == "D/GIS"
    assert ext("\\\\server\\share\\a") == "_server_share/a"
    assert ext("C:\\Users\\roman\\Desktop\\ЛЭП\\узел7\\2024\\июль") == "узел7/2024/июль"  # последние 3
    assert ext("D:\\a\\b\\c\\d") == "D/b/c/d"  # имя диска не считается уровнем
    assert ext("D:\\a\\b\\c\\d", max_depth=2) == "D/c/d"
    assert ext("C:\\Program Files\\QGIS\\svg") == "C/QGIS/svg"  # системные папки выбрасываются
    assert ext("C:\\Users\\roman\\AppData\\Roaming\\QGIS") == "Roaming/QGIS"
    assert ext("C:\\Users\\roman") == ""
    home = "/Users/user"
    yd = "/Volumes/YD/Yandex.Disk.localized/Работа/(Заказчик)/ВЛ/3. Карты/9. ГИС/data/02. КФ"
    assert c.external_subdir(yd, home) == "YD/9. ГИС/data/02. КФ"
    assert c.external_subdir("/Users/user/Downloads", home) == "Downloads"
    assert c.external_subdir("/usr/share/gis", home) == "share/gis"

    P = "/work/Проект"

    def t(d):
        return c.target_subdir(d, P, P + "/data_all", home=home)

    assert t(P + "/data/ЛЕС/2024") == "ЛЕС/2024"  # 1
    assert t(P + "/data") == ""
    assert t(P) == ""  # 2
    assert t(P + "/Рабочие файлы/gpkg") == "Рабочие файлы/gpkg"  # 3
    assert t(P + "/data_all/ЛЕС") == "ЛЕС"  # уже собранное остаётся на месте
    assert t("/Users/user/Desktop/ЛЭП/узел7") == "external_links/Desktop/ЛЭП/узел7"  # 4
    assert t("/Users/user") == "external_links"


def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _vector_file(path, driver, layer_name=None, n=2, append=False):
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter

    mem = QgsVectorLayer("Point?crs=EPSG:4326&field=n:integer", "src", "memory")
    for i in range(n):
        f = QgsFeature(mem.fields())
        f.setAttributes([i])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.1 + i / 10, 48.5)))
        mem.dataProvider().addFeature(f)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = driver
    if layer_name:
        opts.layerName = layer_name
    if append:
        opts.actionOnExistingFile = saver.CREATE_LAYER
    res = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), opts)
    assert res[0] == 0, res
    return path


def copy_project(root):
    """Проект со всеми видами источников из ТЗ. Возвращает (project, proj_dir, home, {ключ: слой})."""
    import zipfile

    from qgis.core import (QgsPalLayerSettings, QgsTextBackgroundSettings, QgsTextFormat,
                           QgsVectorLayerSimpleLabeling)

    p = QgsProject.instance()
    p.clear()
    shutil.rmtree(root, ignore_errors=True)
    P = os.path.join(root, "Проект")
    home = os.path.join(root, "home")
    j = os.path.join
    # 1. shapefile в data/ЛЕС со всеми спутниками; рядом — чужие файлы
    shp = _vector_file(j(P, "data", "ЛЕС", "лес.shp"), "ESRI Shapefile")
    for ext in (".qpj", ".shp.xml", ".qml", ".qmd"):
        _write(j(P, "data", "ЛЕС", "лес" + ext), "<x/>" if "ml" in ext or ext == ".qmd" else "")
    _write(j(P, "data", "ЛЕС", "лесной.dbf"))
    _write(j(P, "data", "ЛЕС", "лес.txt"), "заметки")
    # GeoPackage с двумя слоями — копируется один раз
    gpkg = _vector_file(j(P, "data", "ЛЕС", "2024", "участки.gpkg"), "GPKG", "a")
    _vector_file(gpkg, "GPKG", "b", n=3, append=True)
    # 2. прямо в папке проекта; 3. в другой папке проекта — CSV с параметрами
    top = _vector_file(j(P, "граница.geojson"), "GeoJSON")
    csv = _write(j(P, "Рабочие файлы", "точки.csv"), "x;y;n\n39.1;48.5;1\n39.2;48.6;2\n")
    _write(j(P, "Рабочие файлы", "точки.csvt"), '"Real","Real","Integer"')
    # растр с .tfw, .aux.xml и внешними пирамидами; VRT на него
    dem = _tif(j(P, "data", "Рельеф", "dem.tif"))
    _write(dem + ".aux.xml", "<PAMDataset/>")
    _write(j(P, "data", "Рельеф", "dem.tfw"), "0.01\n0\n0\n-0.01\n39.005\n48.795\n")
    ds = gdal.Open(dem)
    ds.BuildOverviews("NEAREST", [2])
    ds = None
    vrt = j(P, "data", "Рельеф", "мозаика.vrt")
    gdal.BuildVRT(vrt, [dem])
    # NetCDF с двумя переменными: NETCDF:"файл":Band1
    two = gdal.GetDriverByName("GTiff").Create(j(root, "two.tif"), 5, 5, 2, gdal.GDT_Int16)
    two.SetGeoTransform([39.0, 0.01, 0, 48.8, 0, -0.01])
    two = None
    nc = j(P, "data", "клим", "t.nc")
    os.makedirs(os.path.dirname(nc))
    gdal.Translate(nc, j(root, "two.tif"), format="netCDF")
    # zip с GeoJSON внутри — /vsizip/; папка шейпов как один источник
    zpath = j(P, "data", "архив.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.write(_vector_file(j(root, "in.geojson"), "GeoJSON"), "in.geojson")
    folder = j(P, "data", "папка_shp")
    _vector_file(j(folder, "x.shp"), "ESRI Shapefile")
    _vector_file(j(folder, "y.shp"), "ESRI Shapefile")
    # 4. вне проекта: в домашней папке; два разных файла с одинаковым обрезанным путём
    trees = _vector_file(j(home, "Desktop", "ЛЭП", "узел7", "trees.geojson"), "GeoJSON")
    dup1 = _vector_file(j(home, "a", "x", "y", "z", "реки.geojson"), "GeoJSON")
    dup2 = _vector_file(j(home, "b", "x", "y", "z", "реки.geojson"), "GeoJSON")

    L = {
        "les": QgsVectorLayer(shp, "Лес", "ogr"),
        "ga": QgsVectorLayer(gpkg + "|layername=a", "Участки А", "ogr"),
        "gb": QgsVectorLayer(gpkg + "|layername=b|subset=\"n\" > 0", "Участки Б", "ogr"),
        "top": QgsVectorLayer(top, "Граница", "ogr"),
        "csv": QgsVectorLayer("file://{}?type=csv&delimiter=;&xField=x&yField=y&crs=EPSG:4326".format(csv),
                              "Точки CSV", "delimitedtext"),
        "dem": QgsRasterLayer(dem, "Рельеф", "gdal"),
        "vrt": QgsRasterLayer(vrt, "Мозаика", "gdal"),
        "nc": QgsRasterLayer('NETCDF:"{}":Band1'.format(nc), "Климат", "gdal"),
        "zip": QgsVectorLayer("/vsizip/{}/in.geojson".format(zpath), "Из архива", "ogr"),
        "dir": QgsVectorLayer(folder + "|layername=y", "Из папки", "ogr"),
        "trees": QgsVectorLayer(trees, "Деревья", "ogr"),
        "dup1": QgsVectorLayer(dup1, "Реки 1", "ogr"),
        "dup2": QgsVectorLayer(dup2, "Реки 2", "ogr"),
        "mem": QgsVectorLayer("Point?crs=EPSG:4326", "Черновик", "memory"),
    }
    bad = [k for k, l in L.items() if not l.isValid()]
    assert not bad, bad
    _svg_symbol(L["les"], j(P, "значки", "дерево.svg"))
    pal = QgsPalLayerSettings()
    pal.fieldName = "n"
    fmt = QgsTextFormat()
    bg = QgsTextBackgroundSettings()
    bg.setEnabled(True)
    bg.setType(QgsTextBackgroundSettings.ShapeType.ShapeSVG)
    bg.setSvgFile(_write(j(home, "значки", "фон.svg"),
                         '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'))
    fmt.setBackground(bg)
    pal.setFormat(fmt)
    L["top"].setLabeling(QgsVectorLayerSimpleLabeling(pal))
    L["top"].setLabelsEnabled(True)
    p.addMapLayers(list(L.values()))
    return p, P, home, L


def _label_svg(layer):
    # по шагам: цепочка временных объектов роняет Python в QGIS 3.44
    labeling = layer.labeling()
    settings = labeling.settings()
    fmt = settings.format()
    return fmt.background().svgFile()


def test_copy_structure():
    """Сборка со структурой папок по ТЗ: файлы копируются как есть со спутниками,
    контейнер — один раз, в адресе слоя меняется только путь; внешние файлы — в
    external_links, совпавшие имена получают _2 с записью в отчёте; растры — в
    data_all/raster; значки — в data_all/symbols. Проект открывается с диска и
    без исходных файлов."""
    import zipfile

    from temp_layers_to_folder import packager

    j = os.path.join
    root = os.path.join(OUT, "copy")
    p, P, home, L = copy_project(root)
    proj = j(P, "Проект.qgz")
    assert p.write(proj)
    items, skipped = saver.find_layers(p, temporary_only=False)
    assert not skipped, skipped
    with _Home(home):
        res = packager.consolidate_project(p, items, "native", include_fonts=False, keep_structure=True)
    D = j(P, "data_all")
    by_name = {r["name"]: r for r in res["results"]}
    assert all(r["ok"] for r in res["results"]), [r for r in res["results"] if not r["ok"]]

    def listing(*sub):
        return sorted(os.listdir(j(D, *sub)))

    # 1. data/ЛЕС → data_all/ЛЕС: shapefile со всеми спутниками и стилями, без чужих файлов
    assert listing("ЛЕС") == sorted(["2024", "лес.shp", "лес.shx", "лес.dbf", "лес.prj", "лес.cpg",
                                     "лес.qpj", "лес.shp.xml", "лес.qml", "лес.qmd"]), listing("ЛЕС")
    # GeoPackage с двумя слоями — одна копия, оба слоя на ней, фильтр слоя сохранён
    assert [n for n in listing("ЛЕС", "2024") if not n.endswith(("-wal", "-shm"))] == ["участки.gpkg"]
    gcopy = j(D, "ЛЕС", "2024", "участки.gpkg")
    assert L["ga"].source() == gcopy + "|layername=a", L["ga"].source()
    assert L["gb"].source() == gcopy + '|layername=b|subset="n" > 0', L["gb"].source()
    assert L["gb"].featureCount() == 2
    # 2. прямо в проекте → data_all/; 3. другая папка проекта → data_all/<путь>, параметры CSV те же
    assert L["top"].source() == j(D, "граница.geojson")
    assert listing("Рабочие файлы") == ["точки.csv", "точки.csvt"]
    assert "delimiter=;&xField=x&yField=y&crs=EPSG:4326" in L["csv"].source(), L["csv"].source()
    assert L["csv"].isValid() and L["csv"].featureCount() == 2
    # растр со спутниками — в raster/; VRT — в GeoTIFF на своё место там же
    assert listing("raster", "Рельеф") == ["dem.tfw", "dem.tif", "dem.tif.aux.xml", "dem.tif.ovr",
                                           "мозаика.qml", "мозаика.tif"], listing("raster", "Рельеф")
    assert L["nc"].source() == 'NETCDF:"{}":Band1'.format(j(D, "raster", "клим", "t.nc")) and L["nc"].isValid()
    assert L["zip"].source().startswith("/vsizip/" + j(D, "архив.zip")) and L["zip"].isValid()
    assert listing("папка_shp") == sorted(os.listdir(j(P, "data", "папка_shp")))  # папка целиком
    assert L["dir"].source() == j(D, "папка_shp") + "|layername=y"
    # 4. вне проекта — external_links с обрезкой; совпавшее имя — _2 и запись в отчёте
    assert L["trees"].source() == j(D, "external_links", "Desktop", "ЛЭП", "узел7", "trees.geojson")
    assert listing("external_links", "x", "y", "z") == ["реки.geojson", "реки_2.geojson"]
    assert "скопирован как «реки_2.geojson»" in by_name["Реки 2"]["message"], by_name["Реки 2"]
    assert not by_name["Реки 1"]["message"]
    # слой в памяти — в корень data_all в выбранном формате
    assert L["mem"].source().startswith(j(D, "Черновик.gpkg"))
    # значки символов и фона подписей — в data_all/symbols
    assert listing("symbols") == ["дерево.svg", "фон.svg"]
    assert L["les"].renderer().symbol().symbolLayer(0).path() == j(D, "symbols", "дерево.svg")
    assert _label_svg(L["top"]) == j(D, "symbols", "фон.svg")
    # исходные файлы на месте
    assert os.path.isfile(j(P, "data", "ЛЕС", "лес.shp")) and os.path.isfile(j(home, "Desktop", "ЛЭП",
                                                                                 "узел7", "trees.geojson"))

    # проект открывается с диска и без исходных данных
    shutil.move(j(P, "data"), j(root, "data_убрана"))
    shutil.move(home, home + "_убрана")
    p2 = QgsProject()
    assert p2.read(proj)
    for key, layer in L.items():
        l2 = p2.mapLayer(layer.id())
        assert l2.isValid(), (key, l2.source())
        if isinstance(l2, QgsVectorLayer):
            assert l2.featureCount() > 0 or key == "mem", (key, l2.source())
    assert os.path.isfile(_label_svg(p2.mapLayer(L["top"].id())))
    shutil.move(j(root, "data_убрана"), j(P, "data"))
    shutil.move(home + "_убрана", home)

    # Состав.txt и архив — с той же раскладкой
    z = zipfile.ZipFile(res["zip"])
    top = os.path.splitext(os.path.basename(res["zip"]))[0] + "/"
    for rel in ("ЛЕС/лес.shp", "ЛЕС/лес.qpj", "external_links/Desktop/ЛЭП/узел7/trees.geojson",
                "symbols/фон.svg", "папка_шп".replace("шп", "shp") + "/y.shp"):
        assert top + "data_all/" + rel in z.namelist(), rel
    readme = z.read(top + "Состав.txt").decode("utf-8-sig")
    assert "Лес — data_all/ЛЕС/лес.shp" in readme and "в data_all/symbols/" in readme, readme
    assert "Климат — data_all/raster/клим/t.nc" in readme and "Из папки — data_all/папка_shp" in readme, readme
    assert res["excluded"] == [], res["excluded"]  # все слои — в копии проекта для архива

    # повторная сборка: всё уже в data_all — ничего не копируется
    items, _ = saver.find_layers(p, temporary_only=False)
    res2 = packager.consolidate_project(p, items, "native", include_fonts=False, make_archive=False,
                                        keep_structure=True)
    assert res2["results"] == [] and len(res2["already"]) == len(items), res2["results"]


def test_copy_structure_crs():
    """Со структурой папок и сменой СК слой перепроецируется в выбранный формат
    на своё место в структуре: как есть его не скопировать."""
    from temp_layers_to_folder import packager

    root = os.path.join(OUT, "copy_crs")
    p, P, home, L = copy_project(root)
    assert p.write(os.path.join(P, "Проект.qgz"))
    with _Home(home):
        res = packager.consolidate_project(p, [(L["les"], "vector", False), (L["top"], "vector", False)],
                                           "native", crs=UTM37, include_fonts=False, make_archive=False,
                                           keep_structure=True)
    assert all(r["ok"] for r in res["results"]), res["results"]
    D = os.path.join(P, "data_all")
    assert L["les"].source().startswith(os.path.join(D, "ЛЕС", "лес.gpkg")) and L["les"].crs() == UTM37
    assert L["top"].source().startswith(os.path.join(D, "граница.gpkg"))


def test_copy_structure_crs_container_names():
    """Слои из одного GeoPackage при смене СК сохраняют свои имена — имя
    файла-контейнера («участки», «участки_2») их не заменяет."""
    from temp_layers_to_folder import packager

    root = os.path.join(OUT, "copy_crs_names")
    p, P, home, L = copy_project(root)
    kml = _write(os.path.join(P, "data", "kml", "точки.kml"), "".join(
        ['<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>']
        + ['<Folder><name>{}</name><Placemark><name>p</name><Point><coordinates>39.1,48.5</coordinates>'
           '</Point></Placemark></Folder>'.format(n) for n in ("A", "B")] + ["</Document></kml>"]))
    ka, kb = (QgsVectorLayer(kml + "|layername=" + n, "Слой " + n, "ogr") for n in ("A", "B"))
    assert ka.isValid() and kb.isValid()
    L["nc"].setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))  # в файле СК нет
    p.addMapLayers([ka, kb])
    assert p.write(os.path.join(P, "Проект.qgz"))
    with _Home(home):
        res = packager.consolidate_project(
            p, [(L["ga"], "vector", False), (L["gb"], "vector", False), (ka, "vector", False),
                (kb, "vector", False), (L["les"], "vector", False), (L["dir"], "vector", False),
                (L["nc"], "raster", False)],
            "native", crs=UTM37, include_fonts=False, make_archive=False, keep_structure=True)
    assert all(r["ok"] for r in res["results"]), res["results"]
    D = os.path.join(P, "data_all")

    def names(*sub):
        return sorted(n for n in os.listdir(os.path.join(D, *sub)) if not n.endswith(("-wal", "-shm")))

    assert names("ЛЕС", "2024") == ["Участки А.gpkg", "Участки Б.gpkg"], names("ЛЕС", "2024")
    assert names("kml") == ["Слой A.gpkg", "Слой B.gpkg"], names("kml")
    assert "лес.gpkg" in names("ЛЕС")  # слой один в файле — имя файла
    # слой из папки шейпов — в её подпапку; переменная NetCDF — в raster/ на место файла
    assert names("папка_shp") == ["Из папки.gpkg"], names("папка_shp")
    assert [n for n in names("raster", "клим") if n.endswith(".tif")] == ["Климат.tif"], names("raster", "клим")


def _las(path, n=10):
    """Облако точек LAS 1.2 (формат точек 0, без СК) из n×n точек."""
    import struct

    os.makedirs(os.path.dirname(path), exist_ok=True)
    pts = [(500000 + i, 5380000 + k, float(i + k)) for i in range(n) for k in range(n)]
    xs, ys, zs = zip(*pts)
    hdr = bytearray(227)
    hdr[0:4] = b"LASF"
    struct.pack_into("<BB", hdr, 24, 1, 2)
    struct.pack_into("<HII", hdr, 94, 227, 227, 0)
    struct.pack_into("<BHII", hdr, 104, 0, 20, len(pts), len(pts))
    struct.pack_into("<12d", hdr, 131, 0.01, 0.01, 0.01, 0, 0, 0,
                     max(xs), min(xs), max(ys), min(ys), max(zs), min(zs))
    with open(path, "wb") as fh:
        fh.write(hdr)
        for x, y, z in pts:
            fh.write(struct.pack("<iiiHBBbBH", round(x * 100), round(y * 100), round(z * 100),
                                 0, 0, 1, 0, 0, 0))
    return path


def _mesh(path):
    return _write(path, "MESH2D\nND 1 39.0 48.5 0\nND 2 39.1 48.5 0\nND 3 39.1 48.6 0\n"
                        "ND 4 39.0 48.6 0\nE4Q 1 1 2 3 4 1\n")


def _raster_gpkg(path, table, append=False):
    src = _tif(os.path.join(OUT, "_src.tif"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    gdal.Translate(path, src, format="GPKG",
                   creationOptions=["RASTER_TABLE=" + table] + (["APPEND_SUBDATASET=YES"] if append else []))
    return path


def split_project(root):
    """Проект с растрами по всем правилам раскладки, GeoPackage трёх видов
    (растр + вектор, из которого берётся только растр; растр + вектор, оба
    слоя в проекте; только растры), облаком точек, сетками и временным растром.
    Возвращает (project, proj_dir, home, {ключ: слой})."""
    from qgis.core import QgsMeshLayer, QgsPointCloudLayer

    p = QgsProject.instance()
    p.clear()
    shutil.rmtree(root, ignore_errors=True)
    j = os.path.join
    P, home = j(root, "Проект"), j(root, "home")
    road = _vector_file(j(P, "data", "Вектор", "дороги.geojson"), "GeoJSON")
    dem = _tif(j(P, "data", "Рельеф", "dem.tif"))
    ds = gdal.Open(dem)
    ds.BuildOverviews("NEAREST", [2])
    ds = None
    for name in ("dem.tif.aux.xml", "dem.tfw", "dem.rrd", "dem.tif.vat.dbf", "dem.prj", "dem.qml",
                 "dem.txt", "dem2.tfw"):
        _write(j(P, "data", "Рельеф", name), "<x/>" if name.endswith(("xml", "qml")) else "")
    mix = _raster_gpkg(j(P, "data", "База", "смесь.gpkg"), "dem")
    _vector_file(mix, "GPKG", "pts", append=True)
    both = _raster_gpkg(j(P, "data", "Оба", "оба.gpkg"), "dem")
    _vector_file(both, "GPKG", "pts", append=True)
    tiles = _raster_gpkg(j(P, "data", "Тайлы", "тайлы.gpkg"), "t1")
    _raster_gpkg(tiles, "t2", append=True)
    cloud = _las(j(P, "data", "Облака", "cloud.las"))
    mesh = _mesh(j(P, "data", "Сетка", "m.2dm"))
    mesh2 = _mesh(j(P, "data", "Сетка", "m2.2dm"))
    depth = _write(j(P, "data", "Сетка", "m2_depth.dat"),
                   'DATASET\nOBJTYPE "mesh2d"\nRT_JULIAN 2431887.0\nBEGSCL\nND 4\nNC 1\nNAME "Depth"\n'
                   "TIMEUNITS hours\nTS 0 0\n1\n2\n3\n4\nENDDS\n")
    tmp = _tif(j(QgsProcessingUtils.tempFolder(), "tests_split", "OUTPUT.tif"))
    L = {
        "road": QgsVectorLayer(road, "Дороги", "ogr"),
        "dem": QgsRasterLayer(dem, "Рельеф", "gdal"),
        "top": QgsRasterLayer(_tif(j(P, "орто.tif"), srs=False), "Орто", "gdal"),
        "other": QgsRasterLayer(_tif(j(P, "Снимки", "2024", "s.tif")), "Снимок", "gdal"),
        "far": QgsRasterLayer(_tif(j(home, "Desktop", "ЛЭП", "узел7", "far.tif")), "Внешний", "gdal"),
        "mix": QgsRasterLayer(mix, "Смесь растр", "gdal"),
        "both_r": QgsRasterLayer("GPKG:{}:dem".format(both), "Оба растр", "gdal"),
        "both_v": QgsVectorLayer(both + "|layername=pts", "Оба вектор", "ogr"),
        "t1": QgsRasterLayer("GPKG:{}:t1".format(tiles), "Тайл 1", "gdal"),
        "t2": QgsRasterLayer("GPKG:{}:t2".format(tiles), "Тайл 2", "gdal"),
        "cloud": QgsPointCloudLayer(cloud, "Облако", "pdal"),
        "mesh": QgsMeshLayer(mesh, "Сетка", "mdal"),
        "mesh2": QgsMeshLayer(mesh2, "Сетка с расчётом", "mdal"),
        "tmp": QgsRasterLayer(tmp, "Уклон", "gdal"),
    }
    bad = [k for k, l in L.items() if not l.isValid()]
    assert not bad, bad
    assert L["mesh2"].addDatasets(depth)
    L["cloud"].setCrs(UTM37)  # в файлах облака и орто СК нет — назначена в проекте
    L["top"].setCrs(UTM37)
    p.addMapLayers(list(L.values()))
    # QGIS строит индекс облака «cloud.copc.laz» рядом с файлом в фоне — ждём его
    index = j(P, "data", "Облака", "cloud.copc.laz")
    for _ in range(100):
        if os.path.isfile(index) and not QgsApplication.taskManager().countActiveTasks():
            break
        QgsApplication.processEvents()
        import time
        time.sleep(0.1)
    assert os.path.isfile(index)
    assert p.write(j(P, "Проект.qgz"))
    return p, P, home, L


def test_split_by_type():
    """Растры, облака точек и сетки — в data_all/raster, pointcloud, mesh по тем
    же правилам 1–4, со своими сопутствующими файлами. GeoPackage, где есть и
    растр, и вектор, — в общем дереве, одной копией. Облака точек и сетки есть в
    списке сборки только со структурой папок; сетку с подключёнными наборами
    данных собрать нельзя — она в списке с причиной."""
    from temp_layers_to_folder import packager

    j = os.path.join
    root = os.path.join(OUT, "split")
    p, P, home, L = split_project(root)

    items, skipped = packager.find_layers(p, native_format=False)
    reasons = {l.name(): r for l, r in skipped}
    assert reasons["Облако"] == "облако точек — только с родными форматами", reasons
    assert "Сетка" in reasons and "Сетка с расчётом" in reasons, reasons
    items, skipped = packager.find_layers(p, native_format=True)
    kinds = {l.name(): k for l, k, _t in items}
    assert kinds["Облако"] == "pointcloud" and kinds["Сетка"] == "mesh", kinds
    assert saver.describe(L["cloud"], "pointcloud", False) == "облако точек .las"
    assert saver.describe(L["mesh"], "mesh", False) == "сетка .2dm"
    reasons = {l.name(): r for l, r in skipped}
    assert list(reasons) == ["Сетка с расчётом"], reasons
    assert "дополнительные наборы данных" in reasons["Сетка с расчётом"]

    with _Home(home):
        res = packager.consolidate_project(p, items + [(L["mesh2"], "mesh", False)], "native",
                                           include_fonts=False, keep_structure=True)
    by_name = {r["name"]: r for r in res["results"]}
    assert not by_name["Сетка с расчётом"]["ok"] and "наборы данных" in by_name["Сетка с расчётом"]["message"]
    assert L["mesh2"].source() == j(P, "data", "Сетка", "m2.2dm")  # не тронута
    del by_name["Сетка с расчётом"]
    assert all(r["ok"] for r in by_name.values()), [r for r in by_name.values() if not r["ok"]]
    D = j(P, "data_all")

    def listing(*sub):
        return sorted(n for n in os.listdir(j(D, *sub)) if not n.endswith(("-wal", "-shm")))

    assert set(os.listdir(D)) == {"raster", "pointcloud", "mesh", "База", "Вектор", "Оба"}, os.listdir(D)
    assert L["road"].source() == j(D, "Вектор", "дороги.geojson")  # вектор — в общем дереве
    # 1–4 для растров — внутри raster/, со спутниками, без чужих файлов
    assert listing("raster", "Рельеф") == ["dem.prj", "dem.qml", "dem.rrd", "dem.tfw", "dem.tif",
                                           "dem.tif.aux.xml", "dem.tif.ovr", "dem.tif.vat.dbf"], \
        listing("raster", "Рельеф")
    assert L["dem"].source() == j(D, "raster", "Рельеф", "dem.tif")
    assert L["top"].source() == j(D, "raster", "орто.tif")
    assert L["other"].source() == j(D, "raster", "Снимки", "2024", "s.tif")
    assert L["far"].source() == j(D, "raster", "external_links", "Desktop", "ЛЭП", "узел7", "far.tif")
    # в GeoPackage есть вектор — файл в общем дереве, хотя проект берёт из него только растр
    assert L["mix"].source() == j(D, "База", "смесь.gpkg"), L["mix"].source()
    # растр и вектор из одного GeoPackage — одна копия в общем дереве
    assert listing("Оба") == ["оба.gpkg"]
    assert L["both_r"].source() == "GPKG:{}:dem".format(j(D, "Оба", "оба.gpkg")), L["both_r"].source()
    assert L["both_v"].source() == j(D, "Оба", "оба.gpkg") + "|layername=pts"
    # GeoPackage только с растрами — в raster/, одной копией
    assert listing("raster", "Тайлы") == ["тайлы.gpkg"]
    assert L["t2"].source() == "GPKG:{}:t2".format(j(D, "raster", "Тайлы", "тайлы.gpkg"))
    # облако точек — с индексом QGIS; сетка
    assert listing("pointcloud", "Облака") == ["cloud.copc.laz", "cloud.las"], listing("pointcloud", "Облака")
    assert L["cloud"].source() == j(D, "pointcloud", "Облака", "cloud.las")
    assert L["mesh"].source() == j(D, "mesh", "Сетка", "m.2dm")
    # временный растр — в корень raster/
    assert L["tmp"].source() == j(D, "raster", "Уклон.tif"), L["tmp"].source()
    # СК, назначенная в проекте, у копии та же
    assert L["cloud"].crs() == UTM37 and L["top"].crs() == UTM37, (L["cloud"].crs(), L["top"].crs())

    # проект открывается с диска без исходных файлов
    shutil.move(j(P, "data"), j(root, "data_убрана"))
    shutil.move(home, home + "_убрана")
    p2 = QgsProject()
    assert p2.read(j(P, "Проект.qgz"))
    for key, layer in L.items():
        if key != "mesh2":
            assert p2.mapLayer(layer.id()).isValid(), (key, p2.mapLayer(layer.id()).source())
    assert p2.mapLayer(L["cloud"].id()).crs() == UTM37 and p2.mapLayer(L["top"].id()).crs() == UTM37
    p2.clear()
    shutil.move(j(root, "data_убрана"), j(P, "data"))
    shutil.move(home + "_убрана", home)
    readme = zipfile_readme(res["zip"])
    assert "Облако — data_all/pointcloud/Облака/cloud.las" in readme, readme
    assert "Сетка — data_all/mesh/Сетка/m.2dm" in readme, readme


def zipfile_readme(path):
    import zipfile

    z = zipfile.ZipFile(path)
    return z.read(next(n for n in z.namelist() if n.endswith("/Состав.txt"))).decode("utf-8-sig")


def test_native_format_package():
    """«Оставить родные форматы файлов» и флажок подпапок независимы: без флажка
    файлы копируются как есть прямо в data_all, с другим форматом слои
    переводятся в него, но ложатся по исходным подпапкам."""
    from temp_layers_to_folder import packager

    j = os.path.join
    # родные форматы без подпапок — всё как есть, в одну папку
    root = os.path.join(OUT, "native_flat")
    p, P, home, L = copy_project(root)
    assert p.write(j(P, "Проект.qgz"))
    items, _ = packager.find_layers(p, native_format=True)
    with _Home(home):
        res = packager.consolidate_project(p, items, "native", include_fonts=False, make_archive=False,
                                           keep_structure=False)
    assert all(r["ok"] for r in res["results"]), [r for r in res["results"] if not r["ok"]]
    D = j(P, "data_all")
    names = sorted(n for n in os.listdir(D) if not n.endswith(("-wal", "-shm")))
    assert "лес.shp" in names and "лес.dbf" in names and "граница.geojson" in names, names
    assert "участки.gpkg" in names and "dem.tif" in names and "dem.tfw" in names, names
    assert [n for n in names if os.path.isdir(j(D, n))] == ["images", "папка_shp"], names
    assert L["les"].source() == j(D, "лес.shp")
    assert L["ga"].source() == j(D, "участки.gpkg") + "|layername=a"
    assert L["mem"].source().startswith(j(D, "Черновик.gpkg"))  # в памяти — в GeoPackage
    assert L["dup1"].source() == j(D, "реки.geojson") and L["dup2"].source() == j(D, "реки_2.geojson")

    # другой формат со структурой папок — слои переводятся в формат, но по подпапкам
    root = os.path.join(OUT, "convert_struct")
    p, P, home, L = copy_project(root)
    assert p.write(j(P, "Проект.qgz"))
    items, _ = packager.find_layers(p, native_format=False)
    with _Home(home):
        res = packager.consolidate_project(p, items, "gpkg", include_fonts=False, make_archive=False,
                                           keep_structure=True)
    assert all(r["ok"] for r in res["results"]), [r for r in res["results"] if not r["ok"]]
    D = j(P, "data_all")
    assert L["les"].source().startswith(j(D, "ЛЕС", "лес.gpkg")), L["les"].source()
    assert L["ga"].source().startswith(j(D, "ЛЕС", "2024", "Участки А.gpkg")), L["ga"].source()
    # имя файла — как у исходного: структура повторяет исходную раскладку
    assert L["csv"].source().startswith(j(D, "Рабочие файлы", "точки.gpkg")), L["csv"].source()
    assert L["trees"].source().startswith(
        j(D, "external_links", "Desktop", "ЛЭП", "узел7", "trees.gpkg")), L["trees"].source()
    assert L["mem"].source().startswith(j(D, "Черновик.gpkg")), L["mem"].source()  # без файла — в data_all
    # растр без смены СК копируется как есть, но в raster/ по структуре
    assert L["dem"].source() == j(D, "raster", "Рельеф", "dem.tif"), L["dem"].source()


def test_native_format_temp_mode():
    """В режиме временных слоёв «родные форматы» копируют файлы результатов
    Processing как есть, под названием слоя; слои в памяти — в GeoPackage."""
    j = os.path.join
    p = QgsProject.instance()
    p.clear()
    tmp = j(QgsProcessingUtils.tempFolder(), "tests_native")
    shutil.rmtree(tmp, ignore_errors=True)
    shp = _vector_file(j(tmp, "OUTPUT.shp"), "ESRI Shapefile")
    mem = QgsVectorLayer("Point?crs=EPSG:4326", "Черновик", "memory")
    out_shp = QgsVectorLayer(shp, "Буферизованный", "ogr")
    ras = QgsRasterLayer(_tif(j(tmp, "OUTPUT.tif")), "Уклон", "gdal")
    p.addMapLayers([mem, out_shp, ras])
    items = saver.find_temporary_layers(p)
    assert sorted(l.name() for l, *_ in items) == ["Буферизованный", "Уклон", "Черновик"], items

    out = os.path.join(OUT, "native_temp")
    res = saver.save_layers(p, items, out, "native")
    assert all(r["ok"] for r in res), res
    # macOS: QGIS пишет .qml в другой форме записи Юникода — сравниваем нормализованные имена
    names = sorted(unicodedata.normalize("NFC", n) for n in os.listdir(out))
    assert "Буферизованный.shp" in names and "Буферизованный.dbf" in names, names
    assert "Черновик.gpkg" in names and "Уклон.tif" in names, names
    assert out_shp.source() == j(out, "Буферизованный.shp") and out_shp.featureCount() == 2
    assert mem.source().startswith(j(out, "Черновик.gpkg"))
    assert "Буферизованный.qml" in names  # стиль рядом с копией

    # СК, назначенная слою (в файле её нет), сохраняется; файл с двумя слоями копируется один раз
    os.remove(j(tmp, "OUTPUT.prj"))
    no_crs = QgsVectorLayer(shp, "Без СК", "ogr")
    no_crs.setCrs(UTM37)
    gpkg = _vector_file(j(tmp, "ДВА.gpkg"), "GPKG", "a")
    _vector_file(gpkg, "GPKG", "b", n=3, append=True)
    two = [QgsVectorLayer(gpkg + "|layername=" + n, "Таблица " + n, "ogr") for n in ("a", "b")]
    p.addMapLayers([no_crs] + two)
    out2 = os.path.join(OUT, "native_temp2")
    res = saver.save_layers(p, [(l, "vector", True) for l in [no_crs] + two], out2, "native")
    assert all(r["ok"] for r in res), res
    assert no_crs.crs() == UTM37, no_crs.crs()
    copy = j(out2, "Таблица a.gpkg")
    assert two[0].source() == copy + "|layername=a" and two[1].source() == copy + "|layername=b", two[1].source()
    assert two[1].featureCount() == 3
    assert [n for n in os.listdir(out2) if n.endswith(".gpkg")] == ["Таблица a.gpkg"], os.listdir(out2)

    # источник-папка копируется целиком, файла стиля рядом с папкой не появляется
    folder = j(tmp, "папка_shp")
    _vector_file(j(folder, "x.shp"), "ESRI Shapefile")
    _vector_file(j(folder, "y.shp"), "ESRI Shapefile")
    from_dir = QgsVectorLayer(folder + "|layername=y", "Из папки", "ogr")
    p.addMapLayer(from_dir)
    out3 = os.path.join(OUT, "native_temp3")
    res = saver.save_layers(p, [(from_dir, "vector", True)], out3, "native")
    assert res[0]["ok"], res
    assert from_dir.source() == j(out3, "Из папки") + "|layername=y", from_dir.source()
    assert sorted(os.listdir(out3)) == ["Из папки"], os.listdir(out3)
    assert sorted(os.listdir(j(out3, "Из папки"))) == sorted(os.listdir(folder))


def test_native_format_keeps_edits():
    """С родными форматами слой с незавершённой правкой не копируется, а
    записывается: иначе правка не попала бы в копию."""
    j = os.path.join
    p = QgsProject.instance()
    p.clear()
    path = _vector_file(j(OUT, "edits", "исходный.geojson"), "GeoJSON")
    lay = QgsVectorLayer(path, "Правки", "ogr")
    p.addMapLayer(lay)
    lay.startEditing()
    f = QgsFeature(lay.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.5, 48.9)))
    lay.addFeature(f)
    out = j(OUT, "edits_out")
    res = saver.save_layers(p, [(lay, "vector", False)], out, "native", replace=False)
    assert res[0]["ok"] and res[0]["path"].endswith(".gpkg"), res  # записан, а не скопирован
    assert "несохранённые правки попали в копию" in res[0]["message"], res
    copy = QgsVectorLayer(res[0]["uri"], "копия", "ogr")
    assert copy.isValid() and copy.featureCount() == 3, copy.featureCount()
    lay.rollBack()


def test_pointcloud_sources():
    """Адреса облаков точек: EPT копируется папкой (ept.json внутри), у COPC
    меняется путь при новом имени, виртуальное облако (.vpc) не копируется.
    Папка типа: у одного типа — его папка, у разных типов в одном файле — общее
    дерево."""
    from qgis.core import QgsPointCloudLayer

    from temp_layers_to_folder import copier

    j = os.path.join
    d = j(OUT, "pc_sources")
    ept = _write(j(d, "ept_облако", "ept.json"), "{}")
    _write(j(d, "ept_облако", "ept-data", "0-0-0-0.laz"))
    src = copier.layer_source(QgsPointCloudLayer(ept, "EPT", "ept"))
    assert src.path == j(d, "ept_облако"), src.path
    assert src.uri(j(d, "copy", "ept_облако_2")) == j(d, "copy", "ept_облако_2", "ept.json")
    copc = _write(j(d, "a.copc.laz"))
    src = copier.layer_source(QgsPointCloudLayer(copc, "COPC", "copc"))
    assert src.path == copc and src.uri(j(d, "copy", "a_2.copc.laz")) == j(d, "copy", "a_2.copc.laz")
    vpc = QgsPointCloudLayer(_write(j(d, "все.vpc"), "{}"), "VPC", "vpc")
    assert copier.layer_source(vpc) is None and "виртуальное облако" in copier.copy_obstacle(vpc)
    assert copier.companion_names(j(d, "a.copc.laz"), pointcloud=True) == ["a.copc.laz"]

    assert copier.unit_type({"raster"}, j(d, "x.tif")) == "raster"
    assert copier.unit_type({"mesh"}, j(d, "x.nc")) == "mesh"
    assert copier.unit_type({"raster", "mesh"}, j(d, "x.nc")) == ""  # NetCDF и растром, и сеткой
    assert copier.unit_type({"vector"}, j(d, "x.shp")) == ""

    class QgsPointCloudLayer:  # облако из интернета: без сети настоящий слой не открыть
        def isValid(self): return True
        def providerType(self): return "copc"
        def source(self): return "https://example.com/лидар.copc.laz"

    kind, _t, reason = saver.classify(QgsPointCloudLayer(), copy_as_is=True)
    assert kind is None and reason.startswith("онлайн"), reason  # в архиве останется ссылкой


def test_split_by_type_crs_and_off():
    """Смена СК: растр перепроецируется в GeoTIFF на своё место в raster/, облако
    точек копируется как есть с пометкой. Без разделения (SPLIT_BY_TYPE=False)
    всё ложится в общее дерево."""
    from temp_layers_to_folder import packager

    j = os.path.join
    root = os.path.join(OUT, "split_crs")
    p, P, home, L = split_project(root)
    items = [(L["dem"], "raster", False), (L["cloud"], "pointcloud", False)]
    with _Home(home):
        res = packager.consolidate_project(p, items + [(L["t1"], "raster", False), (L["t2"], "raster", False)],
                                           "native", crs=MSK, include_fonts=False, make_archive=False,
                                           keep_structure=True)
    assert all(r["ok"] for r in res["results"]), res["results"]
    D = j(P, "data_all")
    # растры из одного GeoPackage — под своими именами
    assert sorted(n for n in os.listdir(j(D, "raster", "Тайлы")) if n.endswith(".tif")) == \
        ["Тайл 1.tif", "Тайл 2.tif"], os.listdir(j(D, "raster", "Тайлы"))
    assert L["dem"].source() == j(D, "raster", "Рельеф", "dem.tif") and L["dem"].crs() == MSK
    assert not os.path.exists(j(D, "raster", "Рельеф", "dem.tfw"))  # переписан GDAL, не скопирован
    assert L["cloud"].source() == j(D, "pointcloud", "Облака", "cloud.las")
    assert "система координат не изменена" in res["results"][1]["message"], res["results"]
    # повторно: облако уже в data_all — не копируется второй раз, хотя его СК другая
    res = packager.consolidate_project(p, items, "native", crs=MSK, include_fonts=False,
                                       make_archive=False, keep_structure=True)
    assert res["results"] == [] and len(res["already"]) == 2, res
    assert sorted(os.listdir(j(D, "pointcloud", "Облака"))) == ["cloud.copc.laz", "cloud.las"]
    # без структуры папок сетка не собирается
    res = packager.consolidate_project(p, [(L["mesh"], "mesh", False)], "gpkg", include_fonts=False,
                                       make_archive=False)
    assert not res["results"][0]["ok"] and "родными форматами" in res["results"][0]["message"], res

    root = os.path.join(OUT, "split_off")
    p, P, home, L = split_project(root)
    items, _ = packager.find_layers(p, native_format=True)
    with _Home(home):
        res = packager.consolidate_project(p, items, "native", include_fonts=False, make_archive=False,
                                           keep_structure=True, split_by_type=False)
    assert all(r["ok"] for r in res["results"]), res["results"]
    D = j(P, "data_all")
    assert not any(os.path.exists(j(D, d)) for d in ("raster", "pointcloud", "mesh")), os.listdir(D)
    assert L["dem"].source() == j(D, "Рельеф", "dem.tif")
    assert L["far"].source() == j(D, "external_links", "Desktop", "ЛЭП", "узел7", "far.tif")
    assert L["t1"].source() == "GPKG:{}:t1".format(j(D, "Тайлы", "тайлы.gpkg"))
    assert L["cloud"].source() == j(D, "Облака", "cloud.las")
    assert L["mesh"].source() == j(D, "Сетка", "m.2dm")
    assert L["tmp"].source() == j(D, "Уклон.tif"), L["tmp"].source()


def test_split_dialog_list():  # noqa: C901
    """Облака точек и сетки становятся доступны в списке, когда в «Формате»
    выбирают «Оставить родные форматы файлов»; снятые галочки у других слоёв
    не сбрасываются. Флажки сборки — внизу, вместе с остальными."""
    from qgis.PyQt.QtCore import QPoint, Qt
    from qgis.PyQt.QtWidgets import QMainWindow

    from temp_layers_to_folder import dialog as dlg_mod

    class Iface:
        def __init__(self):
            self.w = QMainWindow()

        def mainWindow(self): return self.w

    p, P, home, L = split_project(os.path.join(OUT, "split_dlg"))
    iface = Iface()
    d = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    d.mode_package.setChecked(True)
    d.format.setCurrentIndex(d.format.findData("gpkg"))

    def row(name):
        return next(d.layers.item(i) for i in range(d.layers.count())
                    if d.layers.item(i).text().startswith(name + "   "))

    assert not row("Облако").flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert "только с родными форматами" in row("Облако").text()
    row("Дороги").setCheckState(dlg_mod.UNCHECKED)
    d.format.setCurrentIndex(d.format.findData("native"))
    assert row("Облако").checkState() == dlg_mod.CHECKED and "облако точек .las" in row("Облако").text()
    assert row("Сетка").checkState() == dlg_mod.CHECKED
    assert not row("Сетка с расчётом").flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert row("Дороги").checkState() == dlg_mod.UNCHECKED
    assert {l.name() for l, *_ in d._selected()} >= {"Облако", "Сетка"}
    assert "data_all/pointcloud" in d.pkg_structure.toolTip()
    assert "как есть" in d.format.toolTip()
    # флажки сборки — внизу, ниже списка слоёв, вместе с «Сохранить стили слоёв»
    d.layout().activate()

    def top(w):
        return w.mapTo(d, QPoint(0, 0)).y()

    assert top(d.pkg_structure) > top(d.layers) and top(d.pkg_archive) > top(d.layers)
    assert abs(top(d.pkg_structure) - top(d.styles)) < 100, (top(d.pkg_structure), top(d.styles))
    assert d.pkg_structure.isVisibleTo(d) and d.pkg_archive.isVisibleTo(d)
    # в режиме временных слоёв флажков сборки нет
    d.format.setCurrentIndex(d.format.findData("gpkg"))
    d.mode_temp.setChecked(True)
    assert not d.pkg_structure.isVisibleTo(d) and not d.pkg_archive.isVisibleTo(d)
    d._save_settings()
    d.close()


def test_keep_structure_dialog():
    """Флажок «Сохранять структуру папок» — только у сборки. Проект, уже собранный
    без подпапок, раскладке не поддаётся — окно предупреждает об этом до сборки.
    Исходная версия проекта раскладывается; выбор формата и флажка запоминается."""
    from qgis.PyQt.QtWidgets import QMainWindow, QMessageBox

    from temp_layers_to_folder import dialog as dlg_mod

    class Bar:
        def pushMessage(self, *a, **k): pass

    class SaveAction:
        def trigger(self):
            QgsProject.instance().write()

    class Iface:
        def __init__(self):
            self.w, self.bar = QMainWindow(), Bar()

        def mainWindow(self): return self.w
        def messageBar(self): return self.bar
        def actionSaveProject(self): return SaveAction()

    asked = []

    def yes(*a, **k):
        asked.append(a[2] if len(a) > 2 else "")
        return QMessageBox.StandardButton.Yes

    QMessageBox.question = staticmethod(yes)
    j = os.path.join
    root = os.path.join(OUT, "struct_dlg")
    p, proj_dir, L = structure_project(root)
    proj = j(proj_dir, "Окно.qgz")
    assert p.write(proj)
    data = j(proj_dir, "data_all")
    iface = Iface()
    d = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    d.mode_temp.setChecked(True)
    assert not d.pkg_structure.isVisibleTo(d)
    d.mode_package.setChecked(True)
    assert d.pkg_structure.isVisibleTo(d) and not d.pkg_structure.isChecked()
    d.pkg_archive.setChecked(False)
    d.pkg_fonts.setChecked(False)
    d.crs.setCrs(QgsCoordinateReferenceSystem())

    # сборка без подпапок, как в прошлых версиях, затем повторная — с флажком
    d.run()
    assert os.path.isfile(j(data, "Опоры.gpkg")), d.log.toPlainText()
    assert "прямо в data_all, без подпапок" in asked[-1], asked[-1]
    d.pkg_structure.setChecked(True)
    d.format.setCurrentIndex(d.format.findData("native"))
    assert "по тем же подпапкам" in d.package_hint.text(), d.package_hint.text()
    d.run()
    assert "без подпапок: 6" in asked[-1], asked[-1]
    assert not os.path.exists(j(data, "Данные")), os.listdir(data)
    d.close()

    # исходная версия проекта раскладывается; флажок запомнен
    p, proj_dir, L = structure_project(root)
    assert p.write(proj)
    d2 = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    assert d2.mode_package.isChecked() and d2.pkg_structure.isChecked()
    assert d2.format.currentData() == "native"
    with _Home(root):
        d2.run()
    assert "Подпапки — как у исходных" in asked[-1] and "без подпапок" not in asked[-1], asked[-1]
    log = d2.log.toPlainText().replace(os.sep, "/")
    for rel in ("Данные/Вектор/Опоры.geojson", "raster/Данные/Растры/dem.tif", "Карта.geojson",
                "external_links/Архив/Топо/a.geojson", "external_links/Архив/Почвы/b.geojson",
                "Черновик.gpkg"):
        assert os.path.isfile(j(data, rel)) and "data_all/" + rel in log, (rel, log)
    assert "Свои картинки: 2 шт. в data_all/symbols/" in log, log
    d2.pkg_structure.setChecked(False)
    d2.format.setCurrentIndex(d2.format.findData("gpkg"))
    d2.pkg_archive.setChecked(True)
    d2.mode_temp.setChecked(True)
    d2._save_settings()
    d2.close()


def test_gpkg_layer_name_with_dash():
    """«- поворотные точки»: GDAL не создаёт таблицу GeoPackage, имя которой
    начинается с «-». Файл называется как слой, таблица — без знака в начале.
    Неудачная запись не оставляет пустой файл."""
    p = QgsProject.instance()
    p.clear()
    lay = QgsVectorLayer("Point?crs=EPSG:4326&field=n:integer", "- поворотные точки", "memory")
    f = QgsFeature(lay.fields())
    f.setAttributes([1])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.1, 48.5)))
    lay.dataProvider().addFeature(f)
    p.addMapLayer(lay)
    items = saver.find_temporary_layers(p)

    out = os.path.join(OUT, "dash")
    res = saver.save_layers(p, items, out, "gpkg", replace=False)
    assert res[0]["ok"], res
    path = os.path.join(out, "- поворотные точки.gpkg")
    assert os.path.isfile(path) and "поворотные точки" in saver._existing_gpkg_layers(path)
    res = saver.save_layers(p, items, os.path.join(OUT, "dash_single"), "gpkg_single", gpkg_name="все",
                            replace=False)
    assert res[0]["ok"], res

    out = os.path.join(OUT, "dash_fail")
    real = saver.gpkg_layer_name
    saver.gpkg_layer_name = lambda name: name  # как до исправления
    try:
        res = saver.save_layers(p, items, out, "gpkg", replace=False)
    finally:
        saver.gpkg_layer_name = real
    assert not res[0]["ok"] and "special characters" in res[0]["message"], res
    assert os.listdir(out) == [], os.listdir(out)


def test_deleted_source_file():
    """Файл слоя удалили при открытом проекте: QGIS ещё рисует слой через открытый
    файл, но сохранять и собирать его нельзя — растры и значки при этом теряются.
    В списке такой слой виден с причиной и в сборку не идёт."""
    from qgis.core import QgsCoordinateTransformContext, QgsVectorFileWriter

    from temp_layers_to_folder import packager

    root = os.path.join(OUT, "deleted")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    mem = QgsVectorLayer("Point?crs=EPSG:4326&field=n:integer", "src", "memory")
    f = QgsFeature(mem.fields())
    f.setAttributes([1])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(39.1, 48.5)))
    mem.dataProvider().addFeature(f)
    gpkg = os.path.join(root, "src.gpkg")
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    assert QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), opts)[0] == 0
    tif = _tif(os.path.join(root, "src.tif"))

    p = QgsProject.instance()
    p.clear()
    vec = QgsVectorLayer(gpkg, "Удалённый", "ogr")
    ras = QgsRasterLayer(tif, "Удалённый растр", "gdal")
    alive = QgsVectorLayer(_geojson(os.path.join(root, "alive.geojson")), "Живой", "ogr")
    p.addMapLayers([vec, ras, alive])
    assert vec.featureCount() == 1
    assert p.write(os.path.join(root, "Удалённые.qgz"))
    items, _ = saver.find_layers(p, temporary_only=False)
    assert len(items) == 3

    for path in (gpkg, tif):
        os.remove(path)
    assert vec.isValid() and ras.isValid()  # QGIS не замечает удаления
    items, skipped = saver.find_layers(p, temporary_only=False)
    assert [l.name() for l, *_ in items] == ["Живой"], items
    reasons = {l.name(): reason for l, reason in skipped}
    assert "удалён" in reasons.get("Удалённый", "") and "удалён" in reasons.get("Удалённый растр", ""), reasons
    res = packager.consolidate_project(p, items, "gpkg", include_fonts=False, make_archive=False)
    assert [r["name"] for r in res["results"]] == ["Живой"], res["results"]
    assert sorted(res["excluded"]) == ["Удалённый", "Удалённый растр"], res["excluded"]


def _mldata(path, records, version=2):
    """Файл данных Memory Layer Saver: records = [(id слоя, [(поле, тип QMetaType, имя
    типа)], [([значения], wkt или None)])] — так, как его пишет сам модуль."""
    from qgis.PyQt.QtCore import QDataStream, QFile, QIODevice

    f = QFile(path)
    assert f.open(QIODevice.OpenModeFlag.WriteOnly)
    ds = QDataStream(f)
    ds.setVersion(QDataStream.Version.Qt_4_5)
    for c in b"QGis.MemoryLayerData":
        ds.writeUInt8(c)
    ds.writeUInt32(version)
    for layer_id, fields, feats in records:
        ds.writeQString(layer_id)
        if version > 1:
            ds.writeQString("")
        ds.writeInt16(len(fields))
        for name, qtype, typename in fields:
            ds.writeQString(name)
            ds.writeInt16(qtype)
            ds.writeQString(typename)
            ds.writeInt16(0)
            ds.writeInt16(0)
            ds.writeQString("")
        for values, wkt in feats:
            ds.writeBool(True)
            for v in values:
                ds.writeQVariant(v)
            wkb = bytes(QgsGeometry.fromWkt(wkt).asWkb()) if wkt else b""
            ds.writeUInt32(len(wkb))
            if wkb:
                ds.writeRawData(wkb)
        ds.writeBool(False)
    f.close()


def test_memory_layer_saver_not_loaded():
    """Временные слои модуля Memory Layer Saver: он хранит их объекты в проекте и
    загружает при открытии. Если модуль не установлен или выключен, слой пустой —
    сохранить его значило бы заменить объекты пустым файлом. Такой слой не
    сохраняется и виден с причиной; пустые по-настоящему и загруженные слои
    сохраняются как раньше."""
    import zipfile

    from qgis.PyQt.QtCore import QDate, Qt
    from qgis.PyQt.QtWidgets import QMainWindow

    from temp_layers_to_folder import dialog as dlg_mod
    from temp_layers_to_folder import packager

    root = os.path.join(OUT, "mls")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    p = QgsProject.instance()
    p.clear()
    park = QgsVectorLayer("Polygon?crs=EPSG:32637&field=name:string", "Нац парк", "memory")
    draft = QgsVectorLayer("Point?crs=EPSG:32637", "Черновик", "memory")
    new = QgsVectorLayer("LineString?crs=EPSG:32637", "Новый", "memory")
    loaded = QgsVectorLayer("Point?crs=EPSG:32637&field=n:integer", "Загружен", "memory")
    p.addMapLayers([park, draft, new, loaded])
    fields = [("name", int(QMetaType.Type.QString), "string"), ("n", int(QMetaType.Type.Int), "integer"),
              ("d", int(QMetaType.Type.QDate), "date"), ("x", int(QMetaType.Type.Double), "double")]
    poly = "POLYGON((0 0,0 10,10 10,10 0,0 0))"
    records = [
        ("чужой_слой", fields, [(["длинная строка " * 20, 7, QDate(2026, 9, 21), 1.5], poly),
                                ([None, None, None, None], None)]),
        (park.id(), [fields[0]], [(["п%d" % i], poly) for i in range(3)]),
        (draft.id(), [], []),  # пустой по-настоящему
        (loaded.id(), [fields[1]], [([1], "POINT(1 1)"), ([2], "POINT(2 2)")]),
    ]
    _mldata(p.createAttachedFile("layers.mldata"), records)
    qgz = os.path.join(root, "Проект.qgz")
    assert p.write(qgz)
    p.clear()
    assert p.read(qgz)
    loaded = p.mapLayersByName("Загружен")[0]  # его объекты модуль загрузил
    for i in (1, 2):
        f = QgsFeature(loaded.fields())
        f.setAttributes([i])
        f.setGeometry(QgsGeometry.fromWkt("POINT({0} {0})".format(i)))
        loaded.dataProvider().addFeature(f)
    park = p.mapLayersByName("Нац парк")[0]
    assert park.featureCount() == 0

    for items, skipped in (saver.find_layers(p, temporary_only=True), saver.find_layers(p, temporary_only=False),
                           packager.find_layers(p), packager.find_layers(p, native_format=True)):
        assert sorted(l.name() for l, *_ in items) == ["Загружен", "Новый", "Черновик"], items
        assert [(l.name(), r) for l, r in skipped] == [("Нац парк", saver.MLS_NOT_LOADED)], skipped

    # в окне — серой строкой с объяснением во всплывающей подсказке
    class Iface:
        w = QMainWindow()

        def mainWindow(self):
            return self.w

        def messageBar(self):
            return None

        def layerTreeView(self):
            return None

    d = dlg_mod.SaveTempLayersDialog(Iface(), None)
    for mode in (d.mode_temp, d.mode_package):
        mode.setChecked(True)
        d.refresh()
        rows = [d.layers.item(i) for i in range(d.layers.count())]
        grey = [r for r in rows if r.text().startswith("Нац парк")]
        assert len(grey) == 1 and grey[0].flags() == Qt.ItemFlag.NoItemFlags, [r.text() for r in rows]
        assert saver.MLS_NOT_LOADED in grey[0].text() and grey[0].toolTip() == saver.MLS_HINT
    d.close()
    d.deleteLater()

    res = packager.consolidate_project(p, packager.find_layers(p)[0], "gpkg", include_fonts=False,
                                       make_archive=False)
    assert sorted(r["name"] for r in res["results"] if r["ok"]) == ["Загружен", "Новый", "Черновик"], res
    assert park.providerType() == "memory" and res["excluded"] == ["Нац парк"], res["excluded"]
    with zipfile.ZipFile(qgz) as z:  # данные Memory Layer Saver остались в проекте
        assert any(n.endswith("layers.mldata") for n in z.namelist()), z.namelist()

    # старый способ: <проект>.qgs.mldata рядом с проектом; формат версии 1 — без фильтра
    p.clear()
    old = QgsVectorLayer("Point?crs=EPSG:32637", "Старый", "memory")
    p.addMapLayer(old)
    qgs = os.path.join(root, "Старый.qgs")
    assert p.write(qgs)
    _mldata(qgs + ".mldata", [(old.id(), [], [([], "POINT(1 1)")])], version=1)
    assert [(l.name(), r) for l, r in saver.find_layers(p)[1]] == [("Старый", saver.MLS_NOT_LOADED)]
    with open(qgs + ".mldata", "r+b") as f:  # чужой заголовок — это не файл Memory Layer Saver
        f.write(b"X")
    assert [l.name() for l, *_ in saver.find_layers(p)[0]] == ["Старый"]
    with open(qgs + ".mldata", "wb") as f:  # испорченный файл — не мешает сохранять
        f.write(b"QGis.MemoryLayerData\x00\x00\x00\x02\xff\xff")
    assert [l.name() for l, *_ in saver.find_layers(p)[0]] == ["Старый"]
    os.remove(qgs + ".mldata")
    assert [l.name() for l, *_ in saver.find_layers(p)[0]] == ["Старый"]


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
    # название модуля и окна — одно
    meta = open(os.path.join(os.path.dirname(HERE), "temp_layers_to_folder", "metadata.txt"), encoding="utf-8").read()
    assert "\nname=Сохранение временных слоёв\n" in meta
    assert d.windowTitle() == "Сохранение временных слоёв", d.windowTitle()
    # два режима: временные слои и сборка в data_all; «Создать архив» — только у сборки
    assert sorted(d._radios) == [dlg_mod.MODE_PACKAGE, dlg_mod.MODE_TEMP] and not hasattr(d, "mode_all")
    assert not hasattr(d, "structure") and d.pkg_structure.text() == "Сохранять структуру папок"
    d.mode_package.setChecked(True)
    assert d.layers_box.title() == "Слои проекта"
    assert d.pkg_archive.isVisibleTo(d) and d.pkg_structure.isVisibleTo(d)
    assert [it.text().split("   ")[0] for it in d._items()][-1] == "Постоянный"
    assert len(list(d._items())) == 6
    d.mode_temp.setChecked(True)
    assert d.replace.isChecked()
    assert not d.pkg_archive.isVisibleTo(d) and not d.pkg_structure.isVisibleTo(d)
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
