import asyncio
import html
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

BOT_TOKEN = "".join(os.environ["BOT_TOKEN"].split())
CHANNEL_ID = "".join(os.environ["CHANNEL_ID"].split())
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "KiryShop").strip()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()

MAX_PHOTOS = 4
REQUEST_TIMEOUT = 25

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kiryshop-bot")


def allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return bool(
        update.effective_user
        and str(update.effective_user.id) == ALLOWED_USER_ID
    )


def clean_title(raw: str) -> str:
    raw = html.unescape(raw or "")
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r"\s*[-|]\s*Yupoo.*$", "", raw, flags=re.I)
    return raw[:180] or "New product"


def translate_to_english(text: str) -> str:
    try:
        translated = GoogleTranslator(
            source="auto",
            target="en",
        ).translate(text)
        return clean_title(translated)
    except Exception:
        logger.exception("Translation failed")
        return clean_title(text)


def extract_album(url: str) -> tuple[str, list[str]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
        )
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title = ""

    og_title = soup.find("meta", property="og:title")

    if og_title and og_title.get("content"):
        title = og_title["content"]

    elif soup.title:
        title = soup.title.get_text(" ", strip=True)

    candidates = []

    for meta in soup.find_all("meta"):
        if (
            meta.get("property")
            in {"og:image", "twitter:image"}
            and meta.get("content")
        ):
            candidates.append(meta["content"])

    for img in soup.find_all("img"):
        for attr in (
            "data-origin-src",
            "data-original",
            "data-src",
            "data-lazy",
            "src",
        ):
            value = img.get(attr)
            if value:
                candidates.append(value)
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)
        if not text:
            continue

            candidates.extend(
            re.findall(
                r'https?:\\?/\\?/[^"\'\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\'\s]*)?',
                text,
                flags=re.I,
            )
        )

    images = []
    seen = set()

    for item in candidates:
        item = item.replace("\\/", "/").strip()

        if item.startswith("//"):
            item = "https:" + item
        else:
            item = urljoin(url, item)

        low = item.lower()

        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low):
            continue

        if any(x in low for x in ("avatar", "logo", "icon", "qrcode", "qr-code")):
            continue

        if item not in seen:
            seen.add(item)
            images.append(item)

    if not images:
        raise ValueError("Не удалось найти фотографии в этом альбоме Yupoo.")

    return clean_title(title), images[:MAX_PHOTOS]


def get_font(size: int):
    fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for font in fonts:
        if Path(font).exists():
            return ImageFont.truetype(font, size=size)

    return ImageFont.load_default()


def add_watermark(source_path: Path, output_path: Path):
    with Image.open(source_path).convert("RGBA") as image:

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font_size = max(24, int(min(image.size) * 0.055))
        font = get_font(font_size)

        box = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)

        text_w = box[2] - box[0]
        text_h = box[3] - box[1]

        padding = max(14, int(font_size * 0.55))

        x = image.width - text_w - padding
        y = image.height - text_h - padding

        bg = max(8, int(font_size * 0.25))

        draw.rounded_rectangle(
            (
                x - bg,
                y - bg,
                x + text_w + bg,
                y + text_h + bg,
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
        result.save(
            output_path,
            format="JPEG",
            quality=91,
            optimize=True,
        )
        def download_and_watermark(
    urls: list[str],
    folder: Path,
    referer: str,
) -> list[Path]:

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": referer,
        "Accept": (
            "image/avif,image/webp,image/apng,"
            "image/jpeg,image/*,*/*;q=0.8"
        ),
    }

    output_files = []

    session = requests.Session()
    session.headers.update(headers)

    for index, image_url in enumerate(urls, start=1):

        response = session.get(
            image_url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )

        response.raise_for_status()

        source = folder / f"source_{index}.img"
        output = folder / f"product_{index}.jpg"

        with source.open("wb") as f:
            for chunk in response.iter_content(
                chunk_size=1024 * 128
            ):
                if chunk:
                    f.write(chunk)

        add_watermark(source, output)

        output_files.append(output)

    if not output_files:
        raise ValueError(
            "Не удалось скачать фотографии."
        )

    return output_files


async def handle_yupoo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not allowed(update):
        await update.message.reply_text(
            "У вас нет доступа к этому боту."
        )
        return

    text = (update.message.text or "").strip()

    if "yupoo.com" not in text:
        await update.message.reply_text(
            "Отправь мне ссылку на альбом Yupoo."
        )
        return

    status = await update.message.reply_text(
        "Загружаю товар..."
    )

    try:

        title, image_urls = await asyncio.to_thread(
            extract_album,
            text,
        )

        english_title = await asyncio.to_thread(
            translate_to_english,
            title,
        )

        with tempfile.TemporaryDirectory() as tmp:

            folder = Path(tmp)

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

            opened = [
                path.open("rb")
                for path in files
            ]
                        try:
                media = []

                for index, file_obj in enumerate(opened):

                    if index == 0:
                        media.append(
                            InputMediaPhoto(
                                media=file_obj,
                                caption=caption,
                                parse_mode="HTML",
                            )
                        )
                    else:
                        media.append(
                            InputMediaPhoto(
                                media=file_obj,
                            )
                        )

                await context.bot.send_media_group(
                    chat_id=CHANNEL_ID,
                    media=media,
                )

            finally:
                for file_obj in opened:
                    file_obj.close()

        await status.edit_text(
            "✅ Товар опубликован."
        )

    except Exception as exc:
        logger.exception(
            "Failed to process album"
        )

        await status.edit_text(
            f"❌ Ошибка: {exc}"
        )


def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_yupoo,
        )
    )

    logger.info("Bot started")

    app.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
                
