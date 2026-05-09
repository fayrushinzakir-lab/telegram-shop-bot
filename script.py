"""
Telegram Bot: Парсер товаров с узбекских магазинов
- 2 поста в день в случайное время
- Без цены и ссылки на источник
- Время по Ташкенту
- Локация магазина прикрепляется к посту (если заданы координаты)
"""

import os
import json
import logging
import requests
import asyncio
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL_ID          = os.environ.get("CHANNEL_ID", "")
POSTED_FILE         = "posted.json"
STATE_FILE          = "state.json"
REQUEST_TIMEOUT     = 15
POSTS_PER_DAY       = 2

# Время по Ташкенту
TIMEZONE            = ZoneInfo("Asia/Tashkent")
POST_HOUR_START     = 9       # 9:00 утра
POST_HOUR_END       = 17      # 17:00 вечера

# Локация магазина — заполнить координатами с Google Maps
STORE_NAME          = "White Factory"
STORE_ADDRESS       = "Малика, ориентир здание Меркато"
STORE_LATITUDE      = None    # например, 41.311081
STORE_LONGITUDE     = None    # например, 69.240562

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

# ─── Состояние ───────────────────────────────────────────────────────────────

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

# ─── Утилиты ─────────────────────────────────────────────────────────────────

def load_posted() -> set:
    if not os.path.exists(POSTED_FILE):
        return set()
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError) as e:
        log.warning(f"Не удалось прочитать {POSTED_FILE}: {e}")
        return set()

def save_posted(posted: set) -> None:
    try:
        with open(POSTED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(posted), f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"Не удалось сохранить {POSTED_FILE}: {e}")

def get_soup(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.error(f"Ошибка при запросе {url}: {e}")
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

def parse_all() -> list:
    all_products = []
    sources = [
        ("https://nout.uz",        NOUT_PAGES,        "nout.uz"),
        ("https://pcmarket.uz",    PCMARKET_PAGES,    "pcmarket.uz"),
        ("https://notebookoff.uz", NOTEBOOKOFF_PAGES, "notebookoff.uz"),
    ]

    for base, pages, label in sources:
        log.info(f"=== Парсинг {label} ===")
        seen_urls = set()
        for page_url in pages:
            try:
                items = parse_page(page_url, base)
                for item in items:
                    if item["url"] not in seen_urls:
                        seen_urls.add(item["url"])
                        all_products.append(item)
            except Exception as e:
                log.error(f"Ошибка {label} на {page_url}: {e}")
        log.info(f"{label}: итого {len(seen_urls)} уникальных товаров")

    return all_products

# ─── Telegram ────────────────────────────────────────────────────────────────

def format_caption(product: dict) -> str:
    return (
        f"💻 <b>{product['name']}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏪 <b>{STORE_NAME}</b>\n"
        f"📍 {STORE_ADDRESS}\n\n"
        f"👨‍💼 Дмитрий: +998909161817\n"
        f"👨‍💼 Данил: +998909018519"
    )

def is_valid_image_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return lower.startswith("http") and any(
        ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]
    )

async def post_product(bot: Bot, product: dict) -> bool:
    caption = format_caption(product)
    img_url = product.get("img", "")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать нам", url="https://t.me/Dmitriy_WhiteFactory")]
    ])
    try:
        # 1. Фото товара с описанием
        if is_valid_image_url(img_url):
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=img_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )

        # 2. Карта с локацией магазина (если координаты заданы)
        if STORE_LATITUDE is not None and STORE_LONGITUDE is not None:
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

        return True
    except TelegramError as e:
        log.error(f"Telegram ошибка '{product['name']}': {e}")
        try:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return True
        except TelegramError as e2:
            log.error(f"Повторная ошибка: {e2}")
        return False

# ─── Главная функция ─────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        log.error("Не заданы TELEGRAM_TOKEN или CHANNEL_ID!")
        return

    can_post, slot = should_post_now()
    if not can_post:
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    posted = load_posted()

    all_products = parse_all()
    log.info(f"Всего товаров: {len(all_products)}, уже опубликовано: {len(posted)}")

    new_products = [p for p in all_products if p.get("url") and p["url"] not in posted]
    log.info(f"Новых товаров: {len(new_products)}")

    if not new_products:
        log.warning("Нет новых товаров. Слот не закрываем.")
        return

    product = random.choice(new_products)
    log.info(f"Публикуем: {product['name'][:60]}")

    success = await post_product(bot, product)
    if success:
        posted.add(product["url"])
        save_posted(posted)
        mark_slot_posted(slot)
        log.info(f"─── Пост опубликован, слот {slot} (Ташкент) закрыт ───")
    else:
        log.error("Не удалось опубликовать. Слот остаётся открытым.")

if __name__ == "__main__":
    asyncio.run(main())
