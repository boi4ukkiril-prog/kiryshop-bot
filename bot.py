"""KiryShop: existing Yupoo import + public WeCatalog catalog import.
One main photo, existing caption, existing PostgreSQL topics/history.
WeCatalog uses an ordinary anonymous session obtained from the supplier URL.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import psycopg
import requests
from bs4 import BeautifulSoup
from PIL import Image
from telegram import Update
from telegram.error import RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BUILD_VERSION = "YUPOO-WECATALOG-2026-11-12"
BOT_TOKEN = "".join(os.environ.get("BOT_TOKEN", "").split())
GROUP_ID = "".join(os.environ.get("GROUP_ID", "").split())
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()
# Kept for compatibility. Like the supplied version, no watermark is drawn.
WATERMARK_TEXT = os.getenv("WATERMARK_TEXT", "KiryShop").strip() or "KiryShop"
REQUEST_TIMEOUT = 35
PAUSE_BETWEEN_PRODUCTS = 2.0
DEFAULT_WECATALOG_LIMIT = 5
MAX_IMPORT_LIMIT = 1000
MAX_CATALOG_PAGES = 100
CAPTION = "📩 Price and order in private messages"

CATEGORIES = {
    "down coat": "Down COAT", "shoes": "Shoes", "cap": "Cap", "bag": "Bag",
    "belt/jewellery": "Belt/Jewellery", "belt jewellery": "Belt/Jewellery",
    "belt🧣jewellery": "Belt/Jewellery", "female": "Female", "vv": "VV",
    "ch": "CH", "cd": "CD", "old money": "Old Money", "old money 老钱": "Old Money",
    "hermes": "Hermes", "hermès": "Hermes", "fog essentials": "FOG Essentials",
    "fog": "FOG Essentials", "casablanca": "Casablanca", "bal": "BAL", "prd": "PRD",
    "monc": "MONC", "goose": "Goose", "denim tear": "Denim Tear",
    "gallery dept": "Gallery Dept", "protocol index": "Protocol Index",
    "paly hollywood": "Paly Hollywood", "palm angels/paly hollywood": "Paly Hollywood",
    "hellstar": "HellStar", "sp5der": "SP5der", "erd": "ERD", "acne": "Acne",
    "bbr": "BBR", "ce1": "CE1", "ami & ralph lauren": "Ami & Ralph Lauren",
    "corteiz & godspeed": "Corteiz & GodSpeed", "jacquemus": "Jacquemus",
    "loe": "LOE", "loe & js": "LOE", "guc": "GUC", "fd": "FD", "ow": "OW",
    "tb": "TB", "slp": "SLP", "miu": "Miu", "represent": "Represent",
    "saint m": "SAINT M", "arcteryx": "Arcteryx", "amiri": "Amiri", "vet": "VET",
    "sale": "Sale",
}
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("kiryshop")
# Avoid HTTP client INFO logs containing Telegram bot URLs/tokens.
logging.getLogger("httpx").setLevel(logging.WARNING)


def allowed(update: Update) -> bool:
    return not ALLOWED_USER_ID or bool(update.effective_user and str(update.effective_user.id) == ALLOWED_USER_ID)


def browser_headers(referer=None):
    result = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1", "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        result["Referer"] = referer
    return result


@contextmanager
def db():
    with psycopg.connect(DATABASE_URL) as connection:
        yield connection


def init_db():
    with db() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS topics (category_name TEXT PRIMARY KEY, thread_id BIGINT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS published_albums (album_id TEXT PRIMARY KEY, album_url TEXT NOT NULL, category_name TEXT NOT NULL, published_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")


def save_topic(category, thread):
    with db() as connection:
        connection.execute("INSERT INTO topics(category_name,thread_id) VALUES (%s,%s) ON CONFLICT(category_name) DO UPDATE SET thread_id=EXCLUDED.thread_id", (category, thread))


def load_topic(category):
    with db() as connection:
        row = connection.execute("SELECT thread_id FROM topics WHERE category_name=%s", (category,)).fetchone()
    return int(row[0]) if row else None


def was_published(key):
    with db() as connection:
        return connection.execute("SELECT 1 FROM published_albums WHERE album_id=%s", (key,)).fetchone() is not None


def remember_album(key, url, category):
    with db() as connection:
        connection.execute("INSERT INTO published_albums(album_id,album_url,category_name) VALUES (%s,%s,%s) ON CONFLICT(album_id) DO NOTHING", (key, url, category))


def reset_published_category(category):
    with db() as connection:
        return connection.execute("DELETE FROM published_albums WHERE category_name=%s", (category,)).rowcount


def normalize_label(value):
    value = value.strip().casefold().replace("è", "e").replace("é", "e").replace("ê", "e")
    return " ".join(re.sub(r"[^a-z0-9& ]+", " ", value).split())


def canonical_category(raw):
    aliases = {normalize_label(key): value for key, value in CATEGORIES.items()}
    aliases.update({normalize_label(value): value for value in CATEGORIES.values()})
    aliases.update({"protocol": "Protocol Index", "paly": "Paly Hollywood", "palm angels paly hollywood": "Paly Hollywood"})
    return aliases.get(normalize_label(raw))


def source_for(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        raise ValueError("Нужна обычная ссылка https:// на Yupoo или WeCatalog.")
    host = (parsed.hostname or "").lower()
    for domain, source in (("yupoo.com", "yupoo"), ("wecatalog.cn", "wecatalog")):
        if host == domain or host.endswith("." + domain):
            if parsed.port not in (None, 80, 443):
                break
            return source
    raise ValueError("Поддерживаются только yupoo.com и wecatalog.cn.")


def album_id(url):
    match = re.search(r"/albums/(\d+)", urlparse(url).path)
    return match.group(1) if match else None


def set_page(url, page):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def normalize_album_url(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query.setdefault("uid", ["1"])
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True), fragment=""))


def detect_category(soup, url):
    parts = [url]
    for selector in ("h1", ".category-name", ".showalbumheader__gallerytitle", "title"):
        node = soup.select_one(selector)
        if node:
            parts.append(node.get_text(" ", strip=True))
    combined = " | ".join(parts).casefold()
    for key, value in sorted(CATEGORIES.items(), key=lambda pair: len(pair[0]), reverse=True):
        if len(key) <= 3 and key.isalnum():
            if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", combined):
                return value
        elif key in combined:
            return value
    raise ValueError("Не удалось определить категорию. Используй /import Категория ссылка 5")


def category_page(url):
    response = requests.get(url, headers=browser_headers(), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    links, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        full = normalize_album_url(urljoin(response.url, str(anchor["href"])))
        key = album_id(full)
        if key and key not in seen and source_for(full) == "yupoo":
            seen.add(key)
            links.append(full)
    return soup, links


@dataclass
class Product:
    key: str
    url: str
    title: str = ""
    images: list[str] | None = None
    timestamp: int = 0


@dataclass
class Catalog:
    category: str
    products: list[Product]
    skipped: int = 0
    note: str = ""


def yupoo_catalog(url, category=None, limit=None):
    if album_id(url):
        if not category:
            raise ValueError("Для одного товара укажи тему: /import Shoes ссылка 1")
        return Catalog(category, [Product(album_id(url), url)])
    products, seen = [], set()
    completed = False
    for page in range(1, MAX_CATALOG_PAGES + 1):
        soup, links = category_page(set_page(url, page))
        category = category or detect_category(soup, url)
        fresh = [link for link in links if album_id(link) not in seen]
        if not fresh:
            completed = True
            break
        for link in fresh:
            key = album_id(link)
            seen.add(key)
            products.append(Product(key, link))
        if limit and len(products) >= limit:
            products = products[:limit]
            completed = True
            break
    if not products:
        raise ValueError("В разделе Yupoo не найдено товаров.")
    products.reverse()
    return Catalog(category, products, note="Достигнут предел 100 страниц." if not completed else "")


def extract_album(url):
    response = requests.get(url, headers=browser_headers(url), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    title_node = soup.select_one(".showalbumheader__gallerytitle") or soup.select_one(".showalbumheader__gallerytitle--content") or soup.find("h1") or soup.find("title")
    title = title_node.get_text(" ", strip=True) if title_node else "Product"
    def image_url(tag):
        if not tag:
            return ""
        for attr in ("data-origin-src", "data-src", "data-lazy", "data-original", "src"):
            value = str(tag.get(attr) or "").strip()
            if value and not value.startswith("data:"):
                return urljoin(url, value)
        return ""
    cover = ""
    for selector in (".showalbumheader img", ".showalbumheader__gallerycover img", ".showalbumheader__gallerythumb img", ".showalbumheader__galleryimg img", ".showalbumheader__gallerycover"):
        tag = soup.select_one(selector)
        if tag:
            cover = image_url(tag if tag.name == "img" else tag.find("img"))
            if cover:
                break
    if not cover:
        meta = soup.find("meta", attrs={"property": "og:image"})
        if meta and meta.get("content"):
            cover = urljoin(url, meta["content"])
    images = [cover] if cover else []
    for tag in soup.find_all("img"):
        value = image_url(tag)
        if value and not any(word in value.casefold() for word in ("avatar", "logo", "qrcode", "icon")) and value not in images:
            images.append(value)
    if not images:
        raise ValueError("Главное фото альбома не найдено.")
    return title, images


def wecatalog_item(item, shop_id, base):
    """Skip pinned supplier notices, text/video-only posts and restricted media."""
    if item.get("isTop") or item.get("noForwardDown") or item.get("status", 0) != 0:
        return None
    key = str(item.get("goods_id") or "")
    images = item.get("imgsSrc") or item.get("imgs") or []
    images = list(dict.fromkeys(value for value in images if isinstance(value, str) and value.startswith("https://")))
    if not key or not images:
        return None
    # Prefer a source-provided permalink, preserving opaque IDs and URL fragments.
    link = item.get("link")
    url = urljoin(base, link) if isinstance(link, str) and link else base
    return Product("wc:" + shop_id + ":" + key, url, str(item.get("title") or ""), images, int(item.get("time_stamp") or 0))


def wecatalog_catalog(url, category, limit=DEFAULT_WECATALOG_LIMIT):
    if not category:
        raise ValueError("Укажи тему: /import Shoes https://topdreamer.wecatalog.cn/t/SlobuEh 5")
    with requests.Session() as session:
        session.headers.update(browser_headers())
        # This GET establishes the ordinary visitor cookie required by the site.
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        if source_for(response.url) != "wecatalog":
            raise ValueError("WeCatalog перенаправил ссылку за пределы wecatalog.cn.")
        parsed = urlparse(response.url)
        match = re.search(r"/weshop/(store|goods_list)/([^/?#]+)", parsed.path)
        if not match:
            raise ValueError("Пока поддерживается ссылка на магазин или раздел WeCatalog, не отдельная карточка.")
        shop_id = match.group(2)
        base = f"{parsed.scheme}://{parsed.netloc}"
        session.headers.update({"Referer": response.url, "Content-Type": "application/json;charset=utf-8", "wego-albumid": "", "wego-channel": "", "wego-version": "", "wego-staging": "0", "wego-uuid": "", "x-wg-module": "indsite", "x-wg-language": "zh"})
        query = parse_qs(parsed.query)
        params = {"albumId": shop_id, "startDate": "", "endDate": "", "auditPassword": "", "requestDataType": ""}
        tags = query.get("tagId", [""])[0]
        group = query.get("groupId", [""])[0]
        if group:
            params["tagGroupId"] = group
        # goods_list filters use the form request documented by the site's JS.
        if tags:
            import json
            try:
                tag_list = json.loads(tags)
                if not isinstance(tag_list, list):
                    tag_list = [str(tag_list)]
            except (ValueError, TypeError):
                tag_list = [tags]
            form = {"tagList": json.dumps(tag_list)}
        else:
            form = None
        products, seen, cursors = [], set(), set()
        skipped = 0
        complete = False
        for _page in range(MAX_CATALOG_PAGES):
            headers = {"Content-Type": "application/x-www-form-urlencoded"} if form is not None else None
            result = session.post(base + "/album/personal/all", params=params, data=form, headers=headers, timeout=REQUEST_TIMEOUT)
            result.raise_for_status()
            try:
                payload = result.json()
            except ValueError as error:
                raise ValueError("WeCatalog вернул не JSON; возможно, сайт изменился или запросил вход.") from error
            if payload.get("errcode") != 0:
                raise ValueError(f"WeCatalog не отдал каталог (код {payload.get('errcode')}): {payload.get('errmsg', '')}")
            data = payload.get("result") or {}
            target = data.get("targetAlbum") or {}
            if target.get("attentionVerify") or target.get("noForwardDown") or target.get("urlClose") == 1 or target.get("linkVipMsg"):
                raise ValueError("Поставщик ограничил доступ или скачивание. Нужна открытая ссылка от поставщика.")
            # Canonical supplier ID prevents duplicate imports through alternate share URLs.
            canonical_shop = str(target.get("id") or shop_id)
            items = data.get("items")
            if not isinstance(items, list):
                raise ValueError("WeCatalog изменил формат списка товаров.")
            added = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("goods_id")
                if not raw_id or raw_id in seen:
                    continue
                seen.add(raw_id)
                product = wecatalog_item(item, canonical_shop, response.url)
                if not product:
                    skipped += 1
                    continue
                products.append(product)
                added += 1
                if len(products) >= limit:
                    complete = True
                    break
            if complete:
                break
            pagination = data.get("pagination") or {}
            if not pagination.get("isLoadMore"):
                complete = True
                break
            cursor = str(pagination.get("pageTimestamp") or "")
            if not cursor or cursor in cursors:
                raise ValueError("WeCatalog повторяет страницу каталога; импорт остановлен без публикации.")
            cursors.add(cursor)
            params.update({"slipType": 1, "timestamp": cursor, "requestDataType": "itemName" if pagination.get("dataFromGoodsNumAndMarkCode") else ""})
        if not products:
            raise ValueError("В каталоге не найдено открытых товаров с фото. Закреплённые записи пропускаются.")
        # API is newest first; post the selected batch oldest first.
        products.sort(key=lambda item: item.timestamp)
        return Catalog(category, products, skipped, "Достигнут предел 100 страниц; выборка неполная." if not complete else "")


def collect_catalog(url, category=None, limit=None):
    source = source_for(url)
    if source == "wecatalog":
        return wecatalog_catalog(url, category, limit or DEFAULT_WECATALOG_LIMIT)
    return yupoo_catalog(url, category, limit)


def download_image(urls, folder, referer):
    errors = []
    for url in urls[:5]:
        try:
            if urlparse(url).scheme not in ("https", "http"):
                continue
            response = requests.get(url, headers=browser_headers(referer), timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").casefold()
            if content_type and "image" not in content_type and "octet-stream" not in content_type:
                continue
            with Image.open(BytesIO(response.content)) as original:
                image = original.convert("RGB")
            image.thumbnail((1800, 1800))
            output = folder / "main_photo.jpg"
            image.save(output, "JPEG", quality=93, optimize=True)
            return output
        except Exception as error:
            errors.append(type(error).__name__)
    raise ValueError("Не удалось скачать главное фото: " + ", ".join(errors))


def parse_import(text):
    match = re.search(r"https?://\S+", text)
    if not match:
        raise ValueError("Используй: /import Shoes ссылка 5")
    url = match.group().rstrip(",;)")
    source_for(url)
    before = text[:match.start()].strip()
    before = re.sub(r"^/(?:import|check)(?:@\w+)?\s*", "", before).strip()
    category = canonical_category(before) if before else None
    if before and not category:
        raise ValueError("Категория не найдена. Используй /topics.")
    after = text[match.end():].strip()
    limit = None
    if after:
        if not after.isdigit() or not 1 <= int(after) <= MAX_IMPORT_LIMIT:
            raise ValueError(f"Количество должно быть от 1 до {MAX_IMPORT_LIMIT}.")
        limit = int(after)
    return url, category, limit


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not allowed(update):
        return
    await update.message.reply_text("Команды:\n/topics — категории\n/register Shoes — внутри темы группы\n/check Shoes ссылка 5 — проверить без публикации\n/import Shoes ссылка 5 — опубликовать товары\n/stop — остановить текущий импорт\n/version — версия\n/reset Shoes — забыть историю категории (посты остаются)\n\nYupoo: можно отправить ссылку раздела как раньше.\nWeCatalog: укажи тему через /import; по умолчанию 5 товаров.")


async def topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and allowed(update):
        await update.message.reply_text("\n".join(sorted(set(CATEGORIES.values()))))


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not allowed(update):
        return
    if str(update.effective_chat.id) != GROUP_ID or not update.message.message_thread_id:
        await update.message.reply_text("Отправь команду внутри нужной темы группы KiryShop.")
        return
    category = canonical_category(" ".join(context.args))
    if not category:
        await update.message.reply_text("Категория не найдена. Используй /topics.")
        return
    await asyncio.to_thread(save_topic, category, update.message.message_thread_id)
    await update.message.reply_text(f"✅ Registered: {category}")


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and allowed(update):
        await update.message.reply_text(f"KiryShop build: {BUILD_VERSION}")


async def reset_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not allowed(update):
        return
    if context.application.bot_data.get("import_running"):
        await update.message.reply_text("Сначала останови импорт: /stop")
        return
    category = canonical_category(" ".join(context.args))
    if not category:
        await update.message.reply_text("Используй /reset Shoes. Категории: /topics")
        return
    count = await asyncio.to_thread(reset_published_category, category)
    await update.message.reply_text(f"♻️ {category}: удалено записей истории {count}. Посты остались в группе. Повторный импорт сможет опубликовать их снова.")


async def publish_product(context, product, category, thread_id):
    images = product.images
    if images is None:
        _title, images = await asyncio.to_thread(extract_album, product.url)
    with tempfile.TemporaryDirectory() as temp:
        file = await asyncio.to_thread(download_image, images, Path(temp), product.url)
        for attempt in range(3):
            try:
                with file.open("rb") as photo:
                    await context.bot.send_photo(chat_id=GROUP_ID, message_thread_id=thread_id, photo=photo, caption=CAPTION)
                break
            except RetryAfter as error:
                if attempt == 2:
                    raise
                seconds = error.retry_after
                if hasattr(seconds, "total_seconds"):
                    seconds = seconds.total_seconds()
                await asyncio.sleep(float(seconds) + 1)
        # Telegram accepted the photo. A DB failure must stop the import, not silently
        # continue and later duplicate an unrecorded published product.
        try:
            await asyncio.to_thread(remember_album, product.key, product.url, category)
        except Exception as error:
            raise RuntimeError("Фото опубликовано, но история не сохранилась. Импорт остановлен; проверь БД перед повтором.") from error


async def safe_edit(status, text):
    try:
        await status.edit_text(text)
    except Exception:
        logger.warning("Status update failed")


async def import_worker(update, context, status, url, category, limit, preview):
    published = duplicates = errors = 0
    data = context.application.bot_data
    event = data["import_stop"]
    try:
        catalog = await asyncio.to_thread(collect_catalog, url, category, limit)
        thread = await asyncio.to_thread(load_topic, catalog.category)
        if not thread:
            await safe_edit(status, f"❌ Тема {catalog.category} не зарегистрирована.\nОтправь внутри этой темы группы:\n/register {catalog.category}")
            return
        if event.is_set():
            await safe_edit(status, "⏹ Импорт остановлен без публикации.")
            return
        if preview:
            for product in catalog.products:
                duplicates += int(await asyncio.to_thread(was_published, product.key))
            sample = catalog.products[-1]
            images = sample.images
            if images is None:
                _title, images = await asyncio.to_thread(extract_album, sample.url)
            with tempfile.TemporaryDirectory() as temp:
                await asyncio.to_thread(download_image, images, Path(temp), sample.url)
            await safe_edit(status, f"✅ Проверка без публикации\nТема: {catalog.category}\nТоваров в выборке: {len(catalog.products)}\nУже опубликовано: {duplicates}\nПропущено объявлений/записей без фото: {catalog.skipped}\nГлавное фото одного товара скачивается.\n{catalog.note}\nДля публикации: /import {catalog.category} {url} {limit or DEFAULT_WECATALOG_LIMIT}")
            return
        for position, product in enumerate(catalog.products, 1):
            if event.is_set():
                break
            if await asyncio.to_thread(was_published, product.key):
                duplicates += 1
                continue
            try:
                await publish_product(context, product, catalog.category, thread)
                published += 1
            except RuntimeError:
                raise
            except Exception as error:
                errors += 1
                logger.warning("Product failed (%s): %s", product.key, type(error).__name__)
            if position % 10 == 0:
                await safe_edit(status, f"{catalog.category}: {position}/{len(catalog.products)}\nPublished: {published}\nDuplicates: {duplicates}\nErrors: {errors}")
            try:
                await asyncio.wait_for(event.wait(), timeout=PAUSE_BETWEEN_PRODUCTS)
            except asyncio.TimeoutError:
                pass
        heading = "⏹ Остановлено" if event.is_set() else "✅ Finished"
        await safe_edit(status, f"{heading}\nCategory: {catalog.category}\nSelected: {len(catalog.products)}\nPublished: {published}\nDuplicates: {duplicates}\nSkipped notices/no photos: {catalog.skipped}\nErrors: {errors}\n{catalog.note}")
    except Exception as error:
        logger.error("Import failed (%s)", type(error).__name__)
        await safe_edit(status, f"❌ Ошибка: {str(error)[:600]}\nУспешно опубликовано до ошибки: {published}")
    finally:
        data["import_running"] = False


async def import_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not allowed(update):
        return
    try:
        url, category, limit = parse_import(update.message.text or "")
    except ValueError as error:
        await update.message.reply_text(str(error))
        return
    if source_for(url) == "wecatalog" and not category:
        await update.message.reply_text(f"Укажи тему и количество:\n/import Shoes {url} 5\nПроверка без публикации:\n/check Shoes {url} 5")
        return
    data = context.application.bot_data
    if data.get("import_running"):
        await update.message.reply_text("Уже идёт импорт. Дождись результата или отправь /stop.")
        return
    preview = (update.message.text or "").split()[0].split("@")[0] == "/check"
    data["import_running"] = True
    data["import_stop"] = asyncio.Event()
    try:
        status = await update.message.reply_text("Проверяю каталог…" if preview else "Ищу товары… Для остановки: /stop")
        context.application.create_task(import_worker(update, context, status, url, category, limit, preview), update=update)
    except Exception:
        data["import_running"] = False
        raise


async def stop_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not allowed(update):
        return
    event = context.application.bot_data.get("import_stop")
    if context.application.bot_data.get("import_running") and event:
        event.set()
        await update.message.reply_text("Останавливаю после текущей загрузки каталога или отправки товара. Уже отправленные посты останутся.")
    else:
        await update.message.reply_text("Сейчас импорт не запущен.")


def main():
    missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("GROUP_ID", GROUP_ID), ("DATABASE_URL", DATABASE_URL)) if not value]
    if missing:
        raise SystemExit("Не заданы переменные: " + ", ".join(missing))
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    for name, handler in (("start", start), ("topics", topics), ("register", register), ("reset", reset_category), ("version", version), ("import", import_category), ("check", import_category), ("stop", stop_import)):
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, import_category))
    logger.info("%s started", BUILD_VERSION)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
