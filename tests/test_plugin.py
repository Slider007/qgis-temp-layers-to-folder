"""Проверки плагина без интерфейса QGIS. Запуск: tests/run_tests.sh

Скрипт не трогает профиль QGIS: настройки и пользовательская СК
пишутся во временный профиль tests/_profile, результаты — в tests/_out.
"""

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
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingUtils,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

app = QgsApplication([], True, PROFILE)
if os.environ.get("QGIS_PREFIX_PATH"):
    app.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
app.initQgis()

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
    return [l for l, _ in items if l.name() == name]


def save(fmt, sub, **kw):
    p = make_project()
    items = saver.find_temporary_layers(p)
    folder = os.path.join(OUT, sub)
    res = saver.save_layers(p, items, folder, fmt, gpkg_name="слои", **kw)
    return p, items, folder, res


# ------------------------------------------------------------------ проверки

def test_find():
    p = make_project()
    kinds = sorted((l.name(), k) for l, k in saver.find_temporary_layers(p))
    assert kinds == sorted([("Точки: скважины/2", "memory"), ("Участки", "memory"),
                            ("Участки", "memory"), ("Таблица", "memory"),
                            ("Уклон", "raster")]), kinds


def check_saved(p, items, res, provider_ok=("ogr", "gdal")):
    bad = [r for r in res if not r["ok"]]
    assert not bad, bad
    for l, _ in items:
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
    for l, _ in items:
        if l.isSpatial():
            assert l.crs() == UTM37, (l.name(), l.crs().authid())
    pt = next(by_name(items, "Точки: скважины/2")[0].getFeatures()).geometry().asPoint()
    assert 400000 < pt.x() < 600000 and 5300000 < pt.y() < 5500000, pt
    info = gdal.Info(os.path.join(folder, "Уклон.tif"), format="json")
    assert info["bands"][0].get("noDataValue") == -9999


def test_crs_user_msk():
    p, items, folder, res = save("shp", "crs_msk", crs=MSK)
    check_saved(p, items, res)
    for l, _ in items:
        if l.isSpatial():
            assert l.crs() == MSK and l.crs().description() == "МСК (тест)", l.crs().description()


def test_crs_not_set():
    p, items, folder, res = save("gpkg", "crs_none", crs=QgsCoordinateReferenceSystem())
    check_saved(p, items, res)
    assert by_name(items, "Точки: скважины/2")[0].crs().authid() == "EPSG:4326"
    assert by_name(items, "Уклон")[0].width() == 20  # растр скопирован, не перепроецирован


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

        def addToolBarIcon(self, a): pass
        def addPluginToMenu(self, m, a): pass
        def removePluginMenu(self, m, a): pass
        def removeToolBarIcon(self, a): pass

    make_project()
    iface = Iface()
    plugin = temp_layers_to_folder.classFactory(iface)
    plugin.initGui()
    d = dlg_mod.SaveTempLayersDialog(iface, iface.mainWindow())
    d.folder.setFilePath(os.path.join(OUT, "dialog"))
    d.crs.setCrs(UTM37)
    items = list(d._items())
    assert len(items) == 5
    items[0].setCheckState(dlg_mod.UNCHECKED)
    if os.environ.get("SCREENSHOT"):  # снимок окна для README
        d.folder.setFilePath("/Users/me/Documents/Проект/data")
        d.resize(620, 560)
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
    app.exitQgis()
    sys.exit(1 if failed else 0)
