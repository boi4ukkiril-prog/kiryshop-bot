import asyncio
import html
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import psycopg
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from PIL import Image, ImageDraw, ImageFont
from telegram import InputMediaPhoto, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = "".join(os.environ["BOT_TOKEN"].split())
GROUP_ID = "".join(os.environ["GROUP_ID"].split())
DATABASE_URL = os.environ["DATABASE_URL"].strip()
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "KiryShop").strip() or "KiryShop"
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()

MAX_PHOTOS = 4
REQUEST_TIMEOUT = 35
PAUSE_BETWEEN_PRODUCTS = 2.0

CATEGORIES = {
    "down coat": "Down COAT",
    "shoes": "Shoes",
    "cap": "Cap",
    "bag": "Bag",
    "belt/jewellery": "Belt/Jewellery",
    "female": "Female",
    "vv": "VV",
    "ch": "CH",
    "cd": "CD",
    "old money": "Old Money",
    "fog essentials": "FOG Essentials",
    "casablanca": "Casablanca",
    "bal": "BAL",
    "prd": "PRD",
    "monc": "MONC",
    "goose": "Goose",
    "denim tear": "Denim Tear",
    "gallery dept": "Gallery Dept",
    "palm angels/paly hollywood": "Palm Angels",
    "hellstar": "HellStar",
    "sp5der": "SP5der",
    "erd": "ERD",
    "acne": "Acne",
    "bbr": "BBR",
    "ce1": "CE1",
    "ami & ralph lauren": "Ami & Ralph Lauren",
    "corteiz & godspeed": "Corteiz & GodSpeed",
    "jacquemus": "Jacquemus",
    "loe": "LOE",
    "guc": "GUC",
    "fd": "FD",
    "ow": "OW",
    "tb": "TB",
    "slp": "SLP",
    "miu": "Miu",
    "represent": "Represent",
    "saint m": "SAINT M",
    "arcteryx": "Arcteryx",
    "amiri": "Amiri",
    "vet": "VET",
    "sale": "Sale",
}


BRAND_ALIASES = {
    "Hermès": ("hermes", "hermès"),
    "Nike": ("nike",),
    "Adidas": ("adidas",),
    "Balenciaga": ("balenciaga",),
    "Dior": ("dior", "christian dior"),
    "Gucci": ("gucci",),
    "Prada": ("prada",),
    "Louis Vuitton": ("louis vuitton", "louisvuitton"),
    "Moncler": ("moncler",),
    "Canada Goose": ("canada goose",),
    "Burberry": ("burberry",),
    "Celine": ("celine",),
    "Saint Laurent": ("saint laurent", "ysl"),
    "Off-White": ("off-white", "off white"),
    "Palm Angels": ("palm angels",),
    "Gallery Dept": ("gallery dept",),
    "Denim Tears": ("denim tears", "denim tear"),
    "Fear of God": ("fear of god", "essentials"),
    "Amiri": ("amiri",),
    "Vetements": ("vetements",),
    "Represent": ("represent",),
    "Arc'teryx": ("arcteryx", "arc'teryx"),
    "Jacquemus": ("jacquemus",),
    "Loewe": ("loewe",),
    "Miu Miu": ("miu miu",),
    "Ami Paris": ("ami paris",),
    "Ralph Lauren": ("ralph lauren",),
    "Corteiz": ("corteiz",),
    "Godspeed": ("godspeed",),
    "Casablanca": ("casablanca",),
    "Hellstar": ("hellstar",),
    "Sp5der": ("sp5der",),
    "Acne Studios": ("acne studios",),
    "Chrome Hearts": ("chrome hearts",),
    "Valentino": ("valentino",),
    "New Balance": ("new balance",),
    "Jordan": ("air jordan", "jordan"),
    "UGG": ("ugg",),
    "Stone Island": ("stone island",),
    "Givenchy": ("givenchy",),
    "Fendi": ("fendi",),
    "Versace": ("versace",),
    "Bottega Veneta": ("bottega veneta",),
    "Alexander McQueen": ("alexander mcqueen", "mcqueen"),
    "Dolce & Gabbana": ("dolce & gabbana", "dolce gabbana"),
}


