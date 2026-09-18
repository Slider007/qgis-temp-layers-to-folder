#!/bin/sh
# Собирает dist/temp_layers_to_folder-<версия>.zip для «Модули → Установить из ZIP».
set -e
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^version=//p' temp_layers_to_folder/metadata.txt)
mkdir -p dist
ZIP="dist/temp_layers_to_folder-$VERSION.zip"
rm -f "$ZIP"
zip -qr "$ZIP" temp_layers_to_folder -x '*__pycache__*' '*.pyc' '*.DS_Store'
echo "$ZIP"
