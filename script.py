"""
Telegram Bot: Парсер товаров + праздничные поздравления
- 2 поста с товарами в день в случайное время (по Ташкенту)
- Картинка товара скачивается и отправляется файлом
- Если новых товаров нет — повторно публикуется давно не выходивший
- В каждый праздник Узбекистана — поздравление от компании со своим
  текстом и своей открыткой (картинкой)
- Локация магазина прикрепляется к товарным постам (если заданы координаты)
"""

import os
import json
import time
import random
import logging
import requests
import asyncio
from io import BytesIO
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

# Тестовый режим (через кнопку "Run workflow"): если задан слаг праздника,
# скрипт сразу отправит это поздравление, не трогая историю и слоты товаров.
FORCE_HOLIDAY       = os.environ.get("FORCE_HOLIDAY", "").strip()
if FORCE_HOLIDAY.lower() in ("", "none"):
    FORCE_HOLIDAY = ""
TEST_CHAT_ID        = os.environ.get("TEST_CHAT_ID", "").strip()   # куда слать тест (пусто = канал)

POSTED_FILE         = "posted.json"
STATE_FILE          = "state.json"
HOLIDAYS_FILE       = "holidays.json"     # какие праздники уже поздравлены
REQUEST_TIMEOUT     = 15
REQUEST_RETRIES     = 2          # повторов на неудачный запрос страницы
REQUEST_DELAY_MIN   = 1.0        # пауза между страницами (вежливость к сайтам)
REQUEST_DELAY_MAX   = 2.5
POSTS_PER_DAY       = 2
POSTED_RETENTION_DAYS = 365      # сколько хранить историю публикаций
HOLIDAY_POST_HOUR   = 9          # с какого часа (Ташкент) постить поздравление

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
LOGO_PATH           = os.environ.get("LOGO_PATH", "logo.png")   # лого для открыток (в корне репозитория)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# ─── Праздники Узбекистана ────────────────────────────────────────────────────
# Фиксированные праздники: ключ (месяц, день).
FIXED_HOLIDAYS = {
    (1, 1): {
        "name": "Новый год",
        "title": "С Новым годом!",
        "emoji": "🎄",
        "subtitle": "Пусть новый год будет добр к вам",
        "message": ("Пусть уходящий год оставит лишь лучшие воспоминания, а "
                    "новый принесёт здоровье, тепло близких и исполнение "
                    "задуманного. Благодарим вас за доверие и будем рады быть "
                    "полезными в наступающем году!"),
        "c1": "#1a237e", "c2": "#0d1240", "accent": "#ffd54f",
    },
    (1, 14): {
        "name": "День защитников Родины",
        "title": "С Днём защитников Родины!",
        "emoji": "🎖️",
        "subtitle": "Сила, мужество и мирное небо",
        "message": ("Поздравляем с Днём защитников Родины! Желаем крепкого "
                    "здоровья, внутренней силы и уверенности в завтрашнем дне. "
                    "Спасибо каждому, кто хранит покой нашей страны."),
        "c1": "#4b5320", "c2": "#2e3514", "accent": "#d4af37",
    },
    (3, 8): {
        "name": "Международный женский день",
        "title": "С 8 Марта!",
        "emoji": "🌷",
        "subtitle": "Весеннего тепла и внимания",
        "message": ("Дорогие женщины! Пусть в вашей жизни всегда найдётся место "
                    "заботе, восхищению и приятным сюрпризам. Желаем здоровья, "
                    "вдохновения и весеннего настроения каждый день. "
                    "С праздником!"),
        "c1": "#e91e63", "c2": "#ad1457", "accent": "#ffe0ec",
    },
    (3, 21): {
        "name": "Навруз",
        "title": "С праздником Навруз!",
        "emoji": "🌷",
        "subtitle": "Обновление, достаток и добро",
        "message": ("С праздником весеннего обновления! Пусть Навруз наполнит "
                    "дом достатком, согласием и добрыми надеждами, а всё "
                    "задуманное расцветёт вместе с весной. Навруз муборак!"),
        "c1": "#1b8a5a", "c2": "#0b5d3b", "accent": "#ffd166",
    },
    (5, 9): {
        "name": "День Памяти и Почестей",
        "title": "День Памяти и Почестей",
        "emoji": "🕊️",
        "subtitle": "Помним. Чтим. Благодарим.",
        "message": ("В этот день мы склоняем головы перед мужеством тех, кто "
                    "отстоял мир для будущих поколений. Светлая память героям и "
                    "глубокая благодарность ветеранам. Мира и спокойствия "
                    "каждому дому."),
        "c1": "#7b1e1e", "c2": "#4a1010", "accent": "#e0c097",
    },
    (9, 1): {
        "name": "День Независимости",
        "title": "С Днём Независимости!",
        "emoji": "🇺🇿",
        "subtitle": "Мир, единство и процветание",
        "message": ("Поздравляем с Днём Независимости Республики Узбекистан! "
                    "Желаем нашей стране уверенного развития, а каждой семье — "
                    "мира, благополучия и гордости за родную землю."),
        "c1": "#1565c0", "c2": "#0b3d91", "accent": "#9be7ff",
    },
    (10, 1): {
        "name": "День учителя и наставника",
        "title": "С Днём учителя и наставника!",
        "emoji": "📚",
        "subtitle": "Знания, мудрость и благодарность",
        "message": ("Поздравляем всех, кто учит и наставляет! Ваши труд и "
                    "терпение формируют будущее. Желаем благодарных учеников, "
                    "вдохновения и сил для новых открытий."),
        "c1": "#b9770e", "c2": "#7a4d06", "accent": "#ffe3a3",
    },
    (12, 8): {
        "name": "День Конституции",
        "title": "С Днём Конституции!",
        "emoji": "🇺🇿",
        "subtitle": "Закон, права и стабильность",
        "message": ("Поздравляем с Днём Конституции Республики Узбекистан! Пусть "
                    "в основе каждого дня будут уважение, справедливость и "
                    "уверенность в завтрашнем дне. Благополучия вам и вашим "
                    "близким!"),
        "c1": "#1976d2", "c2": "#0d47a1", "accent": "#bbdefb",
    },
}

