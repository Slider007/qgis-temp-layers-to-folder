import os

from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDockWidget, QToolBar

try:  # Qt6 / QGIS 4: QAction живёт в QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 / QGIS 3
    from qgis.PyQt.QtWidgets import QAction

# Общее подменю модулей компании в «Модулях». QGIS находит подменю по названию,
# поэтому у всех наших модулей эта строка должна совпадать буква в букву.
MENU = "&Альтан-Эко"
TOOLBAR_NAME = "Временные слои"
TOOLBAR_ID = "TempLayersToFolderToolbar"  # по нему QGIS запоминает место панели


class SaveTempLayersPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self.toolbar = None
        self.layers_toolbar = None
        self.layers_separator = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        icon = QIcon(icon_path)
        if icon.isNull():
            icon = QgsApplication.getThemeIcon("/mActionFileSave.svg")
        self.action = QAction(icon, "Сохранить временные слои…", self.iface.mainWindow())
        self.action.setToolTip("Сохранить временные (или все) слои проекта в выбранную папку")
        self.action.triggered.connect(self.run)
        self.toolbar = self.iface.addToolBar(TOOLBAR_NAME)
        self.toolbar.setObjectName(TOOLBAR_ID)
        self.toolbar.setToolTip(TOOLBAR_NAME)
        self.toolbar.addAction(self.action)
        self.iface.addPluginToMenu(MENU, self.action)
        self._add_to_layers_panel()

    def _find_layers_toolbar(self):
        """Строка значков над деревом слоёв. Отдельного API у QGIS для неё нет,
        поэтому ищем панель инструментов внутри док-виджета «Слои»."""
        dock = self.iface.mainWindow().findChild(QDockWidget, "Layers")
        containers = [dock] if dock is not None else []
        view = self.iface.layerTreeView()
        if view is not None and view.parentWidget() is not None:
            containers.append(view.parentWidget())
        for container in containers:
            bars = container.findChildren(QToolBar)
            if bars:
                return bars[0]
        return None

    def _add_to_layers_panel(self):
        self.layers_toolbar = self._find_layers_toolbar()
        if self.layers_toolbar is None:
            return  # кнопка останется на своей панели и в меню
        self.layers_separator = self.layers_toolbar.addSeparator()
        self.layers_toolbar.addAction(self.action)

    def unload(self):
        if self.layers_toolbar is not None:
            try:
                self.layers_toolbar.removeAction(self.action)
                self.layers_toolbar.removeAction(self.layers_separator)
            except RuntimeError:  # панель уже удалена при закрытии QGIS
                pass
            self.layers_toolbar = self.layers_separator = None
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None
        if self.action:
            self.iface.removePluginMenu(MENU, self.action)
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