def extract_brand(text: str) -> str:
    normalized = " ".join((text or "").lower().split())

    # Remove prices and Yupoo boilerplate from the album title only.
    normalized = re.sub(
        r"\bprice\s*[:：]?\s*\d+(?:[.,]\d+)?\b",
        " ",
        normalized,
        flags=re.I,
    )
    normalized = normalized.replace("supplier product catalog", " ")
    normalized = normalized.replace("yupoo", " ")
    normalized = normalized.replace("album", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Match whole words/phrases, not fragments from unrelated text.
    for brand, aliases in BRAND_ALIASES.items():
        for alias in aliases:
            pattern = r"(?<![a-z0-9])" + re.escape(alias.lower()) + r"(?![a-z0-9])"
            if re.search(pattern, normalized, flags=re.I):
                return brand

    # If brand is not confidently found, keep a clean album title
    # instead of assigning a wrong brand.
    cleaned = re.sub(r"\bprice\s*[:：]?\s*\d+(?:[.,]\d+)?\b", " ", text, flags=re.I)
    cleaned = re.sub(
        r"\s*[\|\-]\s*(album|tophotfashion|supplier product catalog).*$",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" |-")

    return cleaned[:80] or "Product"


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kiryshop-bot-v2")


def allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    user = update.effective_user
    return bool(user and str(user.id) == ALLOWED_USER_ID)


def browser_headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
            "AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    return headers


@contextmanager
def db():
    with psycopg.connect(DATABASE_URL) as connection:
        yield connection


def init_db() -> None:
    with db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    category_name TEXT PRIMARY KEY,
                    thread_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS published_albums (
                    album_id TEXT PRIMARY KEY,
                    album_url TEXT NOT NULL,
                    category_name TEXT NOT NULL,
                    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        connection.commit()


def save_topic(category_name: str, thread_id: int) -> None:
    with db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO topics(category_name, thread_id)
                VALUES (%s, %s)
                ON CONFLICT(category_name)
                DO UPDATE SET thread_id = EXCLUDED.thread_id
                """,
                (category_name, thread_id),
            )
        connection.commit()


def load_topic(category_name: str) -> int | None:
    with db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT thread_id FROM topics WHERE category_name = %s",
                (category_name,),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else None


def was_published(album_id_value: str) -> bool:
    with db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM published_albums WHERE album_id = %s",
                (album_id_value,),
            )
            return cursor.fetchone() is not None


def remember_album(
    album_id_value: str,
    album_url: str,
    category_name: str,
) -> None:
    with db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO published_albums(album_id, album_url, category_name)
                VALUES (%s, %s, %s)
                ON CONFLICT(album_id) DO NOTHING
                """,
                (album_id_value, album_url, category_name),
            )
        connection.commit()


def album_id(url: str) -> str | None:
    match = re.search(r"/albums/(\d+)", url)
    return match.group(1) if match else None


def canonical_category(raw: str) -> str | None:
    cleaned = " ".join(raw.strip().lower().split())
    for key, canonical in CATEGORIES.items():
        if cleaned in {key.lower(), canonical.lower()}:
            return canonical
    return None


def set_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def detect_category(soup: BeautifulSoup, url: str) -> str:
    parts = [url.lower()]

    for selector in (
        "h1",
        ".category-name",
        ".showalbumheader__gallerytitle",
        "title",
    ):
        element = soup.select_one(selector)
        if element:
            parts.append(element.get_text(" ", strip=True).lower())

    combined = " | ".join(parts)

    for key, canonical in CATEGORIES.items():
        if key in combined:
            return canonical

    raise ValueError("Не удалось определить категорию раздела.")


def normalize_album_url(full_url: str) -> str:
    parsed = urlparse(full_url)
    query = parse_qs(parsed.query)

    if "uid" not in query:
        query["uid"] = ["1"]

    return urlunparse(
        parsed._replace(
            query=urlencode(query, doseq=True),
            fragment="",
        )
    )


def category_page(url: str) -> tuple[str, list[str]]:
    response = requests.get(
        url,
        headers=browser_headers(),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    category_name = detect_category(soup, url)

    links: list[str] = []
    seen_ids: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        full = urljoin(url, str(anchor["href"]))

        if "/albums/" not in full:
            continue

        full = normalize_album_url(full)
        current_id = album_id(full)

        if current_id and current_id not in seen_ids:
            seen_ids.add(current_id)
            links.append(full)

    return category_name, links


def all_album_links(
    category_url: str,
    max_pages: int = 100,
) -> tuple[str, list[str]]:
    category_name: str | None = None
    links: list[str] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        current_category, current_links = category_page(
            set_page(category_url, page)
        )
        category_name = category_name or current_category

        new_links: list[str] = []

        for link in current_links:
            current_id = album_id(link)
            if current_id and current_id not in seen_ids:
                seen_ids.add(current_id)
                new_links.append(link)

        if not new_links:
            break

        links.extend(new_links)

    if not category_name:
        raise ValueError("Категория не найдена.")

    if not links:
        raise ValueError("В разделе не найдено товаров.")

    return category_name, links


def extract_album(album_url: str) -> tuple[str, list[str]]:
    response = requests.get(
        album_url,
        headers=browser_headers(album_url),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title = ""
    meta = soup.find("meta", property="og:title")

    if meta and meta.get("content"):
        title = str(meta["content"])
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)

    # Only use album-specific fields for brand detection.
    brand_sources = [title]

    for meta_tag in soup.find_all("meta"):
        name = str(meta_tag.get("property") or meta_tag.get("name") or "").lower()
        content = meta_tag.get("content")

        if content and name in {
            "og:title",
            "twitter:title",
            "description",
            "og:description",
        }:
            brand_sources.append(str(content))

    candidates: list[str] = []

    for meta_tag in soup.find_all("meta"):
        prop = meta_tag.get("property") or meta_tag.get("name")
        content = meta_tag.get("content")

        if content and prop in {
            "og:image",
            "twitter:image",
            "twitter:image:src",
        }:
            candidates.append(str(content))

    for image in soup.find_all("img"):
        for text_attr in ("alt", "title"):
            text_value = image.get(text_attr)
            if text_value:
                brand_sources.append(str(text_value))

        for attr in (
            "data-origin-src",
            "data-original",
            "data-src",
            "data-lazy",
            "data-url",
            "src",
        ):
            value = image.get(attr)
            if value:
                candidates.append(str(value))

    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False)

        if not text:
            continue

        candidates.extend(
            re.findall(
                r'https?:\\?/\\?/[^"\'\s]+?\.(?:jpg|jpeg|png|webp)'
                r'(?:\?[^"\'\s]*)?',
                text,
                flags=re.I,
            )
        )

    images: list[str] = []
    seen: set[str] = set()

    for item in candidates:
        item = item.replace("\\/", "/").strip()

        if item.startswith("//"):
            item = "https:" + item
        else:
            item = urljoin(album_url, item)

        low = item.lower()

        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low):
            continue

        if any(
            word in low
            for word in ("avatar", "logo", "icon", "qrcode", "favicon")
        ):
            continue

        if item not in seen:
            seen.add(item)
            images.append(item)

    if not images:
        raise ValueError("Не удалось найти фотографии товара.")

    brand = extract_brand(" | ".join(brand_sources))
    return brand, images[:MAX_PHOTOS]


