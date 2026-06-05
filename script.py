"""
Telegram Bot: Парсер товаров с узбекских магазинов
- 2 поста в день в случайное время (по Ташкенту)
- Без цены и ссылки на источник
- Картинка скачивается и отправляется файлом (надёжнее, чем по URL)
- Если новых товаров нет — повторно публикуется давно не выходивший
- Локация магазина прикрепляется к посту (если заданы координаты)
- Опциональный алерт администратору, если источник перестал отдавать товары
"""

import os
import json
import time
import random
import logging
import requests
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def _env_float(name: str, default):
    val = os.environ.get(name, "")
    if val == "":
        return default
    try:
        return float(val)
    except ValueError:
        log.warning(f"Не удалось разобрать {name}={val!r}, использую значение по умолчанию")
        return default


TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL_ID          = os.environ.get("CHANNEL_ID", "")
ADMIN_CHAT_ID       = os.environ.get("ADMIN_CHAT_ID", "")   # необязательно: куда слать алерты

POSTED_FILE         = "posted.json"
STATE_FILE          = "state.json"
REQUEST_TIMEOUT     = 15
REQUEST_RETRIES     = 2          # повторов на неудачный запрос страницы
REQUEST_DELAY_MIN   = 1.0        # пауза между страницами (вежливость к сайтам)
REQUEST_DELAY_MAX   = 2.5
POSTS_PER_DAY       = 2
POSTED_RETENTION_DAYS = 365      # сколько хранить историю публикаций

# Время по Ташкенту
TIMEZONE            = ZoneInfo("Asia/Tashkent")
POST_HOUR_START     = 9       # 9:00 утра
POST_HOUR_END       = 17      # 17:00 вечера

# Магазин (можно переопределить через переменные окружения)
STORE_NAME          = os.environ.get("STORE_NAME", "White Factory")
STORE_ADDRESS       = os.environ.get("STORE_ADDRESS", "Малика, ориентир здание Меркато")
# Координаты с Google Maps. Чтобы выключить локацию — оставьте пусто (STORE_LAT="").
STORE_LATITUDE      = _env_float("STORE_LAT", 41.337737)
STORE_LONGITUDE     = _env_float("STORE_LON", 69.273143)

# Контакты в подписи и кнопка
CONTACT_LINE_1      = os.environ.get("CONTACT_1", "👨‍💼 Дмитрий: +998909161817")
CONTACT_LINE_2      = os.environ.get("CONTACT_2", "👨‍💼 Даниил: +998909018519")
CONTACT_BUTTON_URL  = os.environ.get("CONTACT_BUTTON_URL", "https://t.me/Dmitriy_WhiteFactory")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ─── URL категорий ───────────────────────────────────────────────────────────

NOUT_PAGES = [
    "https://nout.uz",
    "https://nout.uz/brand/asus/",
    "https://nout.uz/brand/hp/",
    "https://nout.uz/brand/lenovo/",
    "https://nout.uz/brand/acer/",
    "https://nout.uz/brand/dell/",
    "https://nout.uz/brand/msi/",
    "https://nout.uz/brand/apple/",
    "https://nout.uz/naushniki",
    "https://nout.uz/monitory",
]

PCMARKET_PAGES = [
    "https://pcmarket.uz",
    "https://pcmarket.uz/cat/noutbuki/",
    "https://pcmarket.uz/cat/noutbuki/igrovye-noutbuki/",
    "https://pcmarket.uz/cat/noutbuki/ofisnye-noutbuki/",
    "https://pcmarket.uz/cat/monitors/",
    "https://pcmarket.uz/cat/klaviatury/",
    "https://pcmarket.uz/cat/myshi/",
    "https://pcmarket.uz/cat/naushniki/",
    "https://pcmarket.uz/cat/kolonki/",
    "https://pcmarket.uz/cat/kompyutery/",
    "https://pcmarket.uz/cat/kompyutery/igrovye/",
    "https://pcmarket.uz/cat/printer/",
    "https://pcmarket.uz/cat/monobloki/",
    "https://pcmarket.uz/cat/fleshki/",
    "https://pcmarket.uz/cat/pereferiya-dlya-pk/cumki-dlya-noutbuka/",
    "https://pcmarket.uz/cat/kovriki-dlya-myshki/",
]

