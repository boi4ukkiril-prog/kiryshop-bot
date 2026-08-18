# KiryShop Bot — FINAL CLEAN

Версия: `KIRYSHOP-PRODUCT-PHOTOS-ONLY-2026-08-18`

Что исправлено и проверено:

- бот собирает весь раздел Yupoo и разворачивает полный список: публикация идёт от старых товаров к новым;
- новые товары поставщика оказываются в конце Telegram-ленты;
- максимум 3 разных фото товара на одну публикацию;
- размерные сетки и таблицы фильтруются в несколько этапов:
  1. по URL/alt/title/HTML-подсказкам;
  2. по белому/серому «документному» фону;
  3. по длинным горизонтальным и вертикальным линиям таблиц;
  4. по повторяющимся текстовым строкам;
  5. отдельно проверяются верхняя, средняя, нижняя и боковые части изображения, чтобы отсеять коллажи «таблица + фото товара»;
- если изображение невозможно нормально проверить, оно НЕ публикуется;
- если в альбоме после фильтра нет чистых фото товара, весь альбом пропускается — размерная сетка не используется как запасное фото;
- одинаковые и визуально почти одинаковые фото удаляются;
- восстановлены функции fingerprint/hash_distance для удаления дублей;
- исправлена и подключена команда `/version`;
- сохранены новые топики: Hermes, Protocol Index, Paly Hollywood;
- `/register Protocol Index`, `/register Hermes`, `/register Paly Hollywood` работают через нормализатор;
- база отмечает альбом опубликованным только после успешной отправки в Telegram;
- подпись остаётся: `📩 Price and order in private messages`.

## После загрузки на Railway

Проверь:

`/version`

Бот должен ответить:

`KiryShop build: KIRYSHOP-PRODUCT-PHOTOS-ONLY-2026-08-18`

Новые темы регистрируются внутри соответствующей Telegram-темы:

`/register Hermes`

`/register Protocol Index`

`/register Paly Hollywood`

## Переменные Railway

Нужны:

- `BOT_TOKEN`
- `GROUP_ID`
- `DATABASE_URL`

Опционально:

- `ALLOWED_USER_ID`
- `WATERMARK_TEXT` (по умолчанию `KiryShop`)


## Duplicate reset
If a category shows all albums as Duplicates after changing the posting format:
`/reset Protocol Index`

This deletes only duplicate-history records for that category. It does not delete Telegram posts or topic registrations.


## WORKING ONE MAIN PHOTO
Fixed the broken downloader in the previous build.

- 1 product = 1 main photo
- first Yupoo album image is used
- no visual size-chart filtering
- no media groups
- duplicate is recorded only after Telegram sends successfully
- /reset <category> remains available

/version should show:
WORKING-ONE-MAIN-PHOTO-2026-08-18


## YUPOO COVER PHOTO BUILD
Important distinction:
- NOT the first photo in the detail gallery (often a size chart)
- uses the album COVER / header thumbnail shown beside the product title
- 1 product = 1 cover photo
- gallery images are only download fallbacks

/version:
YUPOO-COVER-PHOTO-2026-08-18
