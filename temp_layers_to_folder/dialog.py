import os

from qgis.core import Qgis, QgsCoordinateReferenceSystem, QgsIconUtils, QgsProject, QgsSettings
from qgis.gui import QgsFileWidget, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from . import saver

SETTINGS = "temp_layers_to_folder/"
KIND_LABELS = {
    "memory": "в памяти",
    "vector": "временный файл",
    "raster": "временный растр",
}

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked
ROLE_ID = Qt.ItemDataRole.UserRole


def _bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


class SaveTempLayersDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.project = QgsProject.instance()
        self._busy = False
        self._cancel = False

        self.setWindowTitle("Сохранить временные слои")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)

        # --- куда и как
        form = QFormLayout()
        self.folder = QgsFileWidget()
        self.folder.setStorageMode(saver._enum(QgsFileWidget, "StorageMode", "GetDirectory"))
        self.folder.setDialogTitle("Папка для сохранения временных слоёв")
        form.addRow("Папка:", self.folder)

        self.format = QComboBox()
        for f in saver.FORMATS:
            self.format.addItem(f["label"], f["key"])
        form.addRow("Формат:", self.format)

        self.crs = QgsProjectionSelectionWidget()
        self.crs.setOptionVisible(
            saver._enum(QgsProjectionSelectionWidget, "CrsOption", "CrsNotSet"), True)
        self.crs.setNotSetText("Не менять (как у каждого слоя)")
        self.crs.setDialogTitle("Система координат для сохранения")
        self.crs.setToolTip("Слои в другой СК будут перепроецированы при сохранении. "
                            "Растры перепроецируются в GeoTIFF (ближайший сосед).")
        form.addRow("Система координат:", self.crs)

        self.gpkg_name = QLineEdit()
        self.gpkg_name.setPlaceholderText("temporary_layers")
        self.gpkg_name_label = QLabel("Имя файла .gpkg:")
        form.addRow(self.gpkg_name_label, self.gpkg_name)
        root.addLayout(form)

        # --- список слоёв
        box = QGroupBox("Временные слои проекта")
        box_layout = QVBoxLayout(box)
        self.layers = QListWidget()
        self.layers.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        box_layout.addWidget(self.layers)
        row = QHBoxLayout()
        self.btn_all = QPushButton("Выбрать все")
        self.btn_none = QPushButton("Снять все")
        self.btn_refresh = QPushButton("Обновить список")
        self.count_label = QLabel()
        for w in (self.btn_all, self.btn_none, self.btn_refresh):
            row.addWidget(w)
        row.addStretch()
        row.addWidget(self.count_label)
        box_layout.addLayout(row)
        root.addWidget(box)

        # --- параметры
        self.replace = QCheckBox("Заменить временные слои в проекте сохранёнными")
        self.replace.setToolTip("Слои проекта переключатся на сохранённые файлы и перестанут "
                                "быть временными. Стиль, порядок и связи сохранятся.")
        self.styles = QCheckBox("Сохранить стили слоёв")
        self.styles.setToolTip("Файл .qml рядом с данными или стиль по умолчанию внутри GeoPackage — "
                               "при открытии файла стиль подхватится сам.")
        self.overwrite = QCheckBox("Перезаписывать существующие файлы")
        self.overwrite.setToolTip("Если выключено, к имени добавится _2, _3…")
        for w in (self.replace, self.styles, self.overwrite):
            root.addWidget(w)
        hint = QLabel("Растры сохраняются в GeoTIFF независимо от выбранного формата: "
                      "без смены СК — копируются как есть, со сменой — перепроецируются.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")
        root.addWidget(hint)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setVisible(False)
        self.log.setMaximumHeight(150)
        root.addWidget(self.log)

        self.buttons = QDialogButtonBox()
        self.btn_save = self.buttons.addButton("Сохранить", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_close = self.buttons.addButton("Закрыть", QDialogButtonBox.ButtonRole.RejectRole)
        root.addWidget(self.buttons)

        # --- сигналы
        self.btn_save.clicked.connect(self.run)
        self.btn_close.clicked.connect(self.reject)
        self.btn_all.clicked.connect(lambda: self._set_all(CHECKED))
        self.btn_none.clicked.connect(lambda: self._set_all(UNCHECKED))
        self.btn_refresh.clicked.connect(self.refresh)
        self.layers.itemChanged.connect(self._update_count)
        self.format.currentIndexChanged.connect(self._format_changed)

        self._load_settings()
        self._format_changed()
        self.refresh()

    # ------------------------------------------------------------ настройки
    def _load_settings(self):
        s = QgsSettings()
        folder = s.value(SETTINGS + "folder", "")
        if not folder and self.project.absolutePath():
            folder = self.project.absolutePath()
        self.folder.setFilePath(folder or "")
        idx = self.format.findData(s.value(SETTINGS + "format", "gpkg_single"))
        self.format.setCurrentIndex(max(idx, 0))
        self.gpkg_name.setText(s.value(SETTINGS + "gpkg_name", "temporary_layers"))
        self.replace.setChecked(_bool(s.value(SETTINGS + "replace"), True))
        self.styles.setChecked(_bool(s.value(SETTINGS + "styles"), True))
        self.overwrite.setChecked(_bool(s.value(SETTINGS + "overwrite"), False))
        crs = QgsCoordinateReferenceSystem()
        authid, wkt = s.value(SETTINGS + "crs_authid", ""), s.value(SETTINGS + "crs_wkt", "")
        if authid:
            crs = QgsCoordinateReferenceSystem(authid)
        if not crs.isValid() and wkt:
            crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
        self.crs.setCrs(crs)

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS + "folder", self.folder.filePath())
        s.setValue(SETTINGS + "format", self.format.currentData())
        s.setValue(SETTINGS + "gpkg_name", self.gpkg_name.text())
        s.setValue(SETTINGS + "replace", self.replace.isChecked())
        s.setValue(SETTINGS + "styles", self.styles.isChecked())
        s.setValue(SETTINGS + "overwrite", self.overwrite.isChecked())
        crs = self.crs.crs()
        s.setValue(SETTINGS + "crs_authid", crs.authid() if crs.isValid() else "")
        s.setValue(SETTINGS + "crs_wkt", crs.toWkt() if crs.isValid() else "")

    # ------------------------------------------------------------ список
    def refresh(self):
        self.layers.blockSignals(True)
        self.layers.clear()
        for layer, kind in saver.find_temporary_layers(self.project):
            item = QListWidgetItem("{}   ({})".format(layer.name(), KIND_LABELS[kind]))
            try:
                item.setIcon(QgsIconUtils.iconForLayer(layer))
            except Exception:
                pass
            item.setData(ROLE_ID, layer.id())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(CHECKED)
            self.layers.addItem(item)
        if self.layers.count() == 0:
            item = QListWidgetItem("В проекте нет временных слоёв")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.layers.addItem(item)
        self.layers.blockSignals(False)
        self._update_count()

    def _items(self):
        for i in range(self.layers.count()):
            item = self.layers.item(i)
            if item.data(ROLE_ID):
                yield item

    def _set_all(self, state):
        for item in self._items():
            item.setCheckState(state)

    def _update_count(self, *_):
        items = list(self._items())
        checked = sum(1 for it in items if it.checkState() == CHECKED)
        self.count_label.setText("выбрано {} из {}".format(checked, len(items)))
        self.btn_save.setEnabled(checked > 0 and not self._busy)

    def _format_changed(self, *_):
        single = saver.FORMATS_BY_KEY[self.format.currentData()]["single"]
        self.gpkg_name.setVisible(single)
        self.gpkg_name_label.setVisible(single)

    # ------------------------------------------------------------ сохранение
    def _selected(self):
        by_id = {l.id(): (l, k) for l, k in saver.find_temporary_layers(self.project)}
        return [by_id[it.data(ROLE_ID)] for it in self._items()
                if it.checkState() == CHECKED and it.data(ROLE_ID) in by_id]

    def _on_progress(self, i, total, name):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(i)
        self.progress.setFormat("{} / {}  {}".format(i, total, name) if name else "%v / %m")
        QApplication.processEvents()

    def run(self):
        folder = self.folder.filePath().strip()
        if not folder:
            QMessageBox.warning(self, self.windowTitle(), "Выберите папку для сохранения.")
            return
        if not os.path.isdir(folder):
            answer = QMessageBox.question(
                self, self.windowTitle(),
                "Папка не существует:\n{}\n\nСоздать её?".format(folder))
            if answer != QMessageBox.StandardButton.Yes:
                return

        items = self._selected()
        if not items:
            QMessageBox.information(self, self.windowTitle(),
                                    "Выбранные слои больше не найдены — список обновлён.")
            self.refresh()
            return

        self._save_settings()
        self._busy = True
        self._cancel = False
        self._set_controls_enabled(False)
        self.progress.setVisible(True)
        self.log.clear()
        self.log.setVisible(True)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results = saver.save_layers(
                self.project, items, folder, self.format.currentData(),
                gpkg_name=self.gpkg_name.text().strip() or "temporary_layers",
                replace=self.replace.isChecked(),
                save_styles=self.styles.isChecked(),
                overwrite=self.overwrite.isChecked(),
                crs=self.crs.crs(),
                progress=self._on_progress,
                is_cancelled=lambda: self._cancel,
            )
        except saver.Cancelled:
            results = None
            self.log.appendPlainText("Отменено.")
        except Exception as e:  # noqa: BLE001
            results = None
            self.log.appendPlainText("Ошибка: {}".format(e))
        finally:
            QApplication.restoreOverrideCursor()
            self._busy = False
            self._set_controls_enabled(True)

        if results is not None:
            self._report(results, folder)
        self.refresh()

    def _report(self, results, folder):
        ok = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        for r in results:
            mark = "✔" if r["ok"] else "✘"
            line = "{} {}".format(mark, r["name"])
            if r["path"]:
                line += " → " + r["path"].replace(folder.rstrip(os.sep) + os.sep, "")
            if r["message"]:
                line += "  [{}]".format(r["message"])
            self.log.appendPlainText(line)

        self.log.appendPlainText("\nПапка: " + folder)
        text = "Сохранено слоёв: {} из {} в папку {}".format(len(ok), len(results), folder)
        level = "Success" if not failed else "Warning"
        self.iface.messageBar().pushMessage(
            "Временные слои", text, level=saver._enum(Qgis, "MessageLevel", level), duration=8)
        if ok and self.replace.isChecked():
            self.log.appendPlainText(
                "\nСлои проекта теперь ссылаются на сохранённые файлы. "
                "Не забудьте сохранить проект.")

    def _set_controls_enabled(self, enabled):
        for w in (self.folder, self.format, self.crs, self.gpkg_name, self.layers, self.btn_all,
                  self.btn_none, self.btn_refresh, self.replace, self.styles,
                  self.overwrite, self.btn_save):
            w.setEnabled(enabled)
        self.btn_close.setText("Закрыть" if enabled else "Отмена")
        if enabled:
            self._update_count()

    def reject(self):
        if self._busy:
            self._cancel = True
            return
        super().reject()
