# Набор «Продажи кофеен (текст sales_q3.txt)»: 3 шаблона датасета × 3 варианта

Живой прогон `llm-probe.yml` run 35 (36389702634, коммит 443cf0a): модель, аудит по картинкам и по тексту, цикл исправления `auto`. Колоды пересобраны локально по живому плану тем же кодом — вёрстка детерминирована; время и находки — из лога живого прогона.

Время — секунды: вариант (раскладка, сборка, аудит, исправление) и вся колода с разбором входа и планом — бюджет 300 с.

| шаблон | вариант | слайдов | вариант, с | колода, с | error | warning | info |
|---|---|---|---|---|---|---|---|
| vk_tech | dense | 12 | 186 | 412 | 0 | 1 | 2 |
| vk_tech | balanced | 12 | 223 | 412 | 0 | 1 | 2 |
| vk_tech | airy | 12 | 112 | 412 | 1 | 1 | 2 |
| vk_workspace | dense | 14 | 214 | 300 | 1 | 1 | 4 |
| vk_workspace | balanced | 14 | 182 | 300 | 1 | 1 | 4 |
| vk_workspace | airy | 14 | 164 | 300 | 0 | 1 | 4 |
| vk_education | dense | 15 | 185 | 258 | 0 | 5 | 4 |
| vk_education | balanced | 15 | 115 | 258 | 0 | 3 | 4 |
| vk_education | airy | 15 | 124 | 258 | 0 | 3 | 4 |

Частые находки (error и warning, все колоды набора):

* `content.title_is_takeaway` — 6
* `content.visuals_on_topic` — 4
* `density.bullet_too_long` — 3
* `layout.text_overflow` — 3
* `content.no_prompt_leftovers` — 2
* `layout.text_over_decor` — 1
* `content.body_matches_title` — 1

Сводный лист всех колод — `sheet.png`.
