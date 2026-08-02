# KiryShop Yupoo → Telegram Bot

Бот принимает ссылку на отдельный альбом товара Yupoo, получает до 4 фотографий,
переводит название товара на английский, наносит водяной знак и публикует товар
в Telegram-канал без цены.

## Railway variables

Добавьте в Railway → Variables:

- `BOT_TOKEN` — токен бота от BotFather.
- `CHANNEL_ID` — username канала, например `@kiryshop`, или цифровой ID.
- `WATERMARK_TEXT` — текст водяного знака.
- `ALLOWED_USER_ID` — необязательно: ваш цифровой Telegram ID.

## Запуск

Railway автоматически использует `Procfile`:

`worker: python bot.py`
