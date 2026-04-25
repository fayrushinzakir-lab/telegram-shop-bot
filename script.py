"""
Telegram Bot: Парсер товаров с узбекских магазинов
- 2 поста в день в случайное время
- Без цены и ссылки на источник
"""

import os
import json
import logging
import requests
import asyncio
import random
from datetime import datetime
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ─── Реальные URL категорий (проверены) ──────────────────────────────────────

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

# ─── Состояние ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"date": "", "count": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": "", "count": 0}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"Не удалось сохранить state.json: {e}")

def should_post_now() -> bool:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    current_hour = datetime.utcnow().hour
    state = load_state()

    if state.get("date") != today:
        hours = random.sample(range(8, 22), POSTS_PER_DAY)
        state = {"date": today, "count": 0, "hours": sorted(hours)}
        save_state(state)
        log.info(f"Новый день. Часы для постов: {state['hours']} UTC")

    if state.get("count", 0) >= POSTS_PER_DAY:
        log.info(f"Сегодня уже опубликовано {POSTS_PER_DAY} поста. Пропускаем.")
        return False

    planned_hours = state.get("hours", [])
    if current_hour in planned_hours:
        log.info(f"Час {current_hour} UTC — время постить!")
        return True

    log.info(f"Час {current_hour} UTC — не время. Запланировано: {planned_hours} UTC")
    return False

def increment_post_count() -> None:
    state = load_state()
    state["count"] = state.get("count", 0) + 1
    save_state(state)

# ─── Утилиты ──────────────────────────────────────────────────────────────────

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

# ─── Парсер ───────────────────────────────────────────────────────────────────

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
                # Пропускаем навигационные ссылки
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
        f"🏪 <b>White Factory</b>\n"
        f"📍 Малика, здание Меркато\n\n"
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

    # Проверяем — время ли постить
    if not should_post_now():
       return

    bot = Bot(token=TELEGRAM_TOKEN)
    posted = load_posted()

    all_products = parse_all()
    log.info(f"Всего товаров: {len(all_products)}, уже опубликовано: {len(posted)}")

    new_products = [p for p in all_products if p.get("url") and p["url"] not in posted]
    log.info(f"Новых товаров: {len(new_products)}")

    if not new_products:
        log.info("Нет новых товаров.")
        return

    product = random.choice(new_products)
    log.info(f"Публикуем: {product['name'][:60]}")

    success = await post_product(bot, product)
    if success:
        posted.add(product["url"])
        save_posted(posted)
        increment_post_count()
        log.info("─── Пост опубликован успешно ───")

if __name__ == "__main__":
    asyncio.run(main())
