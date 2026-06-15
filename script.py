"""
Telegram Bot: Парсер товаров и автопостинг в канал
- 2 поста с товарами в день в случайное время (по Ташкенту)
- Картинка товара скачивается и отправляется файлом
- Если новых товаров нет — повторно публикуется давно не выходивший
- Кнопка «Как доехать» с локацией магазина в каждом товарном посте

Если бот запускается в GitHub Actions: файлы posted.json и state.json
должны коммититься обратно в репозиторий после запуска,
иначе история теряется. В workflow добавьте:
    concurrency:
      group: telegram-bot
      cancel-in-progress: false
чтобы два запуска не перетирали состояние друг друга.
"""

import os
import json
import html
import time
import random
import re
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
    """Координата из окружения.
    - переменная не задана          → default
    - пусто / off / none / disable  → None (локация выключена)
    - число                         → число"""
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    if val == "" or val.lower() in ("off", "none", "disable", "disabled"):
        return None
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
MAX_SLOT_DELAY_H    = 3          # макс. опоздание слота (часов); старше — пропускаем
IMG_MIN_BYTES       = 2048
IMG_MAX_BYTES       = 10 * 1024 * 1024
NAME_MAX_LEN        = 300        # обрезка названия (лимит подписи к фото — 1024)

# Время по Ташкенту
TIMEZONE            = ZoneInfo("Asia/Tashkent")
POST_HOUR_START     = 9       # 9:00 утра
POST_HOUR_END       = 17      # 17:00 вечера

# Магазин (можно переопределить через переменные окружения)
STORE_NAME          = os.environ.get("STORE_NAME", "White Factory")
STORE_ADDRESS       = os.environ.get("STORE_ADDRESS", "Малика, ориентир здание Меркато")
# Координаты с Google Maps. Чтобы выключить кнопку локации: STORE_LAT=off
STORE_LATITUDE      = _env_float("STORE_LAT", 41.337737)
STORE_LONGITUDE     = _env_float("STORE_LON", 69.273143)
# Своя ссылка на карты (карточка организации в Яндекс.Картах).
# Пусто — соберётся автоматически из координат.
MAPS_URL            = os.environ.get("MAPS_URL", "https://yandex.uz/maps/org/white_factory/170878566865/?ll=69.273143%2C41.337737&z=16").strip()

