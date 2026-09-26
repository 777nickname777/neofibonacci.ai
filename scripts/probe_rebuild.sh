#!/usr/bin/env bash
# Колоды по живым планам — локально, без ключа модели.
#
# Берёт каталоги пар, разложенные `scripts/probe_harvest.py`, и собирает по
# каждой три варианта: контент-пакет из pack.json (наша схема — модель не
# нужна), план — из plan.json через записанный ответ. Контекстный аудит
# локально не идёт (модели со зрением нет): картинки для листов берутся
# отсюда, а цифры аудита и времени — из живого лога.
#
# Запуск из корня репозитория:
#   bash scripts/probe_rebuild.sh КАТАЛОГ_ПАР ВЫХОД [ПАРАЛЛЕЛЬНО=3]
# Итог: ВЫХОД/<вход>/<шаблон>/<вариант>/png/*.png, лог сборки ВЫХОД/<вход>-<шаблон>.log
set -euo pipefail

PAIRS=$1
OUT=$2
PARALLEL=${3:-3}

for dir in "$PAIRS"/*/*; do
  [ -s "$dir/plan.json" ] && [ -s "$dir/pack.json" ] || continue
  input=$(basename "$(dirname "$dir")")
  name=$(basename "$dir")
  if [ -f "data/templates/$name.pptx" ]; then
    template="data/templates/$name.pptx"
  elif [ -f "data/holdout/$name.pptx" ]; then
    template="data/holdout/$name.pptx"
  else
    echo "нет шаблона $name — пропуск" >&2
    continue
  fi
  recorded="$OUT/recorded/$input/$name"
  mkdir -p "$recorded"
  cp "$dir/plan.json" "$recorded/plan_deck.json"
  python -m deckwright.cli run --template "$template" --content "$dir/pack.json" \
    --recorded "$recorded" --fix auto --output "$OUT/$input/$name" \
    > "$OUT/$input-$name.log" 2>&1 &
  while [ "$(jobs -r | wc -l)" -ge "$PARALLEL" ]; do sleep 2; done
done
wait
echo "готово: $OUT"