NOTEBOOKOFF_PAGES = [
    "https://notebookoff.uz",
    "https://notebookoff.uz/catalog",
]

SELECTORS = [
    {
        "card":  ".product-item, .catalog-item, .product-card, .product",
        "name":  ".product-title, .catalog-item__name, .name, h3, h2",
        "img":   "img",
        "link":  "a",
    },
    {
        "card":  "article, .item, li.product, .card",
        "name":  "h1, h2, h3, .title, .name, [class*='name']",
        "img":   "img",
        "link":  "a",
    },
    {
        "card":  "[class*='product'], [class*='item'], [class*='card']",
        "name":  "[class*='title'], [class*='name'], h2, h3",
        "img":   "img",
        "link":  "a",
    },
]

# ─── Состояние (слоты публикаций на день) ─────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"date": "", "count": 0, "hours": [], "posted_hours": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("posted_hours", [])
            data.setdefault("hours", [])
            data.setdefault("count", 0)
            data.setdefault("date", "")
            return data
    except Exception:
        return {"date": "", "count": 0, "hours": [], "posted_hours": []}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"Не удалось сохранить state.json: {e}")

def should_post_now():
    now = datetime.now(TIMEZONE)
    today = now.strftime("%Y-%m-%d")
    current_hour = now.hour
    state = load_state()

    if state.get("date") != today:
        hours = sorted(random.sample(range(POST_HOUR_START, POST_HOUR_END + 1), POSTS_PER_DAY))
        state = {"date": today, "count": 0, "hours": hours, "posted_hours": []}
        save_state(state)
        log.info(f"Новый день {today}. Часы для постов: {hours} (Ташкент)")

    planned = state.get("hours", [])
    posted_hours = state.get("posted_hours", [])

    if state.get("count", 0) >= POSTS_PER_DAY:
        log.info(f"Сегодня уже опубликовано {POSTS_PER_DAY}. Пропускаем.")
        return False, -1

    due = [h for h in planned if h <= current_hour and h not in posted_hours]

    if due:
        slot = min(due)
        log.info(f"Час {current_hour} (Ташкент). Закрываем слот {slot}. План: {planned}, сделано: {posted_hours}")
        return True, slot

    log.info(f"Час {current_hour} (Ташкент). Рано. План: {planned}, сделано: {posted_hours}")
    return False, -1

def mark_slot_posted(slot_hour: int) -> None:
    state = load_state()
    state["count"] = state.get("count", 0) + 1
    posted_hours = state.get("posted_hours", [])
    if slot_hour not in posted_hours:
        posted_hours.append(slot_hour)
    state["posted_hours"] = sorted(posted_hours)
    save_state(state)

# ─── История публикаций (url -> дата последней публикации) ────────────────────

def _parse_ts(ts) -> datetime:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return dt
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=TIMEZONE)

def load_posted() -> dict:
    """Возвращает словарь {url: iso_дата}. Поддерживает старый формат (список url)."""
    if not os.path.exists(POSTED_FILE):
        return {}
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.warning(f"Не удалось прочитать {POSTED_FILE}: {e}")
        return {}

    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        # миграция со старого формата: считаем, что всё опубликовано давно
        old = (datetime.now(TIMEZONE) - timedelta(days=1)).isoformat()
        return {url: old for url in data}
    return {}

def save_posted(posted: dict) -> None:
    # чистим записи старше POSTED_RETENTION_DAYS, чтобы файл не рос бесконечно
    cutoff = datetime.now(TIMEZONE) - timedelta(days=POSTED_RETENTION_DAYS)
    cleaned = {url: ts for url, ts in posted.items() if _parse_ts(ts) >= cutoff}
    try:
        with open(POSTED_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"Не удалось сохранить {POSTED_FILE}: {e}")