# Контакты в подписи и кнопка
CONTACT_LINE_1      = os.environ.get("CONTACT_1", "👨‍💼 Дмитрий: +998909161817")
CONTACT_LINE_2      = os.environ.get("CONTACT_2", "👨‍💼 Даниил: +998909018519")
CONTACT_BUTTON_URL  = os.environ.get("CONTACT_BUTTON_URL", "https://t.me/Dmitriy_WhiteFactory")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Одна сессия на весь скрипт: переиспользует соединения, быстрее и вежливее.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

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
        "price": ".price, .product-price, [class*='price']",
        "img":   "img",
        "link":  "a",
    },
    {
        "card":  "article, .item, li.product, .card",
        "name":  "h1, h2, h3, .title, .name, [class*='name']",
        "price": "[class*='price'], .cost, [class*='cost']",
        "img":   "img",
        "link":  "a",
    },
    {
        "card":  "[class*='product'], [class*='item'], [class*='card']",
        "name":  "[class*='title'], [class*='name'], h2, h3",
        "price": "[class*='price'], [class*='cost']",
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

    # Разбираем слоты: вовремя / просрочены (workflow долго лежал) / ещё рано.
    due, expired = [], []
    for h in planned:
        if h in posted_hours or h > current_hour:
            continue
        if current_hour - h <= MAX_SLOT_DELAY_H:
            due.append(h)
        else:
            expired.append(h)

    if expired:
        # Просроченные слоты закрываем без публикации, чтобы посты не
        # вылетали пачкой поздно вечером после простоя.
        posted_hours = sorted(set(posted_hours) | set(expired))
        state["posted_hours"] = posted_hours
        save_state(state)
        log.warning(f"Слоты {expired} просрочены более чем на {MAX_SLOT_DELAY_H} ч — пропущены.")

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

# ─── История публикаций товаров (url -> дата) ─────────────────────────────────

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
        old = (datetime.now(TIMEZONE) - timedelta(days=1)).isoformat()
        return {url: old for url in data}
    return {}

def save_posted(posted: dict) -> None:
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
            resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
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

def _absolutize(val: str, base: str) -> str:
    if not val:
        return ""
    val = str(val).strip()
    if val.startswith("//"):                 # протокол-относительный URL
        return "https:" + val
    if val.startswith("/") and base:
        return base.rstrip("/") + val
    return val

def safe_attr(el, attr: str, base_url: str = "") -> str:
    if not el:
        return ""
    return _absolutize(el.get(attr, ""), base_url)

def extract_img_url(el, base: str) -> str:
    """Берёт URL картинки с учётом ленивой загрузки: data-src, data-lazy-src,
    data-original, srcset и только потом src (там часто лежит заглушка)."""
    if not el:
        return ""
    for attr in ("data-src", "data-lazy-src", "data-original"):
        val = safe_attr(el, attr, base)
        if val and not val.startswith("data:"):
            return val
    srcset = el.get("srcset") or el.get("data-srcset") or ""
    if srcset:
        first = srcset.split(",")[0].strip().split()[0]
        first = _absolutize(first, base)
        if first and not first.startswith("data:"):
            return first
    val = safe_attr(el, "src", base)
    return "" if val.startswith("data:") else val

def normalize_name(name: str) -> str:
    return " ".join(name.lower().split())

def clean_price(text: str) -> str:
    """Чистит текст цены; возвращает пустую строку, если цифр нет."""
    text = " ".join(text.split())
    if not any(ch.isdigit() for ch in text):
        return ""
    return text[:60]

# ─── Скачивание картинки товара ───────────────────────────────────────────────

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

def fetch_image(url: str, referer: str = ""):
    """Скачивает картинку и возвращает байты, либо None если не вышло/не картинка.
    Качает потоково: большие файлы обрываются, не загружаясь целиком."""
    if not url or not str(url).lower().startswith("http"):
        return None
    if str(url).lower().split("?")[0].endswith(".svg"):
        return None  # Telegram не показывает SVG как фото

    headers = {}
    if referer:
        headers["Referer"] = referer   # многие CDN отдают 403 без Referer

    try:
        with SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            cl = resp.headers.get("Content-Length")
            if cl and int(cl) > IMG_MAX_BYTES:
                log.debug(f"Картинка слишком большая по Content-Length ({cl} б): {url}")
                return None
            chunks, total = [], 0
            for chunk in resp.iter_content(64 * 1024):
                chunks.append(chunk)
                total += len(chunk)
                if total > IMG_MAX_BYTES:
                    log.debug(f"Картинка превысила лимит при скачивании: {url}")
                    return None
            data = b"".join(chunks)
    except (requests.RequestException, ValueError) as e:
        log.warning(f"Не удалось скачать картинку {url}: {e}")
        return None

    if len(data) < IMG_MIN_BYTES:
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
                price  = clean_price(safe_text(card.select_one(sel.get("price", ""))) ) if sel.get("price") else ""
                img    = extract_img_url(card.select_one(sel["img"]), base)
                href   = safe_attr(card.select_one(sel["link"]), "href", base)

                if not name or not href or len(name) < 5:
                    continue
                if href in [base, base + "/", "https://nout.uz/", "https://pcmarket.uz/", "https://notebookoff.uz/"]:
                    continue

                products.append({"name": name, "price": price, "img": img,
                                 "url": href, "page": url})
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
                        continue
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

# ─── Категории товаров (иконка + хэштег) ──────────────────────────────────────
# Тип категории влияет на выбор хэштега, когда в названии есть и устройство,
# и его комплектующее:
#   primary    — самостоятельное устройство (ноутбук, ПК, монитор, мышь…).
#                Обычно именно оно и есть товар.
#   accessory  — аксессуар: в «Сумка для ноутбука» товар — это сумка, а слово
#                «ноутбук» лишь уточнение.
#   component  — комплектующее (SSD, флешка, HDD…). В названиях компьютеров и
#                ноутбуков оно почти всегда стоит как ХАРАКТЕРИСТИКА
#                («Компьютер … SSD 512 ГБ»). Поэтому такой хэштег ставится,
#                только если в названии нет устройства ИЛИ комплектующее идёт
#                раньше устройства (значит, продают именно его).
#
# Ключи — это НАЧАЛА слов (стемы): «ноутбук» поймает «ноутбуки», «ноутбука».
# Совпадение ищется по началу слова (через \b), поэтому «пк» не сработает
# внутри «кно[пк]а» или «па[пк]а», а «ssd» — внутри «nvme[ssd]».
PRODUCT_CATEGORIES = [
    # ─ аксессуары ─
    ("accessory", ("сумк", "рюкзак", "чехол", "кейс для ноут"),         "🎒", "#аксессуары"),
    ("accessory", ("коврик", "mouse pad", "mousepad"),                  "🖱", "#аксессуары"),
    ("accessory", ("веб-камер", "вебкам", "webcam", "веб камер"),       "📷", "#аксессуары"),
    # ─ устройства ─
    ("primary",   ("ноутбук", "ноут", "notebook", "laptop", "macbook",
                   "ultrabook", "ультрабук"),                           "💻", "#ноутбуки"),
    ("primary",   ("моноблок", "all-in-one", "all in one"),             "🖥", "#моноблоки"),
    ("primary",   ("компьютер", "пк", "пэвм", "системный блок",
                   "системник", "десктоп", "рабочая станция",
                   "gaming pc"),                                        "🖥", "#компьютеры"),
    ("primary",   ("монитор", "monitor", "дисплей"),                    "🖥", "#мониторы"),
    ("primary",   ("наушник", "headphone", "headset", "earbud",
                   "earphone", "airpods", "гарнитур"),                  "🎧", "#наушники"),
    ("primary",   ("клавиатур", "keyboard"),                            "⌨️", "#клавиатуры"),
    ("primary",   ("мыш", "mouse"),                                     "🖱", "#мыши"),
    ("primary",   ("колонк", "speaker", "акустик", "саундбар",
                   "soundbar"),                                         "🔊", "#аудио"),
    ("primary",   ("принтер", "printer", "мфу", "сканер"),              "🖨", "#принтеры"),
    ("primary",   ("планшет", "tablet", "ipad"),                        "📱", "#планшеты"),
    ("primary",   ("смартфон", "телефон", "iphone", "galaxy"),          "📱", "#смартфоны"),
    ("primary",   ("часы", "watch"),                                    "⌚", "#гаджеты"),
    # ─ комплектующие (часто это просто характеристика устройства) ─
    ("component", ("ssd", "nvme", "hdd", "флешк", "накопит",
                   "жёсткий диск", "жесткий диск", "винчестер"),        "💾", "#накопители"),
]

_TIER_RANK = {"accessory": 0, "primary": 1, "component": 2}

def _kw_pattern(k: str) -> str:
    """Регэксп для ключа: совпадение по началу слова. Короткие латинские
    токены (ssd, hdd) ищем как ЦЕЛОЕ слово, иначе «ssd» поймал бы «ssdrive»."""
    pat = r"\b" + re.escape(k)
    if k.isascii() and k.isalpha() and len(k) <= 3:
        pat += r"\b"
    return pat

def _category_matches(low: str):
    """Список совпавших категорий: (позиция_раннего_ключа, тип, emoji, тег)."""
    out = []
    for ctype, keys, emoji, tag in PRODUCT_CATEGORIES:
        best_pos = None
        for k in keys:
            m = re.search(_kw_pattern(k), low)
            if m and (best_pos is None or m.start() < best_pos):
                best_pos = m.start()
        if best_pos is not None:
            out.append((best_pos, ctype, emoji, tag))
    return out

def product_meta(name: str):
    """Подбирает иконку и хэштег по названию товара.

    Устойчиво к тому, что у устройства в названии перечислены комплектующие:
      «Игровой компьютер … SSD 512 ГБ … клавиатура»  → #компьютеры
      «ПК Ryzen 5 / SSD 256»                          → #компьютеры
      «Сумка для ноутбука»                            → #аксессуары
      «Наушники для компьютера»                       → #наушники
      «SSD Samsung 970 EVO 1 ТБ»                      → #накопители
    Комплектующее (#накопители) выбирается, только если устройства в названии
    нет ИЛИ комплектующее стоит раньше любого устройства (продают сам диск)."""
    low = name.lower()
    matches = _category_matches(low)
    if not matches:
        return ("🛒", "#техника")

    strong = [m for m in matches if m[1] in ("accessory", "primary")]
    if strong:
        min_strong = min(m[0] for m in strong)
        # комплектующее учитываем, только если оно идёт раньше любого устройства
        candidates = strong + [m for m in matches
                               if m[1] == "component" and m[0] < min_strong]
    else:
        candidates = matches   # устройств нет — товар и есть это комплектующее

    # побеждает самый ранний ключ; при равенстве: аксессуар > устройство > деталь
    best = min(candidates, key=lambda m: (m[0], _TIER_RANK[m[1]]))
    return (best[2], best[3])

def format_caption(product: dict) -> str:
    """Лимит подписи к фото — 1024 символа, поэтому название обрезаем,
    а текст экранируем (символы <, >, & в названиях ломают parse_mode=HTML)."""
    raw_name = product["name"]
    if len(raw_name) > NAME_MAX_LEN:
        raw_name = raw_name[:NAME_MAX_LEN].rstrip() + "…"

    emoji, cat_tag = product_meta(product["name"])
    name = html.escape(raw_name)
    brand_tag = "#" + STORE_NAME.replace(" ", "")

    caption = (
        f"{emoji} <b>{name}</b>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏪 <b>{html.escape(STORE_NAME)}</b>\n"
        f"📍 {html.escape(STORE_ADDRESS)}\n\n"
        f"{html.escape(CONTACT_LINE_1)}\n"
        f"{html.escape(CONTACT_LINE_2)}\n\n"
        f"💬 <i>Напишите нам — поможем с выбором</i>\n\n"
        f"{cat_tag} {brand_tag}"
    )

    # Страховка: если всё равно длиннее лимита подписи — режем название ещё.
    overflow = len(caption) - 1024
    if overflow > 0:
        shorter = raw_name[:max(20, len(raw_name) - overflow - 1)].rstrip() + "…"
        caption = caption.replace(name, html.escape(shorter), 1)
    return caption

def _maps_url() -> str:
    if MAPS_URL:
        return MAPS_URL
    if STORE_LATITUDE is not None and STORE_LONGITUDE is not None:
        return f"https://maps.google.com/?q={STORE_LATITUDE},{STORE_LONGITUDE}"
    return ""

def contact_keyboard(with_location: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("💬 Написать нам", url=CONTACT_BUTTON_URL)]]
    if with_location:
        maps = _maps_url()
        if maps:
            rows.append([InlineKeyboardButton("📍 Как доехать", url=maps)])
    return InlineKeyboardMarkup(rows)

async def post_product(bot: Bot, product: dict) -> bool:
    caption = format_caption(product)
    # Кнопка с картой вместо отдельного сообщения-локации:
    # не захламляет ленту, а адрес и так есть в подписи.
    keyboard = contact_keyboard(with_location=True)
    image = fetch_image(product.get("img", ""), referer=product.get("page", ""))

    try:
        if image:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=caption,
                                 parse_mode="HTML", reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                   parse_mode="HTML", reply_markup=keyboard)
        return True
    except TelegramError as e:
        log.error(f"Telegram ошибка с фото '{product['name'][:60]}': {e}")
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                   parse_mode="HTML", reply_markup=keyboard)
            return True
        except TelegramError as e2:
            log.error(f"Повторная ошибка: {e2}")
            return False

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

    bot = Bot(token=TELEGRAM_TOKEN)

    # Посты товаров по слотам
    can_post, slot = should_post_now()
    if not can_post:
        return

    posted = load_posted()

    all_products, failed_sources = parse_all()
    log.info(f"Всего товаров: {len(all_products)}, в истории публикаций: {len(posted)}")

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
