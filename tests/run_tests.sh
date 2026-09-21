#!/bin/sh
# Запуск проверок на Python из QGIS для macOS.
# Приложение выбирается само; другое: QGIS_APP=/Applications/QGIS-3.44.app tests/run_tests.sh
# В сборках 3.44+ и 4.x интерпретатор лежит в Contents/MacOS/python3.X, а не в MacOS/bin.
set -e
cd "$(dirname "$0")"
APP="${QGIS_APP:-}"
if [ -z "$APP" ]; then
  for a in /Applications/QGIS-LTR.app /Applications/QGIS.app /Applications/QGIS-3.44.app \
           /Applications/QGIS-3.40.5-old.app; do
    [ -d "$a" ] && APP="$a" && break
  done
fi
[ -d "$APP" ] || { echo "QGIS не найден, укажите QGIS_APP=/путь/к/QGIS.app"; exit 1; }
C="$APP/Contents"
PY="$C/MacOS/bin/python3"
export QGIS_PREFIX_PATH="$C/MacOS"
if [ ! -x "$PY" ]; then
  PY=$(ls "$C/MacOS"/python3.* 2>/dev/null | head -1)
  # у новых сборок стандартная библиотека Python лежит в Contents/Frameworks,
  # а префикс QGIS — само приложение: иначе не найдутся провайдеры (CSV, облака точек, WMS…)
  export PYTHONHOME="$C/Frameworks" QGIS_PREFIX_PATH="$APP"
fi
[ -x "$PY" ] || { echo "Не найден python внутри $APP"; exit 1; }
QPY="$C/Resources/python"
[ -d "$QPY" ] || QPY="$C/Resources/qgis/python"   # так устроены сборки 3.44+ и 4.x
export PYTHONPATH="$QPY${PYTHONPATH:+:$PYTHONPATH}"
for d in "$C/Resources/proj" "$C/Resources/qgis/proj" "$C/Resources/proj9"; do
  [ -d "$d" ] && export PROJ_DATA="$d" PROJ_LIB="$d" && break
done
export QT_QPA_PLATFORM=offscreen
echo "QGIS: $APP"
set +e
"$PY" test_plugin.py > _out.log 2>&1
STATUS=$?
grep -v "proj_create_from_database\|Cannot find proj.db\|propagateSizeHints\|^Warning 1: Field" _out.log
rm -f _out.log
exit $STATUS