# Религиозные (переходящие) праздники — шаблоны.
MOVABLE_HOLIDAYS = {
    "ramazan": {
        "name": "Рамазан хайит",
        "title": "С праздником Рамазан хайит!",
        "emoji": "🌙",
        "subtitle": "Мир, милосердие и благодать",
        "message": ("Поздравляем со светлым праздником Рамазан хайит! Пусть "
                    "искренние молитвы будут услышаны, а дом наполнится миром, "
                    "здоровьем и достатком. Рамазон ҳайити муборак бўлсин!"),
        "c1": "#0f766e", "c2": "#064e46", "accent": "#ffe08a",
    },
    "kurban": {
        "name": "Курбан хайит",
        "title": "С праздником Курбан хайит!",
        "emoji": "🕌",
        "subtitle": "Щедрость, согласие и достаток",
        "message": ("Поздравляем с праздником Курбан хайит! Пусть милосердие и "
                    "щедрость вернутся к вам сторицей, а в доме всегда царят "
                    "согласие, благополучие и радость. "
                    "Қурбон ҳайити муборак бўлсин!"),
        "c1": "#15803d", "c2": "#0b5d2b", "accent": "#ffd700",
    },
}

# Даты переходящих праздников по годам. Официальные даты объявляет Управление
# мусульман Узбекистана ближе к празднику — даты на 2027 РАСЧЁТНЫЕ, уточните и
# при необходимости поправьте. Для 2028+ добавьте новые строки.
MOVABLE_DATES = {
    "2026-03-20": "ramazan",   # подтверждено
    "2026-05-27": "kurban",    # подтверждено
    "2027-03-10": "ramazan",   # ⚠ расчётно — уточнить
    "2027-05-17": "kurban",    # ⚠ расчётно — уточнить
}

# Короткие имена (слаги) праздников — нужны для тестового запуска вручную.
_FIXED_SLUGS = {
    (1, 1):   "new_year",
    (1, 14):  "defenders",
    (3, 8):   "march8",
    (3, 21):  "navruz",
    (5, 9):   "memory_day",
    (9, 1):   "independence",
    (10, 1):  "teachers_day",
    (12, 8):  "constitution",
}
HOLIDAYS_BY_SLUG = {slug: FIXED_HOLIDAYS[key] for key, slug in _FIXED_SLUGS.items()}
HOLIDAYS_BY_SLUG.update(MOVABLE_HOLIDAYS)   # ключи "ramazan", "kurban"

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

# ─── Учёт поздравлений (чтобы поздравить раз в праздник) ──────────────────────

def load_holidays_posted() -> set:
    if not os.path.exists(HOLIDAYS_FILE):
        return set()
    try:
        with open(HOLIDAYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError) as e:
        log.warning(f"Не удалось прочитать {HOLIDAYS_FILE}: {e}")
        return set()

