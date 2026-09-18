import os

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

MENU = "&Сохранение временных слоёв"


class SaveTempLayersPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        icon = QIcon(icon_path)
        if icon.isNull():
            icon = QgsApplication.getThemeIcon("/mActionFileSave.svg")
        self.action = QAction(icon, "Сохранить временные слои…", self.iface.mainWindow())
        self.action.setToolTip("Сохранить все временные слои проекта в выбранную папку")
        self.action.triggered.connect(self.run)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(MENU, self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu(MENU, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        if self.dialog:
            self.dialog.deleteLater()
            self.dialog = None

    def run(self):
        from .dialog import SaveTempLayersDialog

        if self.dialog is None:
            self.dialog = SaveTempLayersDialog(self.iface, self.iface.mainWindow())
        else:
            self.dialog.refresh()
        self.dialog.exec()