# ─── Утилиты ─────────────────────────────────────────────────────────────────

def get_soup(url: str):
    last_err = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            last_err = e
            if attempt < REQUEST_RETRIES:
                log.warning(f"Запрос {url} не удался (попытка {attempt + 1}): {e}. Повтор…")
                time.sleep(1.5 * (attempt + 1))
    log.error(f"Запрос {url} окончательно не удался: {last_err}")
    return None

def safe_text(el) -> str:
    return el.get_text(strip=True) if el else ""

def safe_attr(el, attr: str, base_url: str = "") -> str:
    if not el:
        return ""
    val = el.get(attr, "")
    if val and base_url and str(val).startswith("/"):
        return base_url.rstrip("/") + val
    return val or ""

def normalize_name(name: str) -> str:
    return " ".join(name.lower().split())

# ─── Скачивание картинки ──────────────────────────────────────────────────────

def _looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":                       # JPEG
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":                   # PNG
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):                 # GIF
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":      # WEBP
        return True
    return False

def fetch_image(url: str):
    """Скачивает картинку и возвращает байты, либо None если не вышло/не картинка."""
    if not url or not str(url).lower().startswith("http"):
        return None
    if str(url).lower().split("?")[0].endswith(".svg"):
        return None  # Telegram не показывает SVG как фото
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.content
    except requests.RequestException as e:
        log.warning(f"Не удалось скачать картинку {url}: {e}")
        return None

    # Telegram: фото до 10 МБ; слишком маленькое — вероятно заглушка/иконка
    if not (2048 <= len(data) <= 10 * 1024 * 1024):
        log.debug(f"Картинка отброшена по размеру ({len(data)} б): {url}")
        return None
    if not _looks_like_image(data):
        log.debug(f"Не похоже на изображение: {url}")
        return None
    return data

# ─── Парсер ──────────────────────────────────────────────────────────────────

def parse_page(url: str, base: str) -> list:
    soup = get_soup(url)
    if not soup:
        return []

    for sel in SELECTORS:
        cards = soup.select(sel["card"])
        if not cards:
            continue

        products = []
        for card in cards[:30]:
            try:
                name   = safe_text(card.select_one(sel["name"]))
                img_el = card.select_one(sel["img"])
                img    = safe_attr(img_el, "data-src", base) or safe_attr(img_el, "src", base)
                href   = safe_attr(card.select_one(sel["link"]), "href", base)

                if not name or not href or len(name) < 5:
                    continue
                if href in [base, base + "/", "https://nout.uz/", "https://pcmarket.uz/", "https://notebookoff.uz/"]:
                    continue

                products.append({"name": name, "img": img, "url": href})
            except Exception as e:
                log.debug(f"Ошибка карточки на {url}: {e}")

        if products:
            log.info(f"  ✓ {url} → {len(products)} товаров")
            return products

    log.info(f"  ✗ {url} → товаров не найдено")
    return []

def parse_all():
    """Возвращает (список_товаров, список_источников_без_товаров)."""
    all_products = []
    seen_urls = set()
    seen_names = set()
    failed_sources = []

    sources = [
        ("https://nout.uz",        NOUT_PAGES,        "nout.uz"),
        ("https://pcmarket.uz",    PCMARKET_PAGES,    "pcmarket.uz"),
        ("https://notebookoff.uz", NOTEBOOKOFF_PAGES, "notebookoff.uz"),
    ]

    for base, pages, label in sources:
        log.info(f"=== Парсинг {label} ===")
        before = len(all_products)
        for page_url in pages:
            try:
                items = parse_page(page_url, base)
                for item in items:
                    nname = normalize_name(item["name"])
                    if item["url"] in seen_urls or nname in seen_names:
                        continue  # дубль по ссылке или по названию
                    seen_urls.add(item["url"])
                    seen_names.add(nname)
                    all_products.append(item)
            except Exception as e:
                log.error(f"Ошибка {label} на {page_url}: {e}")
            time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

        added = len(all_products) - before
        log.info(f"{label}: добавлено {added} уникальных товаров")
        if added == 0:
            failed_sources.append(label)

    return all_products, failed_sources