def save_holidays_posted(done: set) -> None:
    try:
        with open(HOLIDAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(done), f, ensure_ascii=False, indent=2)
    except IOError as e:
        log.error(f"Не удалось сохранить {HOLIDAYS_FILE}: {e}")

def get_today_holiday(now: datetime):
    """Возвращает данные праздника на сегодня или None."""
    rid = MOVABLE_DATES.get(now.strftime("%Y-%m-%d"))
    if rid:
        return MOVABLE_HOLIDAYS.get(rid)
    return FIXED_HOLIDAYS.get((now.month, now.day))

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

    if not (2048 <= len(data) <= 10 * 1024 * 1024):
        log.debug(f"Картинка отброшена по размеру ({len(data)} б): {url}")
        return None
    if not _looks_like_image(data):
        log.debug(f"Не похоже на изображение: {url}")
        return None
    return data

# ─── Генерация праздничной открытки ───────────────────────────────────────────

_FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_FONT_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

def _font_path(bold: bool):
    for p in (_FONT_BOLD_PATHS if bold else _FONT_REG_PATHS):
        if os.path.exists(p):
            return p
    return None

def _hex(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

def _logo_white(Image):
    """Логотип (белый знак на чёрном) → RGBA: белый знак на прозрачном фоне.
    Прозрачность берётся из яркости, чёрный фон уходит сам. None — если файла нет."""
    if not LOGO_PATH or not os.path.exists(LOGO_PATH):
        return None
    try:
        mask = Image.open(LOGO_PATH).convert("L")          # яркость = маска
        white = Image.new("RGBA", mask.size, (255, 255, 255, 255))
        clear = Image.new("RGBA", mask.size, (255, 255, 255, 0))
        return Image.composite(white, clear, mask)          # alpha = яркость
    except Exception as e:
        log.warning(f"Не удалось загрузить логотип {LOGO_PATH}: {e}")
        return None

def _draw_card(W: int, hol: dict, Image, ImageDraw, ImageFont):
    """Рисует квадратную открытку размера WxW. Все размеры — доли от W,
    поэтому можно рисовать крупно и потом уменьшить (для чётких краёв)."""
    H = W
    bold_path = _font_path(bold=True)
    reg_path = _font_path(bold=False) or bold_path

    top, bot = _hex(hol["c1"]), _hex(hol["c2"])
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=(
            int(top[0] + (bot[0] - top[0]) * t),
            int(top[1] + (bot[1] - top[1]) * t),
            int(top[2] + (bot[2] - top[2]) * t),
        ))

    accent = hol.get("accent", "#ffffff")
    title_font = ImageFont.truetype(bold_path, round(W * 0.089))
    sub_font   = ImageFont.truetype(reg_path, round(W * 0.041))
    brand_font = ImageFont.truetype(bold_path, round(W * 0.037))

    # логотип по центру сверху
    resample = getattr(Image, "Resampling", Image).LANCZOS
    logo = _logo_white(Image)
    if logo:
        lw = round(W * 0.20)
        lh = max(1, round(lw * logo.height / logo.width))
        logo_r = logo.resize((lw, lh), resample)
        img.paste(logo_r, ((W - lw) // 2, round(W * 0.085)), logo_r)

    def wrap(text, font, max_w):
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if d.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    # заголовок (чуть ниже центра — освобождаем место под логотип сверху)
    title_lines = wrap(hol["title"], title_font, W - round(W * 0.185))
    line_h = title_font.getbbox("Ay")[3] + round(W * 0.017)
    y = (H - line_h * len(title_lines)) // 2 + round(W * 0.045)
    for ln in title_lines:
        w = d.textlength(ln, font=title_font)
        d.text(((W - w) / 2, y), ln, font=title_font, fill="#ffffff")
        y += line_h

    # декоративная линия
    ly = y + round(W * 0.022)
    hw = round(W * 0.083)
    th = max(2, round(W * 0.0056))
    d.rectangle([(W / 2 - hw, ly), (W / 2 + hw, ly + th)], fill=accent)

    # подзаголовок
    sub = hol.get("subtitle", "")
    if sub:
        sy = ly + round(W * 0.037)
        for ln in wrap(sub, sub_font, W - round(W * 0.222)):
            w = d.textlength(ln, font=sub_font)
            d.text(((W - w) / 2, sy), ln, font=sub_font, fill="#f1f1f1")
            sy += sub_font.getbbox("Ay")[3] + round(W * 0.011)

    # имя компании внизу
    brand = " ".join(STORE_NAME.upper())
    w = d.textlength(brand, font=brand_font)
    d.text(((W - w) / 2, H - round(W * 0.102)), brand, font=brand_font, fill=accent)
    return img

def make_greeting_image(hol: dict):
    """Возвращает PNG-байты открытки или None (тогда поздравление выйдет текстом).
    Рисуем в 2× и уменьшаем с LANCZOS — края текста выходят гладкими и чёткими."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.info("Pillow не установлен — поздравление выйдет текстом.")
        return None

    if not _font_path(bold=True):
        log.info("Шрифт для открытки не найден — поздравление выйдет текстом.")
        return None

    try:
        FINAL = 1280       # итоговый размер (с запасом под сжатие Telegram)
        SS = 2             # супер-сэмплинг: рисуем крупнее, потом уменьшаем
        resample = getattr(Image, "Resampling", Image).LANCZOS

        big = _draw_card(FINAL * SS, hol, Image, ImageDraw, ImageFont)
        card = big.resize((FINAL, FINAL), resample)

        buf = BytesIO()
        card.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"Не удалось сгенерировать открытку: {e}")
        return None

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

# Категория товара → иконка и хэштег (по первому совпадению; порядок важен).
PRODUCT_CATEGORIES = [
    # аксессуары "для ноутбука" проверяем раньше ноутбуков, иначе сумка/чехол → #ноутбуки
    (("сумк", "рюкзак", "чехол"),                                       "🎒", "#аксессуары"),
    (("ноутбук", "ноут", "notebook", "laptop", "macbook", "ultrabook"), "💻", "#ноутбуки"),
    (("моноблок", "all-in-one"),                                        "🖥", "#моноблоки"),
    (("монитор", "monitor", "дисплей"),                                 "🖥", "#мониторы"),
    (("наушник", "headphone", "headset", "earbud", "earphone",
      "airpods", "гарнитур"),                                           "🎧", "#наушники"),
    (("клавиатур", "keyboard"),                                         "⌨️", "#клавиатуры"),
    (("коврик", "mouse pad", "mousepad"),                               "🖱", "#аксессуары"),
    (("мышь", "мышк", "mouse"),                                         "🖱", "#мыши"),
    (("колонк", "speaker", "акустик", "саундбар", "soundbar"),          "🔊", "#аудио"),
    (("принтер", "printer", "мфу", "сканер"),                           "🖨", "#принтеры"),
    (("флешк", "флеш", "накопит", "ssd", "жёсткий диск", "жесткий диск"),"💾", "#накопители"),
    (("планшет", "tablet", "ipad"),                                     "📱", "#планшеты"),
    (("смартфон", "телефон", "iphone", "galaxy"),                       "📱", "#смартфоны"),
    (("часы", "watch"),                                                 "⌚", "#гаджеты"),
    (("веб-камер", "вебкам", "webcam", "камер"),                        "📷", "#аксессуары"),
    (("компьютер", "системный блок", "системник", "десктоп"),           "🖥", "#компьютеры"),
]

def product_meta(name: str):
    low = name.lower()
    for keys, emoji, tag in PRODUCT_CATEGORIES:
        if any(k in low for k in keys):
            return emoji, tag
    return "🛒", "#техника"

def format_caption(product: dict) -> str:
    emoji, cat_tag = product_meta(product["name"])
    brand_tag = "#" + STORE_NAME.replace(" ", "")
    return (
        f"{emoji} <b>{product['name']}</b>\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏪 <b>{STORE_NAME}</b>\n"
        f"📍 {STORE_ADDRESS}\n\n"
        f"{CONTACT_LINE_1}\n"
        f"{CONTACT_LINE_2}\n\n"
        f"💬 <i>Напишите нам — поможем с выбором</i>\n\n"
        f"{cat_tag} {brand_tag}"
    )

def build_holiday_caption(hol: dict) -> str:
    return (
        f"{hol['emoji']} <b>{hol['title']}</b>\n\n"
        f"{hol['message']}\n\n"
        f"С уважением, команда <b>{STORE_NAME}</b> 💙"
    )

def contact_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Написать нам", url=CONTACT_BUTTON_URL)]
    ])

async def post_product(bot: Bot, product: dict) -> bool:
    caption = format_caption(product)
    keyboard = contact_keyboard()
    image = fetch_image(product.get("img", ""))
    sent = False

    # 1. Фото товара с описанием (с откатом на текст)
    try:
        if image:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=caption,
                                 parse_mode="HTML", reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                   parse_mode="HTML", reply_markup=keyboard)
        sent = True
    except TelegramError as e:
        log.error(f"Telegram ошибка с фото '{product['name']}': {e}")
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=caption,
                                   parse_mode="HTML", reply_markup=keyboard)
            sent = True
        except TelegramError as e2:
            log.error(f"Повторная ошибка: {e2}")
            return False

    # 2. Карта с локацией магазина (если координаты заданы)
    if sent and STORE_LATITUDE is not None and STORE_LONGITUDE is not None:
        try:
            await bot.send_venue(chat_id=CHANNEL_ID, latitude=STORE_LATITUDE,
                                 longitude=STORE_LONGITUDE, title=STORE_NAME,
                                 address=STORE_ADDRESS)
            log.info("Локация магазина прикреплена")
        except TelegramError as ve:
            log.warning(f"Не удалось отправить локацию: {ve}")

    return sent

async def send_greeting(bot: Bot, caption: str, image, chat_id=None) -> bool:
    target = chat_id or CHANNEL_ID
    keyboard = contact_keyboard()
    try:
        if image:
            await bot.send_photo(chat_id=target, photo=image, caption=caption,
                                 parse_mode="HTML", reply_markup=keyboard)
        else:
            await bot.send_message(chat_id=target, text=caption,
                                   parse_mode="HTML", reply_markup=keyboard)
        return True
    except TelegramError as e:
        log.error(f"Не удалось опубликовать поздравление: {e}")
        try:
            await bot.send_message(chat_id=target, text=caption,
                                   parse_mode="HTML", reply_markup=keyboard)
            return True
        except TelegramError as e2:
            log.error(f"Повторная ошибка поздравления: {e2}")
            return False

async def notify_admin(bot: Bot, text: str) -> None:
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except TelegramError as e:
        log.warning(f"Не удалось отправить алерт администратору: {e}")

async def post_test_greeting(bot: Bot) -> None:
    """Тест: сразу отправить поздравление по выбранному слагу (FORCE_HOLIDAY).
    Не трогает историю праздников и слоты товаров — можно запускать сколько угодно."""
    hol = HOLIDAYS_BY_SLUG.get(FORCE_HOLIDAY)
    if not hol:
        log.error(f"FORCE_HOLIDAY={FORCE_HOLIDAY!r} не распознан. "
                  f"Доступно: {', '.join(sorted(HOLIDAYS_BY_SLUG))}")
        return
    target = TEST_CHAT_ID or CHANNEL_ID
    caption = build_holiday_caption(hol)
    if not TEST_CHAT_ID:
        caption = "🧪 <i>Предпросмотр праздничного поста</i>\n\n" + caption
    log.info(f"[ТЕСТ] Поздравление «{hol['name']}» → чат {target}")
    image = make_greeting_image(hol)
    if await send_greeting(bot, caption, image, chat_id=target):
        log.info("[ТЕСТ] Отправлено. История праздников не затронута.")
    else:
        log.error("[ТЕСТ] Не удалось отправить.")

async def maybe_post_holiday(bot: Bot) -> None:
    """Если сегодня праздник и мы ещё не поздравляли — публикуем поздравление."""
    now = datetime.now(TIMEZONE)
    hol = get_today_holiday(now)
    if not hol:
        return
    if now.hour < HOLIDAY_POST_HOUR:
        log.info(f"Сегодня праздник «{hol['name']}», но ещё рано ({now.hour}:00 < {HOLIDAY_POST_HOUR}:00).")
        return

    key = now.strftime("%Y-%m-%d")
    done = load_holidays_posted()
    if key in done:
        log.info(f"Поздравление с «{hol['name']}» уже было опубликовано.")
        return

    log.info(f"Публикуем поздравление: {hol['name']}")
    caption = build_holiday_caption(hol)
    image = make_greeting_image(hol)
    if await send_greeting(bot, caption, image):
        done.add(key)
        save_holidays_posted(done)
        log.info(f"─── Поздравление «{hol['name']}» опубликовано ───")
    else:
        log.error(f"Не удалось опубликовать поздравление с «{hol['name']}».")

# ─── Главная функция ─────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN or not CHANNEL_ID:
        log.error("Не заданы TELEGRAM_TOKEN или CHANNEL_ID!")
        return

    bot = Bot(token=TELEGRAM_TOKEN)

    # Тестовый режим: ручной запуск с выбранным праздником из выпадающего списка
    if FORCE_HOLIDAY:
        await post_test_greeting(bot)
        return

    # 0. Праздничное поздравление (раз в праздник, независимо от слотов товаров)
    await maybe_post_holiday(bot)

    # 1. Обычные посты товаров по слотам
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
