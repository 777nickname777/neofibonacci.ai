# Набор «Наблюдаемость (контент датасета)»: 3 шаблона датасета × 3 варианта

Живой прогон `llm-probe.yml` run 35 (36389702634, коммит 443cf0a): модель, аудит по картинкам и по тексту, цикл исправления `auto`. Колоды пересобраны локально по живому плану тем же кодом — вёрстка детерминирована; время и находки — из лога живого прогона.

Время — секунды: вариант (раскладка, сборка, аудит, исправление) и вся колода с разбором входа и планом — бюджет 300 с.

| шаблон | вариант | слайдов | вариант, с | колода, с | error | warning | info |
|---|---|---|---|---|---|---|---|
| vk_tech | dense | 11 | 171 | 180 | 0 | 1 | 3 |
| vk_tech | balanced | 11 | 92 | 180 | 1 | 1 | 3 |
| vk_tech | airy | 11 | 85 | 180 | 0 | 1 | 3 |
| vk_workspace | dense | 12 | 148 | 157 | 2 | 4 | 4 |
| vk_workspace | balanced | 12 | 106 | 157 | 3 | 4 | 2 |
| vk_workspace | airy | 12 | 114 | 157 | 2 | 4 | 6 |
| vk_education | dense | 11 | 195 | 196 | 0 | 4 | 5 |
| vk_education | balanced | 11 | 141 | 196 | 0 | 4 | 5 |
| vk_education | airy | 11 | 91 | 196 | 0 | 4 | 5 |

Частые находки (error и warning, все колоды набора):

* `content.title_is_takeaway` — 9
* `layout.text_overflow` — 9
* `density.bullet_too_long` — 9
* `layout.text_over_decor` — 4
* `content.derived_figure_wrong` — 3
* `content.no_prompt_leftovers` — 1

Сводный лист всех колод — `sheet.png`.
