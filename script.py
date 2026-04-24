"""
Telegram Bot: Парсер товаров с узбекских магазинов ноутбуков
Публикует новые товары в Telegram канал каждый час через GitHub Actions
"""

import os
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.error import TelegramError

# ─── Настройка логирования ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Конфиг ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHANNEL_ID     = os.environ.get("CHANNEL_ID", "")
POSTED_FILE    = "posted.json"
REQUEST_TIMEOUT = 15
DELAY_BETWEEN_POSTS = 3  # секунд между постами (анти-флуд)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def load_posted() -> set:
    """Загружает уже опубликованные ссылки из posted.json."""
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
    """Сохраняет список опубликованных ссылок в posted.json."""
    try:
        with open(POSTED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(posted), f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"Не удалось сохранить {POSTED_FILE}: {e}")


def get_soup(url: str) -> BeautifulSoup | None:
    """Делает GET-запрос и возвращает BeautifulSoup объект или None при ошибке."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as e:
        log.error(f"Ошибка при запросе {url}: {e}")
        return None


def safe_text(el) -> str:
    """Безопасно извлекает текст из тега."""
    return el.get_text(strip=True) if el else ""


def safe_attr(el, attr: str, base_url: str = "") -> str:
    """Безопасно извлекает атрибут из тега, при необходимости добавляет base_url."""
    if not el:
        return ""
    val = el.get(attr, "")
    if val and base_url and val.startswith("/"):
        return base_url.rstrip("/") + val
    return val or ""


# ─── Парсеры ──────────────────────────────────────────────────────────────────

def parse_nout() -> list[dict]:
    """
    Парсит nout.uz — магазин ноутбуков.
    Пробует несколько наборов селекторов для устойчивости.
    """
    BASE = "https://nout.uz"
    PAGES = [
        f"{BASE}/noutbuki",
        f"{BASE}/catalog/noutbuki",
        f"{BASE}",
    ]
    products = []

    for page_url in PAGES:
        soup = get_soup(page_url)
        if not soup:
            continue

        # Наборы селекторов (от конкретных к общим)
        selectors = [
            {
                "card":  ".product-item, .catalog-item, .product-card",
                "name":  ".product-title, .catalog-item__name, h3, h2",
                "price": ".product-price, .price, .catalog-item__price",
                "img":   "img",
                "link":  "a",
            },
            {
                "card":  "article, .item, li.product",
                "name":  "h1, h2, h3, .title, .name",
                "price": ".price, [class*='price']",
                "img":   "img",
                "link":  "a",
            },
        ]

        for sel in selectors:
            cards = soup.select(sel["card"])
            if not cards:
                continue

            log.info(f"nout.uz: найдено {len(cards)} карточек на {page_url}")
            for card in cards[:20]:  # ограничение на итерацию
                try:
                    name_el  = card.select_one(sel["name"])
                    price_el = card.select_one(sel["price"])
                    img_el   = card.select_one(sel["img"])
                    link_el  = card.select_one(sel["link"])

                    name  = safe_text(name_el)
                    price = safe_text(price_el)
                    img   = (
                        safe_attr(img_el, "data-src", BASE) or
                        safe_attr(img_el, "src", BASE)
                    )
                    href  = safe_attr(link_el, "href", BASE)

                    if name and href:
                        products.append({
                            "name":  name,
                            "price": price or "Цена не указана",
                            "img":   img,
                            "url":   href,
                        })
                except Exception as e:
                    log.debug(f"nout.uz: ошибка парсинга карточки: {e}")

            if products:
                break  # нашли товары — не пробуем следующий набор

        if products:
            break  # нашли товары — не пробуем следующую страницу

    log.info(f"nout.uz: итого {len(products)} товаров")
    return products


def parse_pcmarket() -> list[dict]:
    """
    Парсит pcmarket.uz — компьютерный магазин.
    """
    BASE = "https://pcmarket.uz"
    PAGES = [
        f"{BASE}/catalog/noutbuki/",
        f"{BASE}/noutbuki/",
        f"{BASE}",
    ]
    products = []

    for page_url in PAGES:
        soup = get_soup(page_url)
        if not soup:
            continue

        selectors = [
            {
                "card":  ".product-item, .catalog-section-item, .product",
                "name":  ".product-item-title, .name, h3, h2",
                "price": ".price, .product-item-price, [class*='price']",
                "img":   "img",
                "link":  "a",
            },
            {
                "card":  "article, .item, .card",
                "name":  "h1, h2, h3, .title",
                "price": "[class*='price'], .cost",
                "img":   "img",
                "link":  "a",
            },
        ]

        for sel in selectors:
            cards = soup.select(sel["card"])
            if not cards:
                continue

            log.info(f"pcmarket.uz: найдено {len(cards)} карточек на {page_url}")
            for card in cards[:20]:
                try:
                    name_el  = card.select_one(sel["name"])
                    price_el = card.select_one(sel["price"])
                    img_el   = card.select_one(sel["img"])
                    link_el  = card.select_one(sel["link"])

                    name  = safe_text(name_el)
                    price = safe_text(price_el)
                    img   = (
                        safe_attr(img_el, "data-src", BASE) or
                        safe_attr(img_el, "src", BASE)
                    )
                    href  = safe_attr(link_el, "href", BASE)

                    if name and href:
                        products.append({
                            "name":  name,
                            "price": price or "Цена не указана",
                            "img":   img,
                            "url":   href,
                        })
                except Exception as e:
                    log.debug(f"pcmarket.uz: ошибка парсинга карточки: {e}")

            if products:
                break

        if products:
            break

    log.info(f"pcmarket.uz: итого {len(products)} товаров")
    return products


def parse_notebookoff() -> list[dict]:
    """
    Парсит notebookoff.uz — магазин ноутбуков.
    """
    BASE = "https://notebookoff.uz"
    PAGES = [
        f"{BASE}/catalog",
        f"{BASE}/noutbuki",
        f"{BASE}",
    ]
    products = []

    for page_url in PAGES:
        soup = get_soup(page_url)
        if not soup:
            continue

        selectors = [
            {
                "card":  ".product, .product-item, .catalog-item",
                "name":  ".product-title, .name, h3, h2",
                "price": ".price, .product-price, [class*='price']",
                "img":   "img",
                "link":  "a",
            },
            {
                "card":  "article, li, .card, .item",
                "name":  "h1, h2, h3, .title, [class*='name']",
                "price": "[class*='price'], .cost, .amount",
                "img":   "img",
                "link":  "a",
            },
        ]

        for sel in selectors:
            cards = soup.select(sel["card"])
            if not cards:
                continue

            log.info(f"notebookoff.uz: найдено {len(cards)} карточек на {page_url}")
            for card in cards[:20]:
                try:
                    name_el  = card.select_one(sel["name"])
                    price_el = card.select_one(sel["price"])
                    img_el   = card.select_one(sel["img"])
                    link_el  = card.select_one(sel["link"])

                    name  = safe_text(name_el)
                    price = safe_text(price_el)
                    img   = (
                        safe_attr(img_el, "data-src", BASE) or
                        safe_attr(img_el, "src", BASE)
                    )
                    href  = safe_attr(link_el, "href", BASE)

                    if name and href:
                        products.append({
                            "name":  name,
                            "price": price or "Цена не указана",
                            "img":   img,
                            "url":   href,
                        })
                except Exception as e:
                    log.debug(f"notebookoff.uz: ошибка парсинга карточки: {e}")

            if products:
                break

        if products:
            break

    log.info(f"notebookoff.uz: итого {len(products)} товаров")
    return products


# ─── Telegram публикация ──────────────────────────────────────────────────────

def format_caption(product: dict) -> str:
    """Форматирует текст поста."""
    return (
        f"💻 {product['name']}\n\n"
        f"💰 {product['price']}\n\n"
        f"🔗 {product['url']}"
    )


def is_valid_image_url(url: str) -> bool:
    """Проверяет, что URL ведёт на изображение."""
    if not url:
        return False
    lower = url.lower()
    return (
        lower.startswith("http") and
        any(ext in lower for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"])
    )


def post_product(bot: Bot, product: dict) -> bool:
    """
    Публикует один товар в канал.
    Если есть фото — send_photo, иначе — send_message.
    Возвращает True при успехе.
    """
    caption = format_caption(product)
    img_url = product.get("img", "")

    try:
        if is_valid_image_url(img_url):
            bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=img_url,
                caption=caption,
                parse_mode="HTML"
            )
        else:
            bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=False
            )
        return True

    except TelegramError as e:
        log.error(f"Telegram ошибка при публикации '{product['name']}': {e}")
        # Если фото не загрузилось — пробуем без фото
        if img_url and "photo" in str(e).lower():
            try:
                bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption,
                    parse_mode="HTML"
                )
                return True
            except TelegramError as e2:
                log.error(f"Повторная ошибка Telegram: {e2}")
        return False


# ─── Основная логика ──────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        log.error("Не заданы TELEGRAM_TOKEN или CHANNEL_ID!")
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    posted = load_posted()

    log.info("─── Запуск парсинга ───")
    all_products = []

    # Собираем товары со всех сайтов
    for parser, label in [
        (parse_nout,        "nout.uz"),
        (parse_pcmarket,    "pcmarket.uz"),
        (parse_notebookoff, "notebookoff.uz"),
    ]:
        try:
            items = parser()
            log.info(f"{label}: получено {len(items)} товаров")
            all_products.extend(items)
        except Exception as e:
            log.error(f"Критическая ошибка парсера {label}: {e}")

    log.info(f"Всего товаров: {len(all_products)}, уже опубликовано: {len(posted)}")

    # Публикуем только новые
    published_count = 0
    for product in all_products:
        url = product.get("url", "")
        if not url or url in posted:
            continue

        log.info(f"Публикуем: {product['name'][:60]}")
        success = post_product(bot, product)

        if success:
            posted.add(url)
            published_count += 1
            save_posted(posted)  # сохраняем после каждого поста
            time.sleep(DELAY_BETWEEN_POSTS)

    log.info(f"─── Готово. Опубликовано новых товаров: {published_count} ───")


if __name__ == "__main__":
    main()
