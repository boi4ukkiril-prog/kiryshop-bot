import asyncio
import html
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BOT_TOKEN = "".join(os.environ["BOT_TOKEN"].split())
CHANNEL_ID = "".join(os.environ["CHANNEL_ID"].split())
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "KiryShop").strip() or "KiryShop"
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()

MAX_PHOTOS = 4
REQUEST_TIMEOUT = 30

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kiryshop-bot")


def user_is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True

    user = update.effective_user
    return bool(user and str(user.id) == ALLOWED_USER_ID)


def clean_title(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*[-|]\s*Yupoo.*$", "", text, flags=re.I)
    return text[:180] or "New product"


def translate_to_english(text: str) -> str:
    try:
        translated = GoogleTranslator(source="auto", target="en").translate(text)
        return clean_title(translated or text)
    except Exception:
        logger.exception("Translation failed")
        return clean_title(text)


def _page_headers() -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def extract_album(url: str) -> tuple[str, list[str]]:
    response = requests.get(
        url,
        headers=_page_headers(),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title = ""
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = str(og_title["content"])
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)

    candidates: list[str] = []

    for meta in soup.find_all("meta"):
        content = meta.get("content")
        prop = meta.get("property") or meta.get("name")
        if content and prop in {"og:image", "twitter:image", "twitter:image:src"}:
            candidates.append(str(content))

    for image in soup.find_all("img"):
        for attribute in (
            "data-origin-src",
            "data-original",
            "data-src",
            "data-lazy",
            "data-url",
            "src",
        ):
            value = image.get(attribute)
            if value:
                candidates.append(str(value))

    for script in soup.find_all("script"):
        script_text = script.string or script.get_text(" ", strip=False)
        if not script_text:
            continue

        found = re.findall(
            r'https?:\\?/\\?/[^"\'\s]+?\.(?:jpg|jpeg|png|webp)'
            r'(?:\?[^"\'\s]*)?',
            script_text,
            flags=re.I,
        )
        candidates.extend(found)

    images: list[str] = []
    seen: set[str] = set()

    for item in candidates:
        item = item.replace("\\/", "/").strip()

        if item.startswith("//"):
            item = "https:" + item
        else:
            item = urljoin(url, item)

        parsed = urlparse(item)
        if parsed.scheme not in {"http", "https"}:
            continue

        low = item.lower()
        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low):
            continue

        if any(
            excluded in low
            for excluded in ("avatar", "logo", "icon", "qrcode", "qr-code", "favicon")
        ):
            continue

        if item not in seen:
            seen.add(item)
            images.append(item)

    if not images:
        raise ValueError(
            "Не удалось найти фотографии. Отправь ссылку именно на отдельный альбом товара Yupoo."
        )

    return clean_title(title), images[:MAX_PHOTOS]


def get_font(size: int):
    font_paths = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )

    for font_path in font_paths:
        if Path(font_path).exists():
            return ImageFont.truetype(font_path, size=size)

    return ImageFont.load_default()


def add_watermark(source_path: Path, output_path: Path) -> None:
    with Image.open(source_path).convert("RGBA") as image:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_size = max(24, int(min(image.size) * 0.055))
        font = get_font(font_size)

        bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        padding = max(14, int(font_size * 0.55))
        x = max(10, image.width - text_width - padding)
        y = max(10, image.height - text_height - padding)

        background_padding = max(8, int(font_size * 0.25))

        draw.rounded_rectangle(
            (
                x - background_padding,
                y - background_padding,
                x + text_width + background_padding,
                y + text_height + background_padding,
            ),
            radius=max(8, int(font_size * 0.25)),
            fill=(0, 0, 0, 115),
        )

        draw.text(
            (x, y),
            WATERMARK_TEXT,
            font=font,
            fill=(255, 255, 255, 225),
        )

        result = Image.alpha_composite(image, overlay).convert("RGB")
        result.save(output_path, format="JPEG", quality=91, optimize=True)


def download_and_watermark(
    urls: list[str],
    folder: Path,
    referer: str,
) -> list[Path]:
    headers = {
        "User-Agent": _page_headers()["User-Agent"],
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    output_files: list[Path] = []
    session = requests.Session()
    session.headers.update(headers)

    for index, image_url in enumerate(urls, start=1):
        response = session.get(
            image_url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "image" not in content_type:
            raise ValueError(f"Yupoo не отдал фотографию №{index}.")

        source_path = folder / f"source_{index}.img"
        output_path = folder / f"product_{index}.jpg"

        with source_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    file.write(chunk)

        add_watermark(source_path, output_path)
        output_files.append(output_path)

    if not output_files:
        raise ValueError("Не удалось скачать фотографии товара.")

    return output_files


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context

    if not update.message:
        return

    if not user_is_allowed(update):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    await update.message.reply_text(
        "Отправь мне ссылку на отдельный альбом товара Yupoo."
    )


async def handle_yupoo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message:
        return

    if not user_is_allowed(update):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = (update.message.text or "").strip()

    if "yupoo.com" not in text.lower():
        await update.message.reply_text(
            "Отправь мне ссылку на отдельный альбом товара Yupoo."
        )
        return

    if "/albums/" not in text:
        await update.message.reply_text(
            "Нужна ссылка на конкретный товар. В правильной ссылке обычно есть /albums/."
        )
        return

    status = await update.message.reply_text("Загружаю товар…")

    try:
        title, image_urls = await asyncio.to_thread(extract_album, text)
        english_title = await asyncio.to_thread(translate_to_english, title)

        with tempfile.TemporaryDirectory() as temporary_directory:
            folder = Path(temporary_directory)

            files = await asyncio.to_thread(
                download_and_watermark,
                image_urls,
                folder,
                text,
            )

            caption = (
                f"<b>{html.escape(english_title)}</b>\n\n"
                "📩 Message for price and order"
            )

            opened_files = [path.open("rb") for path in files]

            try:
                media: list[InputMediaPhoto] = []

                for index, file_object in enumerate(opened_files):
                    if index == 0:
                        media.append(
                            InputMediaPhoto(
                                media=file_object,
                                caption=caption,
                                parse_mode="HTML",
                            )
                        )
                    else:
                        media.append(InputMediaPhoto(media=file_object))

                await context.bot.send_media_group(
                    chat_id=CHANNEL_ID,
                    media=media,
                )
            finally:
                for file_object in opened_files:
                    file_object.close()

        await status.edit_text("✅ Товар опубликован в канале.")

    except Exception as error:
        logger.exception("Failed to process album")
        await status.edit_text(f"❌ Ошибка: {error}")


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_yupoo)
    )

    logger.info("Bot started")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
