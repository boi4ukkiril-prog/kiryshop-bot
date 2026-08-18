from io import BytesIO
import asyncio
import hashlib
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import psycopg
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


BUILD_VERSION = "YUPOO-COVER-PHOTO-2026-08-18"

BOT_TOKEN = "".join(os.environ["BOT_TOKEN"].split())
GROUP_ID = "".join(os.environ["GROUP_ID"].split())
DATABASE_URL = os.environ["DATABASE_URL"].strip()
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "KiryShop").strip() or "KiryShop"
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()

MAX_PHOTOS = 1
MAX_IMAGE_CANDIDATES = 40
REQUEST_TIMEOUT = 35
PAUSE_BETWEEN_PRODUCTS = 2.0
MIN_IMAGE_SIDE = 320

CATEGORIES = {
    "down coat": "Down COAT",
    "shoes": "Shoes",
    "cap": "Cap",
    "bag": "Bag",
    "belt/jewellery": "Belt/Jewellery",
    "belt jewellery": "Belt/Jewellery",
    "belt🧣jewellery": "Belt/Jewellery",
    "female": "Female",
    "vv": "VV",
    "ch": "CH",
    "cd": "CD",
    "old money": "Old Money",
    "old money 老钱": "Old Money",
    "hermes": "Hermes",
    "hermès": "Hermes",
    "fog essentials": "FOG Essentials",
    "fog": "FOG Essentials",
    "casablanca": "Casablanca",
    "bal": "BAL",
    "prd": "PRD",
    "monc": "MONC",
    "goose": "Goose",
    "denim tear": "Denim Tear",
    "gallery dept": "Gallery Dept",
    "protocol index": "Protocol Index",
    "paly hollywood": "Paly Hollywood",
    "palm angels/paly hollywood": "Paly Hollywood",
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
    "loe & js": "LOE",
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

SIZE_HINT_WORDS = (
    "size chart", "sizechart", "size_chart", "size-chart",
    "size table", "sizetable", "size_table", "size-table",
    "size guide", "sizeguide", "size_guide", "size-guide",
    "measurement", "measurements", "measure chart", "measure-chart",
    "sizing", "dimensions", "dimension chart",
    "尺码", "尺寸", "码表", "参数表", "胸围", "衣长", "肩宽",
    "袖长", "腰围", "臀围", "裤长", "脚长", "建议身高", "建议体重",
)

NON_PRODUCT_HINTS = (
    "avatar", "logo", "icon", "qrcode", "qr-code", "qr_code",
    "favicon", "loading", "placeholder", "sprite", "banner",
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("kiryshop")


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


def remember_album(album_id_value: str, album_url: str, category_name: str) -> None:
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


def reset_published_category(category_name: str) -> int:
    """Forget only this category's published album IDs."""
    with db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM published_albums WHERE category_name = %s",
                (category_name,),
            )
            deleted = cursor.rowcount or 0
        connection.commit()
    return int(deleted)


def album_id(url: str) -> str | None:
    match = re.search(r"/albums/(\d+)", url)
    return match.group(1) if match else None


def _normalize_label(value: str) -> str:
    value = (value or "").strip().casefold()
    value = value.replace("è", "e").replace("é", "e").replace("ê", "e")
    value = re.sub(r"[‐‑‒–—−_/]+", " ", value)
    value = re.sub(r"[^a-z0-9& ]+", " ", value)
    return " ".join(value.split())


def canonical_category(raw: str) -> str | None:
    cleaned = _normalize_label(raw)
    aliases: dict[str, str] = {}

    for key, canonical in CATEGORIES.items():
        aliases[_normalize_label(key)] = canonical
        aliases[_normalize_label(canonical)] = canonical

    aliases.update(
        {
            "protocol": "Protocol Index",
            "protocol index": "Protocol Index",
            "hermes": "Hermes",
            "paly": "Paly Hollywood",
            "paly hollywood": "Paly Hollywood",
            "palm angels paly hollywood": "Paly Hollywood",
        }
    )
    return aliases.get(cleaned)


def set_page(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def detect_category(soup: BeautifulSoup, url: str) -> str:
    text_parts = [url]
    for selector in ("h1", ".category-name", ".showalbumheader__gallerytitle", "title"):
        node = soup.select_one(selector)
        if node:
            text_parts.append(node.get_text(" ", strip=True))

    combined = " | ".join(text_parts).casefold()

    # Prefer longer aliases first so short codes such as CH/CD do not win by accident.
    for key, canonical in sorted(CATEGORIES.items(), key=lambda item: len(item[0]), reverse=True):
        key_low = key.casefold()
        if len(key_low) <= 3 and key_low.isalnum():
            if re.search(r"(?<![a-z0-9])" + re.escape(key_low) + r"(?![a-z0-9])", combined):
                return canonical
        elif key_low in combined:
            return canonical

    raise ValueError("Не удалось определить категорию раздела.")


def normalize_album_url(full_url: str) -> str:
    parsed = urlparse(full_url)
    query = parse_qs(parsed.query)
    if "uid" not in query:
        query["uid"] = ["1"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


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


def all_album_links(category_url: str, max_pages: int = 100) -> tuple[str, list[str]]:
    category_name: str | None = None
    links: list[str] = []
    seen_ids: set[str] = set()

    for page in range(1, max_pages + 1):
        current_category, current_links = category_page(set_page(category_url, page))
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

    # Supplier/Yupoo pages are newest -> oldest. Reversing the COMPLETE catalog
    # makes Telegram posting oldest -> newest, so the supplier's newest products
    # end up at the bottom/end of the Telegram feed.
    links.reverse()
    return category_name, links


def extract_album(album_url: str) -> tuple[str, list[str]]:
    """Return album title and images with the Yupoo COVER image first."""
    response = requests.get(
        album_url,
        headers=browser_headers(album_url),
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    title_node = (
        soup.select_one(".showalbumheader__gallerytitle")
        or soup.select_one(".showalbumheader__gallerytitle--content")
        or soup.find("h1")
        or soup.find("title")
    )
    title = title_node.get_text(" ", strip=True) if title_node else "Product"

    def absolute_image_url(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return ""
        if value.startswith("//"):
            return "https:" + value
        if value.startswith("/"):
            return urljoin(album_url, value)
        return value

    def best_from_tag(tag) -> str:
        if not tag:
            return ""
        # Yupoo commonly stores the original/larger image in data-origin-src.
        for attr in (
            "data-origin-src",
            "data-src",
            "data-lazy",
            "data-original",
            "src",
        ):
            value = absolute_image_url(tag.get(attr, ""))
            if value and not value.startswith("data:"):
                return value
        return ""

    cover_url = ""

    # The album cover is displayed in the header at the left of title/price.
    # Prefer selectors around Yupoo's show-album header/cover area.
    cover_selectors = (
        ".showalbumheader img",
        ".showalbumheader__gallerycover img",
        ".showalbumheader__gallerythumb img",
        ".showalbumheader__galleryimg img",
        ".showalbumheader__gallerycover",
    )
    for selector in cover_selectors:
        tag = soup.select_one(selector)
        if tag:
            if getattr(tag, "name", None) == "img":
                cover_url = best_from_tag(tag)
            else:
                cover_url = best_from_tag(tag.find("img"))
            if cover_url:
                break

    # Fallback: Yupoo exposes the cover in OpenGraph metadata on many album pages.
    if not cover_url:
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta:
            cover_url = absolute_image_url(meta.get("content", ""))

    # Gallery images are kept only as a fallback if the cover URL cannot download.
    gallery_urls = []
    seen = set()
    for img in soup.find_all("img"):
        url = best_from_tag(img)
        if not url:
            continue
        low = url.casefold()
        if any(x in low for x in ("avatar", "logo", "qrcode", "icon")):
            continue
        if url not in seen:
            seen.add(url)
            gallery_urls.append(url)

    ordered = []
    if cover_url:
        ordered.append(cover_url)
    for url in gallery_urls:
        if url not in ordered:
            ordered.append(url)

    if not ordered:
        raise ValueError("Не удалось найти заглавное фото альбома.")

    return title, ordered


def download_images(urls: list[str], folder: Path, referer: str) -> list[Path]:
    """Download exactly one main product image from the Yupoo album."""
    if not urls:
        raise ValueError("В альбоме не найдено фото.")

    last_error = None

    # The first URL is the supplier's main image.
    # Only if it cannot be downloaded, try a few next album images.
    for url in urls[:5]:
        try:
            response = requests.get(
                url,
                headers=browser_headers(referer),
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()

            content_type = (response.headers.get("Content-Type") or "").casefold()
            if content_type and "image" not in content_type:
                continue

            output = folder / "main_photo.jpg"
            with Image.open(BytesIO(response.content)).convert("RGB") as image:
                image.thumbnail((1800, 1800))
                image.save(output, "JPEG", quality=93, optimize=True)

            return [output]
        except Exception as error:
            last_error = error
            logger.warning("Image download failed: %s | %s", url, error)

    raise ValueError(f"Не удалось скачать главное фото товара: {last_error}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not allowed(update):
        return
    await update.message.reply_text(
        "Команды:\n"
        "/topics — список категорий\n"
        "/register Название — выполнить внутри нужной темы группы\n"
        "/version — проверить версию бота\n\n"
        "После регистрации тем отправь мне ссылку на целый раздел Yupoo."
    )


async def topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not allowed(update):
        return
    await update.message.reply_text("\n".join(sorted(set(CATEGORIES.values()))))


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not allowed(update) or not update.effective_chat:
        return
    if str(update.effective_chat.id) != GROUP_ID:
        await update.message.reply_text("Команду нужно отправить в группе KiryShop.")
        return

    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("Команду нужно отправить внутри конкретной темы.")
        return

    category_name = canonical_category(" ".join(context.args))
    if not category_name:
        await update.message.reply_text("Категория не найдена. Используй /topics.")
        return

    await asyncio.to_thread(save_topic, category_name, thread_id)
    await update.message.reply_text(f"✅ Registered: {category_name}")


async def reset_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not allowed(update):
        return

    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Используй: /reset Protocol Index")
        return

    category_name = canonical_category(raw)
    if not category_name:
        await update.message.reply_text("Категория не найдена. Используй /topics.")
        return

    deleted = await asyncio.to_thread(reset_published_category, category_name)
    await update.message.reply_text(
        f"♻️ Reset: {category_name}\n"
        f"Deleted duplicate records: {deleted}\n"
        "Теперь отправь ссылку на категорию Yupoo ещё раз."
    )


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not allowed(update):
        return
    await update.message.reply_text(f"KiryShop build: {BUILD_VERSION}")


async def publish_album(
    context: ContextTypes.DEFAULT_TYPE,
    album_url: str,
    category_name: str,
    thread_id: int,
) -> None:
    current_id = album_id(album_url)
    if not current_id:
        raise ValueError("Не удалось определить ID альбома.")

    _title, image_urls = await asyncio.to_thread(extract_album, album_url)

    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        files = await asyncio.to_thread(
            download_images,
            image_urls,
            folder,
            album_url,
        )

        if not files:
            raise ValueError("Главное фото товара не найдено.")

        with files[0].open("rb") as photo:
            await context.bot.send_photo(
                chat_id=GROUP_ID,
                message_thread_id=thread_id,
                photo=photo,
                caption="📩 Price and order in private messages",
            )

    # Save as duplicate only after Telegram actually published the photo.
    await asyncio.to_thread(
        remember_album,
        current_id,
        album_url,
        category_name,
    )


async def import_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not allowed(update):
        return

    url = (update.message.text or "").strip()
    if "yupoo.com" not in url.casefold():
        await update.message.reply_text("Отправь ссылку на раздел Yupoo.")
        return

    status = await update.message.reply_text("Ищу товары…")
    try:
        category_name, links = await asyncio.to_thread(all_album_links, url)
        thread_id = await asyncio.to_thread(load_topic, category_name)
        if not thread_id:
            await status.edit_text(
                f"❌ Тема {category_name} не зарегистрирована.\n"
                f"Создай тему и отправь в ней:\n/register {category_name}"
            )
            return

        published = duplicates = errors = skipped_no_product_photos = 0

        for position, album_url in enumerate(links, start=1):
            current_id = album_id(album_url)
            if current_id and await asyncio.to_thread(was_published, current_id):
                duplicates += 1
                continue

            try:
                await publish_album(context, album_url, category_name, thread_id)
                published += 1
            except ValueError as error:
                if "Нет безопасных фото товара" in str(error):
                    skipped_no_product_photos += 1
                    logger.warning("Album skipped (no clean product photos): %s", album_url)
                else:
                    errors += 1
                    logger.exception("Album failed: %s", album_url)
            except requests.HTTPError:
                errors += 1
                logger.exception("Album HTTP error: %s", album_url)
            except Exception:
                errors += 1
                logger.exception("Album failed: %s", album_url)

            if position % 10 == 0:
                try:
                    await status.edit_text(
                        f"{category_name}\n"
                        f"Processed: {position}/{len(links)}\n"
                        f"Published: {published}\n"
                        f"Duplicates: {duplicates}\n"
                        f"Skipped without clean product photos: {skipped_no_product_photos}\n"
                        f"Errors: {errors}"
                    )
                except Exception:
                    pass
            await asyncio.sleep(PAUSE_BETWEEN_PRODUCTS)

        await status.edit_text(
            "✅ Finished\n\n"
            f"Category: {category_name}\n"
            f"Found: {len(links)}\n"
            f"Published: {published}\n"
            f"Duplicates: {duplicates}\n"
            f"Skipped without clean product photos: {skipped_no_product_photos}\n"
            f"Errors: {errors}"
        )
    except Exception as error:
        logger.exception("Import failed")
        await status.edit_text(f"❌ Ошибка: {error}")


def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("topics", topics))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("reset", reset_category))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, import_category))
    logger.info("%s started", BUILD_VERSION)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
