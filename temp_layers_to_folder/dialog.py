import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsIconUtils, QgsProject, QgsSettings,
                       QgsVectorLayer)
from qgis.gui import QgsFileWidget, QgsProjectionSelectionWidget
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
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
    QRadioButton,
    QVBoxLayout,
)

from . import packager, saver

SETTINGS = "temp_layers_to_folder/"
MODE_TEMP, MODE_ALL, MODE_PACKAGE = "temp", "all", "package"
BOX_TITLES = {MODE_TEMP: "Временные слои проекта", MODE_ALL: "Слои проекта",
              MODE_PACKAGE: "Слои для передачи"}
REPLACE_TEXT = {
    MODE_TEMP: ("Заменить временные слои в проекте сохранёнными",
                "Слои проекта переключатся на сохранённые файлы и перестанут быть временными. "
                "Стиль, порядок и связи сохранятся."),
    MODE_ALL: ("Переключить слои проекта на сохранённые копии",
               "Проект будет смотреть на новые файлы вместо исходных данных. Исходные файлы "
               "и базы не меняются. Выключено — копии просто лягут в папку."),
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

        self.setWindowTitle("Сохранение слоёв в папку")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)

        # --- какие слои, куда и как
        form = QFormLayout()
        self.mode_temp = QRadioButton("Сохранить только временные слои")
        self.mode_all = QRadioButton("Сохранить все слои проекта")
        self.mode_all.setToolTip("Например, чтобы сохранить весь проект в новой системе координат. "
                                 "Онлайн-слои (WMS, XYZ) и облака точек сохранить нельзя.")
        self.mode_package = QRadioButton("Собрать проект в data_all и архив")
        self.mode_package.setToolTip(
            "1) Слои, свои картинки и свободные шрифты копируются в папку data_all рядом с файлом "
            "проекта, проект переключается на них и сохраняется — исходные файлы можно удалить.\n"
            "2) Рядом с проектом появляется архив «<проект>_архив_<дата>.zip»: проект, data_all "
            "и «Состав.txt».")
        self._radios = {MODE_TEMP: self.mode_temp, MODE_ALL: self.mode_all,
                        MODE_PACKAGE: self.mode_package}
        mode_col = QVBoxLayout()
        for radio in self._radios.values():
            mode_col.addWidget(radio)
        form.addRow("Режим:", mode_col)

        self.folder = QgsFileWidget()
        self.folder.setStorageMode(saver._enum(QgsFileWidget, "StorageMode", "GetDirectory"))
        self.folder.setDialogTitle("Папка для сохранения")
        self.folder_label = QLabel("Папка:")
        form.addRow(self.folder_label, self.folder)

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
        # отдельной строкой, а не в форме: QFormLayout обрезает переносимый текст
        self.package_hint = QLabel()
        self.package_hint.setWordWrap(True)
        self.package_hint.setStyleSheet("color: gray;")
        root.addWidget(self.package_hint)

        # --- список слоёв
        box = self.layers_box = QGroupBox()
        box_layout = QVBoxLayout(box)
        self.layers = QListWidget()
        self.layers.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.layers.setMinimumHeight(200)  # в режиме «все слои» список бывает длинным
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
        self.replace = QCheckBox()
        self.structure = QCheckBox("Сохранять структуру папок")
        self.structure.setToolTip(
            "Файлы лягут в такие же подпапки, как исходные, а не все в одну папку: "
            "«Проект/Данные/Растры/dem.tif» → «<папка>/Данные/Растры/dem.tif». Подпапки считаются "
            "от папки проекта; файлы вне неё — от их общей папки.\n"
            "Слои в памяти, временные и из баз данных — в саму папку. "
            "При формате «все слои в одном файле» подпапки получают только растры.")
        self.styles = QCheckBox("Сохранить стили слоёв")
        self.styles.setToolTip("Файл .qml рядом с данными или стиль по умолчанию внутри GeoPackage — "
                               "при открытии файла стиль подхватится сам.")
        self.overwrite = QCheckBox("Перезаписывать существующие файлы")
        self.overwrite.setToolTip("Если выключено, к имени добавится _2, _3…")
        self.pkg_fonts = QCheckBox("Скопировать в data_all/fonts шрифты со свободной лицензией")
        self.pkg_fonts.setToolTip("Шрифты подписей, значков и макетов. Платные и шрифты без указанной "
                                  "лицензии не копируются — они перечисляются в «Состав.txt».")
        self._pkg_widgets = (self.pkg_fonts,)
        for w in (self.replace, self.structure, self.styles, self.overwrite) + self._pkg_widgets:
            root.addWidget(w)
        hint = QLabel("Растры сохраняются отдельными файлами независимо от выбранного формата: "
                      "без смены СК — копируются как есть (VRT и растры из баз — в GeoTIFF), "
                      "со сменой — перепроецируются в GeoTIFF.")
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
        # «Открыть папку» — вне QDialogButtonBox: тот сам показывает все свои кнопки
        self.btn_open = QPushButton("Открыть папку")
        self.btn_open.setVisible(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.btn_open)
        bottom.addWidget(self.buttons, 1)
        root.addLayout(bottom)
        self._last_folder = ""

        # --- сигналы
        self.btn_save.clicked.connect(self.run)
        self.btn_close.clicked.connect(self.reject)
        self.btn_open.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_folder)))
        self.btn_all.clicked.connect(lambda: self._set_all(CHECKED))
        self.btn_none.clicked.connect(lambda: self._set_all(UNCHECKED))
        self.btn_refresh.clicked.connect(self.refresh)
        self.layers.itemChanged.connect(self._update_count)
        self.format.currentIndexChanged.connect(self._format_changed)

        self._load_settings()
        self._format_changed()
        self._apply_mode()
        for radio in self._radios.values():
            radio.toggled.connect(self._mode_changed)
        self.refresh()

    # ------------------------------------------------------------ настройки
    def _load_settings(self):
        s = QgsSettings()
        folder = s.value(SETTINGS + "folder", "")
        if not folder and self.project.absolutePath():
            folder = self.project.absolutePath()
        self.folder.setFilePath(folder or "")
        # ключ «output_format», а не прежний «format»: с версии 1.2.0 по умолчанию
        # файл на каждый слой, и старый сохранённый выбор сбрасывается один раз
        idx = self.format.findData(s.value(SETTINGS + "output_format", saver.FORMATS[0]["key"]))
        self.format.setCurrentIndex(max(idx, 0))
        self.gpkg_name.setText(s.value(SETTINGS + "gpkg_name", "temporary_layers"))
        # «Заменить в проекте» помним отдельно для каждого режима: для постоянных
        # слоёв по умолчанию выключено — проект остаётся на исходных данных
        self._replace = {MODE_TEMP: _bool(s.value(SETTINGS + "replace"), True),
                         MODE_ALL: _bool(s.value(SETTINGS + "replace_all"), False)}
        mode = s.value(SETTINGS + "mode", MODE_TEMP)
        self._mode = mode if mode in self._radios else MODE_TEMP
        self._radios[self._mode].setChecked(True)
        self.pkg_fonts.setChecked(_bool(s.value(SETTINGS + "pkg_fonts"), True))
        self.structure.setChecked(_bool(s.value(SETTINGS + "keep_structure"), False))
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
        s.setValue(SETTINGS + "output_format", self.format.currentData())
        s.remove(SETTINGS + "format")
        s.setValue(SETTINGS + "gpkg_name", self.gpkg_name.text())
        if self._mode in self._replace:
            self._replace[self._mode] = self.replace.isChecked()
        s.setValue(SETTINGS + "mode", self._mode)
        s.setValue(SETTINGS + "pkg_fonts", self.pkg_fonts.isChecked())
        s.setValue(SETTINGS + "keep_structure", self.structure.isChecked())
        s.setValue(SETTINGS + "replace", self._replace[MODE_TEMP])
        s.setValue(SETTINGS + "replace_all", self._replace[MODE_ALL])
        s.setValue(SETTINGS + "styles", self.styles.isChecked())
        s.setValue(SETTINGS + "overwrite", self.overwrite.isChecked())
        crs = self.crs.crs()
        s.setValue(SETTINGS + "crs_authid", crs.authid() if crs.isValid() else "")
        s.setValue(SETTINGS + "crs_wkt", crs.toWkt() if crs.isValid() else "")

    # ------------------------------------------------------------ режим
    def _apply_mode(self):
        package = self._mode == MODE_PACKAGE
        if not package:
            text, tip = REPLACE_TEXT[self._mode]
            self.replace.setText(text)
            self.replace.setToolTip(tip)
            self.replace.setChecked(self._replace[self._mode])
        # при сборке слои всегда переключаются, а папка — data_all рядом с проектом
        self.replace.setVisible(not package)
        self.overwrite.setVisible(not package)
        # у временных слоёв нет исходных папок
        self.structure.setVisible(self._mode != MODE_TEMP)
        self.folder.setVisible(not package)
        self.folder_label.setVisible(not package)
        for w in self._pkg_widgets:
            w.setVisible(package)
        self.layers_box.setTitle(BOX_TITLES[self._mode])
        self.btn_save.setText("Собрать" if package else "Сохранить")
        self._format_changed()
        self._update_package_hint()

    def _mode_changed(self, *_):
        mode = next(m for m, radio in self._radios.items() if radio.isChecked())
        if mode == self._mode:
            return  # сигнал от кнопки, которую выключили
        if self._mode in self._replace:
            self._replace[self._mode] = self.replace.isChecked()
        self._mode = mode
        self._apply_mode()
        self.refresh()

    def _update_package_hint(self, *_):
        package = self._mode == MODE_PACKAGE
        self.package_hint.setVisible(package)
        if not package:
            return
        if not self.project.fileName():
            self.package_hint.setText("Проект ещё не сохранён — сначала будет предложено сохранить его: "
                                      "папка data_all и архив появятся рядом с файлом проекта.")
            return
        self.package_hint.setText(
            "Слои скопируются в «{}», проект переключится на них и сохранится. "
            "Рядом с проектом появится архив «{}.zip».".format(
                packager.data_dir(self.project), packager.archive_name(self.project)))

    def _find(self):
        return saver.find_layers(self.project, temporary_only=self._mode == MODE_TEMP)

    def busy(self):
        return self._busy

    # ------------------------------------------------------------ список
    def _add_item(self, layer, text, fmt="{}   ({})"):
        item = QListWidgetItem(fmt.format(layer.name(), text))
        try:
            item.setIcon(QgsIconUtils.iconForLayer(layer))
        except Exception:
            pass
        self.layers.addItem(item)
        return item

    def refresh(self):
        self.layers.blockSignals(True)
        self.layers.clear()
        items, skipped = self._find()
        for layer, kind, temporary in items:
            item = self._add_item(layer, saver.describe(layer, kind, temporary))
            item.setData(ROLE_ID, layer.id())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(CHECKED)
        for layer, reason in skipped:  # видно, но выбрать нельзя
            item = self._add_item(layer, reason, "{}   — {}")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setToolTip(reason)
        if self.layers.count() == 0:
            item = QListWidgetItem("В проекте нет временных слоёв" if self._mode == MODE_TEMP
                                   else "В проекте нет слоёв")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.layers.addItem(item)
        self.layers.blockSignals(False)
        self._update_count()
        self._update_package_hint()  # окно немодальное: проект могли сохранить под другим именем

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
        # в пакете общий GeoPackage называется по проекту
        single = saver.FORMATS_BY_KEY[self.format.currentData()]["single"] and self._mode != MODE_PACKAGE
        self.gpkg_name.setVisible(single)
        self.gpkg_name_label.setVisible(single)

    # ------------------------------------------------------------ сохранение
    def _selected(self):
        by_id = {item[0].id(): item for item in self._find()[0]}
        return [by_id[it.data(ROLE_ID)] for it in self._items()
                if it.checkState() == CHECKED and it.data(ROLE_ID) in by_id]

    def _on_progress(self, i, total, name):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(i)
        self.progress.setFormat("{} / {}  {}".format(i, total, name) if name else "%v / %m")
        QApplication.processEvents()

    def run(self):
        if self._mode == MODE_PACKAGE:
            return self._run_package()
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

        subdirs = None
        if self._mode != MODE_TEMP and self.structure.isChecked():
            # сначала сама папка сохранения: файл из неё остаётся в своей подпапке
            subdirs = saver.structure_subdirs(items, [folder, self.project.absolutePath()])

        self._start()
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
                subdirs=subdirs,
            )
        except saver.Cancelled:
            results = None
            self.log.appendPlainText("Отменено.")
        except Exception as e:  # noqa: BLE001
            results = None
            self.log.appendPlainText("Ошибка: {}".format(e))
        finally:
            self._finish()

        if results is not None:
            self._report(results, folder)
        self.refresh()

    def _start(self):
        self._save_settings()
        self._busy = True
        self._cancel = False
        self._set_controls_enabled(False)
        self.progress.setVisible(True)
        self.log.clear()
        self.log.setVisible(True)
        self.btn_open.setVisible(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def _finish(self):
        QApplication.restoreOverrideCursor()
        self._busy = False
        self._set_controls_enabled(True)

    def _run_package(self):
        items = self._selected()
        if not items:
            self.refresh()
            return
        if not self._ensure_project_saved():
            return
        edited = [l.name() for l, _kind, temporary in items
                  if isinstance(l, QgsVectorLayer) and not temporary and l.isEditable() and l.isModified()]
        if edited:
            QMessageBox.warning(self, self.windowTitle(),
                                "Сначала сохраните или отмените правки в слоях:\n\n" + "\n".join(edited))
            return
        crs = self.crs.crs()
        text = ("Слои ({}) будут скопированы в папку:\n{}\n\nПроект переключится на эти копии и "
                "будет сохранён. Исходные файлы не удаляются.").format(
                    len(items), packager.data_dir(self.project))
        if crs.isValid():
            text += "\n\nСистема координат слоёв и проекта: {}.".format(crs.authid() or crs.description())
        text += "\n\nЗатем рядом с проектом будет собран архив «{}.zip».\n\nПродолжить?".format(
            packager.archive_name(self.project))
        if QMessageBox.question(self, self.windowTitle(), text) != QMessageBox.StandardButton.Yes:
            return

        self._start()
        result = None
        try:
            result = packager.consolidate_project(
                self.project, items, self.format.currentData(), crs=crs,
                save_styles=self.styles.isChecked(), include_fonts=self.pkg_fonts.isChecked(),
                save_project=lambda: self.iface.actionSaveProject().trigger(),
                progress=self._on_progress, is_cancelled=lambda: self._cancel,
                keep_structure=self.structure.isChecked())
        except saver.Cancelled:
            self.log.appendPlainText("Отменено. Часть слоёв могла уже переключиться на data_all — "
                                     "проект не сохранён, можно закрыть его без сохранения.")
        except Exception as e:  # noqa: BLE001
            self.log.appendPlainText("Ошибка: {}".format(e))
        finally:
            self._finish()
        if result is not None:
            self._report_package(result)
        self.refresh()

    def _ensure_project_saved(self):
        """Копия для пакета строится из файла проекта — он должен быть сохранён."""
        if self.project.fileName() and not self.project.isDirty():
            return True
        answer = QMessageBox.question(
            self, self.windowTitle(),
            "Пакет собирается из сохранённого файла проекта.\n\nСохранить проект сейчас?")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.iface.actionSaveProject().trigger()
        if self.project.fileName() and not self.project.isDirty():
            self._update_package_hint()
            return True
        QMessageBox.warning(self, self.windowTitle(), "Проект не сохранён — упаковка отменена.")
        return False

    def _report_package(self, pkg):
        folder = os.path.dirname(pkg["zip"])
        failed = [r for r in pkg["results"] if not r["ok"]]
        for r in pkg["results"]:
            line = "{} {}".format("✔" if r["ok"] else "✘", r["name"])
            if r["ok"]:
                line += " → " + r["rel"]
            if r["message"]:
                line += "  [{}]".format(r["message"])
            self.log.appendPlainText(line)
        if pkg["already"]:
            self.log.appendPlainText("Уже были в data_all: " + ", ".join(pkg["already"]))
        if pkg["images"]:
            self.log.appendPlainText("Свои картинки в data_all/images: {}".format(len(pkg["images"])))
        fonts = pkg["fonts"]
        if fonts["free"]:
            self.log.appendPlainText("Шрифты в data_all/fonts: " + ", ".join(sorted(fonts["free"])))
        if fonts["paid"]:
            self.log.appendPlainText("Платные шрифты (не вложены): " + ", ".join(fonts["paid"]))
        if fonts["unknown"]:
            self.log.appendPlainText("Шрифты без указанной лицензии (не вложены): " + ", ".join(fonts["unknown"]))
        if fonts["missing"]:
            self.log.appendPlainText("Не найдены шрифты: " + ", ".join(fonts["missing"]))
        if pkg["excluded"]:
            self.log.appendPlainText("Не в data_all и не в архиве: " + ", ".join(pkg["excluded"]))
        self.log.appendPlainText("\nПроект сохранён, слои — в " + pkg["data_dir"])
        self.log.appendPlainText("Архив: " + pkg["zip"])
        self._last_folder = folder
        self.btn_open.setVisible(True)
        level = "Success" if not failed else "Warning"
        self.iface.messageBar().pushMessage(
            "Сборка проекта", "Готово: " + os.path.basename(pkg["zip"]),
            level=saver._enum(Qgis, "MessageLevel", level), duration=10)

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
        self._last_folder = folder
        self.btn_open.setVisible(bool(ok))
        text = "Сохранено слоёв: {} из {} в папку {}".format(len(ok), len(results), folder)
        level = "Success" if not failed else "Warning"
        self.iface.messageBar().pushMessage(
            "Временные слои", text, level=saver._enum(Qgis, "MessageLevel", level), duration=8)
        if ok and self.replace.isChecked():
            self.log.appendPlainText(
                "\nСлои проекта теперь ссылаются на сохранённые файлы. "
                "Не забудьте сохранить проект.")

    def _set_controls_enabled(self, enabled):
        for w in tuple(self._radios.values()) + self._pkg_widgets + (
                self.folder, self.format, self.crs, self.gpkg_name, self.layers, self.btn_all,
                self.btn_none, self.btn_refresh, self.replace, self.structure, self.styles,
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
