"""
Telegram Bot: Парсер товаров с узбекских магазинов ноутбуков
"""

import os
import json
import time
import logging
import requests
import asyncio
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL_ID      = os.environ.get("CHANNEL_ID", "")
POSTED_FILE     = "posted.json"
REQUEST_TIMEOUT = 15
DELAY_BETWEEN_POSTS = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

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
    if val and base_url and val.startswith("/"):
        return base_url.rstrip("/") + val
    return val or ""

def parse_site(base: str, pages: list, selectors: list, label: str) -> list:
    products = []
    for page_url in pages:
        soup = get_soup(page_url)
        if not soup:
            continue
        for sel in selectors:
            cards = soup.select(sel["card"])
            if not cards:
                continue
            log.info(f"{label}: найдено {len(cards)} карточек на {page_url}")
            for card in cards[:20]:
                try:
                    name  = safe_text(card.select_one(sel["name"]))
                    price = safe_text(card.select_one(sel["price"]))
                    img_el = card.select_one(sel["img"])
                    img   = safe_attr(img_el, "data-src", base) or safe_attr(img_el, "src", base)
                    href  = safe_attr(card.select_one(sel["link"]), "href", base)
                    if name and href:
                        products.append({"name": name, "price": price or "Цена не указана", "img": img, "url": href})
                except Exception as e:
                    log.debug(f"{label}: ошибка карточки: {e}")
            if products:
                break
        if products:
            break
    log.info(f"{label}: итого {len(products)} товаров")
    return products

SELECTORS = [
    {"card": ".product-item, .catalog-item, .product-card, .product",
     "name": ".product-title, .catalog-item__name, .name, h3, h2",
     "price": ".product-price, .price, .catalog-item__price, [class*='price']",
     "img": "img", "link": "a"},
    {"card": "article, .item, li.product, .card",
     "name": "h1, h2, h3, .title, .name, [class*='name']",
     "price": ".price, [class*='price'], .cost, .amount",
     "img": "img", "link": "a"},
]

def parse_nout() -> list:
    return parse_site("https://nout.uz",
        ["https://nout.uz/noutbuki", "https://nout.uz/catalog/noutbuki", "https://nout.uz"],
        SELECTORS, "nout.uz")

def parse_pcmarket() -> list:
    return parse_site("https://pcmarket.uz",
        ["https://pcmarket.uz/catalog/noutbuki/", "https://pcmarket.uz/noutbuki/", "https://pcmarket.uz"],
        SELECTORS, "pcmarket.uz")

def parse_notebookoff() -> list:
    return parse_site("https://notebookoff.uz",
        ["https://notebookoff.uz/catalog", "https://notebookoff.uz/noutbuki", "https://notebookoff.uz"],
        SELECTORS, "notebookoff.uz")

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
    return lower.startswith("http") and any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"])

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

async def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        log.error("Не заданы TELEGRAM_TOKEN или CHANNEL_ID!")
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    posted = load_posted()

    log.info("─── Запуск парсинга ───")
    all_products = []

    for parser, label in [(parse_nout, "nout.uz"), (parse_pcmarket, "pcmarket.uz"), (parse_notebookoff, "notebookoff.uz")]:
        try:
            items = parser()
            all_products.extend(items)
        except Exception as e:
            log.error(f"Критическая ошибка парсера {label}: {e}")

    log.info(f"Всего товаров: {len(all_products)}, уже опубликовано: {len(posted)}")

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

    log.info(f"─── Готово. Опубликовано: {published_count} ───")

if __name__ == "__main__":
    asyncio.run(main())