def translate_title(title: str) -> str:
    try:
        translated = GoogleTranslator(
            source="auto",
            target="en",
        ).translate(title)

        return " ".join((translated or title).split())[:180]

    except Exception:
        logger.exception("Translation failed")
        return " ".join(title.split())[:180]


def font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)

    return ImageFont.load_default()


def watermark(source: Path, output: Path) -> None:
    with Image.open(source).convert("RGBA") as image:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        used_font = font(max(24, int(min(image.size) * 0.055)))

        box = draw.textbbox((0, 0), WATERMARK_TEXT, font=used_font)
        width = box[2] - box[0]
        height = box[3] - box[1]
        padding = 18

        x = max(10, image.width - width - padding)
        y = max(10, image.height - height - padding)

        draw.rounded_rectangle(
            (
                x - 10,
                y - 10,
                x + width + 10,
                y + height + 10,
            ),
            radius=10,
            fill=(0, 0, 0, 115),
        )

        draw.text(
            (x, y),
            WATERMARK_TEXT,
            font=used_font,
            fill=(255, 255, 255, 225),
        )

        Image.alpha_composite(
            image,
            overlay,
        ).convert("RGB").save(
            output,
            "JPEG",
            quality=91,
            optimize=True,
        )


def download_images(
    urls: list[str],
    folder: Path,
    referer: str,
) -> list[Path]:
    session = requests.Session()
    headers = browser_headers(referer)
    headers["Accept"] = (
        "image/avif,image/webp,image/apng,"
        "image/jpeg,image/*,*/*;q=0.8"
    )
    session.headers.update(headers)

    output_files: list[Path] = []

    for index, image_url in enumerate(urls, start=1):
        response = session.get(
            image_url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        if "image" not in content_type:
            raise ValueError(
                f"Yupoo не отдал фотографию №{index}."
            )

        source = folder / f"source_{index}.img"
        output = folder / f"product_{index}.jpg"

        with source.open("wb") as file:
            for chunk in response.iter_content(1024 * 128):
                if chunk:
                    file.write(chunk)

        watermark(source, output)
        output_files.append(output)

    return output_files


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context

    if not update.message or not allowed(update):
        return

    await update.message.reply_text(
        "Команды:\n"
        "/topics — список категорий\n"
        "/register Название — выполнить внутри нужной темы группы\n\n"
        "После регистрации тем отправь мне ссылку на целый раздел Yupoo."
    )


async def topics(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    del context

    if not update.message or not allowed(update):
        return

    await update.message.reply_text(
        "\n".join(sorted(set(CATEGORIES.values())))
    )


async def register(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not allowed(update):
        return

    if not update.effective_chat:
        return

    if str(update.effective_chat.id) != GROUP_ID:
        await update.message.reply_text(
            "Команду нужно отправить в группе KiryShop."
        )
        return

    thread_id = update.message.message_thread_id

    if not thread_id:
        await update.message.reply_text(
            "Команду нужно отправить внутри конкретной темы."
        )
        return

    category_name = canonical_category(
        " ".join(context.args)
    )

    if not category_name:
        await update.message.reply_text(
            "Категория не найдена. Используй /topics."
        )
        return

    await asyncio.to_thread(
        save_topic,
        category_name,
        thread_id,
    )

    await update.message.reply_text(
        f"✅ Registered: {category_name}"
    )


async def publish_album(
    context: ContextTypes.DEFAULT_TYPE,
    album_url: str,
    category_name: str,
    thread_id: int,
) -> None:
    current_id = album_id(album_url)

    if not current_id:
        raise ValueError(
            "Не удалось определить ID альбома."
        )

    title, image_urls = await asyncio.to_thread(
        extract_album,
        album_url,
    )

    english_title = title

    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)

        files = await asyncio.to_thread(
            download_images,
            image_urls,
            folder,
            album_url,
        )

        caption = "📩 Price and order in private messages"

        opened = [path.open("rb") for path in files]

        try:
            media: list[InputMediaPhoto] = []

            for index, file_object in enumerate(opened):
                if index == 0:
                    media.append(
                        InputMediaPhoto(
                            media=file_object,
                            caption=caption,
                            parse_mode="HTML",
                        )
                    )
                else:
                    media.append(
                        InputMediaPhoto(
                            media=file_object,
                        )
                    )

            await context.bot.send_media_group(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                media=media,
            )

        finally:
            for file_object in opened:
                file_object.close()

    await asyncio.to_thread(
        remember_album,
        current_id,
        album_url,
        category_name,
    )


async def import_category(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not allowed(update):
        return

    url = (update.message.text or "").strip()

    if "yupoo.com" not in url.lower():
        await update.message.reply_text(
            "Отправь ссылку на раздел Yupoo."
        )
        return

    status = await update.message.reply_text(
        "Ищу товары…"
    )

    try:
        category_name, links = await asyncio.to_thread(
            all_album_links,
            url,
        )

        thread_id = await asyncio.to_thread(
            load_topic,
            category_name,
        )

        if not thread_id:
            await status.edit_text(
                f"❌ Тема {category_name} не зарегистрирована.\n"
                f"Создай тему и отправь в ней:\n"
                f"/register {category_name}"
            )
            return

        published = 0
        duplicates = 0
        errors = 0

        for position, album_url in enumerate(
            links,
            start=1,
        ):
            current_id = album_id(album_url)

            if current_id and await asyncio.to_thread(
                was_published,
                current_id,
            ):
                duplicates += 1
                continue

            try:
                await publish_album(
                    context,
                    album_url,
                    category_name,
                    thread_id,
                )
                published += 1

            except requests.HTTPError as error:
                errors += 1
                logger.error(
                    "Album HTTP error: %s | %s",
                    album_url,
                    error,
                )

            except Exception:
                errors += 1
                logger.exception(
                    "Album failed: %s",
                    album_url,
                )

            if position % 10 == 0:
                try:
                    await status.edit_text(
                        f"{category_name}\n"
                        f"Processed: {position}/{len(links)}\n"
                        f"Published: {published}\n"
                        f"Duplicates: {duplicates}\n"
                        f"Errors: {errors}"
                    )
                except Exception:
                    pass

            await asyncio.sleep(
                PAUSE_BETWEEN_PRODUCTS
            )

        await status.edit_text(
            "✅ Finished\n\n"
            f"Category: {category_name}\n"
            f"Found: {len(links)}\n"
            f"Published: {published}\n"
            f"Duplicates: {duplicates}\n"
            f"Errors: {errors}"
        )

    except Exception as error:
        logger.exception("Import failed")
        await status.edit_text(
            f"❌ Ошибка: {error}"
        )


def main() -> None:
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )
    app.add_handler(
        CommandHandler("topics", topics)
    )
    app.add_handler(
        CommandHandler("register", register)
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            import_category,
        )
    )

    logger.info("KiryShop Bot v2 started")
    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
