"""Окна модуля для снимков: qgis-ui-review/scripts/ui_snap.py.

Запуск:
    ~/.claude/skills/qgis-plugin/scripts/qgis_env.sh -- python \
        ~/.claude/skills/qgis-ui-review/scripts/ui_snap.py tests/ui_shots.py

Настройки уводятся во временный профиль tests/_out/ui_profile — профиль пользователя не трогается.
Состояния, в которых окно видит сотрудник: пустой проект, временные слои, сборка проекта, итог
сохранения (журнал и кнопка «Открыть папку»).
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN_ROOT)

from qgis.PyQt.QtCore import QCoreApplication, QSettings  # noqa: E402

PROFILE = os.path.join(HERE, "_out", "ui_profile")
DATA = os.path.join(HERE, "_out", "ui_data")
shutil.rmtree(PROFILE, ignore_errors=True)
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(os.path.join(DATA, "Проект ВЛ"))
# ui_snap.py уже создал QgsApplication: путь настроек задаётся после initQgis(), как в test_plugin.py
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, PROFILE)
QCoreApplication.setOrganizationName("temp-layers-ui")
QCoreApplication.setApplicationName("temp-layers-ui")

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402


class MessageBar:
    def pushMessage(self, *args, **kwargs):
        pass


class Iface:
    """Минимальный iface: окну нужны только главное окно и строка сообщений."""

    def __init__(self):
        self.window = QMainWindow()
        self.bar = MessageBar()

    def mainWindow(self):
        return self.window

    def messageBar(self):
        return self.bar


def _memory_layer(name, geom="Point"):
    layer = QgsVectorLayer("{}?crs=EPSG:3857&field=id:integer".format(geom), name, "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(4150000, 7550000)))
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    return layer


def _project(saved=True, layers=True):
    project = QgsProject.instance()
    project.clear()
    if layers:
        for name in ("Черновик опор", "Буфер 100 м", "Пересечения с лесом"):
            project.addMapLayer(_memory_layer(name))
    if saved:
        project.write(os.path.join(DATA, "Проект ВЛ", "ВЛ 110 кВ.qgz"))
    return project


def windows():
    from temp_layers_to_folder.dialog import SaveTempLayersDialog

    iface = Iface()

    def пусто():
        _project(saved=False, layers=False)
        return SaveTempLayersDialog(iface)

    def временные():
        _project()
        return SaveTempLayersDialog(iface)

    def сборка():
        _project()
        dialog = SaveTempLayersDialog(iface)
        dialog.mode_package.setChecked(True)
        return dialog

    def итог():
        _project()
        dialog = SaveTempLayersDialog(iface)
        folder = os.path.join(DATA, "Проект ВЛ", "data")
        dialog.log.setVisible(True)
        dialog.log.setPlainText(
            "✔ Черновик опор → Черновик опор.gpkg\n"
            "✔ Буфер 100 м → Буфер 100 м.gpkg\n"
            "✘ Пересечения с лесом  [файл «лес.gpkg» занят другой программой "
            "(например, Яндекс.Диском или вторым QGIS) — закройте её и повторите]\n"
            "\nПапка: " + folder)
        dialog.progress.setVisible(True)
        dialog.progress.setMaximum(3)
        dialog.progress.setValue(3)
        dialog.progress.setFormat("3 / 3  Пересечения с лесом")
        dialog._last_folder = folder
        dialog.btn_open.setVisible(True)
        return dialog

    return [("окно_пустой_проект", пусто), ("окно_временные_слои", временные),
            ("окно_сборка", сборка), ("окно_итог", итог)]
