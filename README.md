# KiryShop Bot — Order, Photo Filter & Topics Fix

Исправлено:
- импорт каталога идёт от самых старых товаров к самым новым;
- новые товары поставщика оказываются в конце Telegram-ленты;
- до 3 уникальных фото на товар;
- одинаковые URL/файлы/визуально близкие копии удаляются;
- размерные сетки отсекаются сначала по HTML/названию, затем по содержимому изображения;
- добавлены новые категории: Hermes, Protocol Index, Paly Hollywood;
- добавлены дополнительные алиасы для Belt/Jewellery, Old Money, FOG и LOE;
- подпись: 📩 Price and order in private messages.

После обновления бота новые Telegram-темы нужно один раз зарегистрировать внутри соответствующей темы:
/register Hermes
/register Protocol Index
/register Paly Hollywood


## REGISTER FIX
This build contains a stronger /register category normalizer.
Supported new topic registrations include:
- /register Protocol Index
- /register Hermes
- /register Paly Hollywood

Use /version after Railway deploy. Expected response:
KiryShop build: REGISTER-FIX-2026-08-18
