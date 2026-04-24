"""
Telegram Bot: Парсер товаров с узбекских магазинов
Парсит все категории: ноутбуки, аксессуары, комплектующие и т.д.
"""

import os
import json
import logging
import requests
import asyncio
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL_ID          = os.environ.get("CHANNEL_ID", "")
POSTED_FILE         = "posted.json"
REQUEST_TIMEOUT     = 15
DELAY_BETWEEN_POSTS = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ─── Все страницы для парсинга ────────────────────────────────────────────────

NOUT_PAGES = [
    "https://nout.uz",
    "https://nout.uz/noutbuki",
    "https://nout.uz/aksessuari",
    "https://nout.uz/sumki-i-ryukzaki",
    "https://nout.uz/myshi",
    "https://nout.uz/klaviatury",
    "https://nout.uz/naushniki",
    "https://nout.uz/veb-kamery",
    "https://nout.uz/colonki",
    "https://nout.uz/komplektuyushchie",
    "https://nout.uz/monitory",
    "https://nout.uz/planshety",
]

PCMARKET_PAGES = [
    "https://pcmarket.uz",
    "https://pcmarket.uz/catalog/noutbuki/",
    "https://pcmarket.uz/catalog/aksessuary/",
    "https://pcmarket.uz/catalog/klaviatury/",
    "https://pcmarket.uz/catalog/myshi/",
    "https://pcmarket.uz/catalog/monitory/",
    "https://pcmarket.uz/catalog/komplektuyushchie/",
    "https://pcmarket.uz/catalog/naushniki-i-garnitury/",
    "https://pcmarket.uz/catalog/veb-kamery/",
    "https://pcmarket.uz/catalog/planshety/",
    "https://pcmarket.uz/catalog/igrovye-pristavki/",
]

NOTEBOOKOFF_PAGES = [
    "https://notebookoff.uz",
    "https://notebookoff.uz/catalog",
    "https://notebookoff.uz/noutbuki",
    "https://notebookoff.uz/aksessuary",
    "https://notebookoff.uz/myshi",
    "https://notebookoff.uz/klaviatury",
    "https://notebookoff.uz/sumki",
    "https://notebookoff.uz/naushniki",
    "https://notebookoff.uz/monitory",
]

# ─── Универсальные селекторы ──────────────────────────────────────────────────

SELECTORS = [
    {
        "card":  ".product-item, .catalog-item, .product-card, .product",
        "name":  ".product-title, .catalog-item__name, .name, h3, h2",
        "price": ".product-price, .price, .catalog-item__price, [class*='price']",
        "img":   "img",
        "link":  "a",
    },
    {
        "card":  "article, .item, li.product, .card",
        "name":  "h1, h2, h3, .title, .name, [class*='name']",
        "price": ".price, [class*='price'], .cost, .amount",
        "img":   "img",
        "link":  "a",
    },
    {
        "card":  "[class*='product'], [class*='item'], [class*='card']",
        "name":  "[class*='title'], [class*='name'], h2, h3",
        "price": "[class*='price'], [class*='cost'], [class*='sum']",
        "img":   "img",
        "link":  "a",
    },
]

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


# ─── Универсальный парсер страницы ───────────────────────────────────────────

def parse_page(url: str, base: str) -> list:
    """Парсит одну страницу и возвращает список товаров."""
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
                name  = safe_text(card.select_one(sel["name"]))
                price = safe_text(card.select_one(sel["price"]))
                img_el = card.select_one(sel["img"])
                img   = safe_attr(img_el, "data-src", base) or safe_attr(img_el, "src", base)
                link_el = card.select_one(sel["link"])
                href  = safe_attr(link_el, "href", base)

                # Пропускаем карточки без названия или ссылки
                if not name or not href:
                    continue
                # Пропускаем слишком короткие названия (навигация, кнопки)
                if len(name) < 5:
                    continue

                products.append({
                    "name":  name,
                    "price": price or "Цена не указана",
                    "img":   img,
                    "url":   href,
                })
            except Exception as e:
                log.debug(f"Ошибка карточки на {url}: {e}")

        if products:
            log.info(f"  ✓ {url} → {len(products)} товаров")
            return products

    log.info(f"  ✗ {url} → товаров не найдено")
    return []


# ─── Парсеры по сайтам ───────────────────────────────────────────────────────

def parse_nout() -> list:
    log.info("=== Парсинг nout.uz ===")
    products = []
    for page_url in NOUT_PAGES:
        items = parse_page(page_url, "https://nout.uz")
        products.extend(items)
    # Убираем дубли по URL
    seen = set()
    unique = []
    for p in products:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    log.info(f"nout.uz: итого {len(unique)} уникальных товаров")
    return unique


def parse_pcmarket() -> list:
    log.info("=== Парсинг pcmarket.uz ===")
    products = []
    for page_url in PCMARKET_PAGES:
        items = parse_page(page_url, "https://pcmarket.uz")
        products.extend(items)
    seen = set()
    unique = []
    for p in products:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    log.info(f"pcmarket.uz: итого {len(unique)} уникальных товаров")
    return unique


def parse_notebookoff() -> list:
    log.info("=== Парсинг notebookoff.uz ===")
    products = []
    for page_url in NOTEBOOKOFF_PAGES:
        items = parse_page(page_url, "https://notebookoff.uz")
        products.extend(items)
    seen = set()
    unique = []
    for p in products:
        if p["url"] not in seen:
            seen.add(p["url"])
            unique.append(p)
    log.info(f"notebookoff.uz: итого {len(unique)} уникальных товаров")
    return unique


# ─── Telegram ────────────────────────────────────────────────────────────────

def format_caption(product: dict) -> str:
    return (
        f"💻 {product['name']}\n\n"
        f"💰 {product['price']}\n\n"
        f"🔗 {product['url']}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📞 +998909161817\n"
        f"💬 @Dmitriy_WhiteFactory"
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
    try:
        if is_valid_image_url(img_url):
            await bot.send_photo(chat_id=CHANNEL_ID, photo=img_url, caption=caption)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption, disable_web_page_preview=False)
        return True
    except TelegramError as e:
        log.error(f"Telegram ошибка '{product['name']}': {e}")
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption)
            return True
        except TelegramError as e2:
            log.error(f"Повторная ошибка: {e2}")
        return False


# ─── Главная функция ─────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        log.error("Не заданы TELEGRAM_TOKEN или CHANNEL_ID!")
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    posted = load_posted()

    log.info("─── Запуск парсинга всех сайтов ───")
    all_products = []

    for parser, label in [
        (parse_nout,        "nout.uz"),
        (parse_pcmarket,    "pcmarket.uz"),
        (parse_notebookoff, "notebookoff.uz"),
    ]:
        try:
            items = parser()
            all_products.extend(items)
        except Exception as e:
            log.error(f"Критическая ошибка парсера {label}: {e}")

    log.info(f"Всего товаров со всех сайтов: {len(all_products)}")
    log.info(f"Уже опубликовано ранее: {len(posted)}")

    published_count = 0
    for product in all_products:
        url = product.get("url", "")
        if not url or url in posted:
            continue

        log.info(f"Публикуем: {product['name'][:60]}")
        success = await post_product(bot, product)

        if success:
            posted.add(url)
            published_count += 1
            save_posted(posted)
            await asyncio.sleep(DELAY_BETWEEN_POSTS)

    log.info(f"─── Готово. Опубликовано новых товаров: {published_count} ───")


if __name__ == "__main__":
    asyncio.run(main())