# ─── Выбор товара ─────────────────────────────────────────────────────────────

def pick_product(all_products: list, posted: dict):
    """Сначала ни разу не публиковавшиеся; если таких нет — самый давний."""
    if not all_products:
        return None, ""

    fresh = [p for p in all_products if p["url"] not in posted]
    if fresh:
        return random.choice(fresh), "новый"

    oldest = min(all_products, key=lambda p: _parse_ts(posted.get(p["url"])))
    return oldest, "повтор"

# ─── Telegram ────────────────────────────────────────────────────────────────

def format_caption(product: dict) -> str:
    return (
        f"💻 <b>{product['name']}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏪 <b>{STORE_NAME}</b>\n"
        f"📍 {STORE_ADDRESS}\n\n"
        f"{CONTACT_LINE_1}\n"
        f"{CONTACT_LINE_2}"
    )

async def post_product(bot: Bot, product: dict) -> bool:
    caption = format_caption(product)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать нам", url=CONTACT_BUTTON_URL)]
    ])
    image = fetch_image(product.get("img", ""))
    sent = False

    # 1. Фото товара с описанием (с откатом на текст, если фото нет/не отправилось)
    try:
        if image:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        sent = True
    except TelegramError as e:
        log.error(f"Telegram ошибка с фото '{product['name']}': {e}")
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            sent = True
        except TelegramError as e2:
            log.error(f"Повторная ошибка: {e2}")
            return False

    # 2. Карта с локацией магазина (если координаты заданы)
    if sent and STORE_LATITUDE is not None and STORE_LONGITUDE is not None:
        try:
            await bot.send_venue(
                chat_id=CHANNEL_ID,
                latitude=STORE_LATITUDE,
                longitude=STORE_LONGITUDE,
                title=STORE_NAME,
                address=STORE_ADDRESS,
            )
            log.info("Локация магазина прикреплена")
        except TelegramError as ve:
            log.warning(f"Не удалось отправить локацию: {ve}")

    return sent

async def notify_admin(bot: Bot, text: str) -> None:
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except TelegramError as e:
        log.warning(f"Не удалось отправить алерт администратору: {e}")

# ─── Главная функция ─────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        log.error("Не заданы TELEGRAM_TOKEN или CHANNEL_ID!")
        return

    can_post, slot = should_post_now()
    if not can_post:
        return

    posted = load_posted()

    all_products, failed_sources = parse_all()
    log.info(f"Всего товаров: {len(all_products)}, в истории публикаций: {len(posted)}")

    bot = Bot(token=TELEGRAM_TOKEN)

    # Предупреждаем администратора, если какой-то сайт перестал отдавать товары
    if failed_sources:
        msg = ("⚠️ Источники без товаров: " + ", ".join(failed_sources) +
               "\nВозможно, изменилась вёрстка сайта или сайт недоступен.")
        log.warning(msg)
        await notify_admin(bot, msg)

    product, kind = pick_product(all_products, posted)
    if product is None:
        log.warning("Парсинг ничего не вернул. Слот не закрываем.")
        return

    log.info(f"Публикуем ({kind}): {product['name'][:60]}")

    success = await post_product(bot, product)
    if success:
        posted[product["url"]] = datetime.now(TIMEZONE).isoformat()
        save_posted(posted)
        mark_slot_posted(slot)
        log.info(f"─── Пост опубликован ({kind}), слот {slot} (Ташкент) закрыт ───")
    else:
        log.error("Не удалось опубликовать. Слот остаётся открытым.")

if __name__ == "__main__":
    asyncio.run(main())
