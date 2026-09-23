#!/bin/bash
set -e
cd ~/united-dialog-archive
cp ~/united_dialog.db united_dialog.db
source ~/venv/bin/activate
python generate_site.py
git add -A
git commit -m "auto: $(date '+%Y-%m-%d %H:%M')" || echo "нечего коммитить"
git push
echo "✅ Опубликовано: https://cfmz.github.io/united-dialog-archive/"
