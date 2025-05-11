import os
import logging
import random
import json

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo  # Python 3.9+
from io import BytesIO
from collections import Counter
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    BotCommand,
    BotCommandScopeChat,
    InputFile
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

from dotenv import load_dotenv

# Загрузка .env
load_dotenv()

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Файл для активности пользователей
USER_FILE = Path("user_activity.json")
# файл для предложений пользователей
SUGGESTIONS_FILE = Path("suggestions.json")
# админ айди
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

async def set_commands(app):
    
    await app.bot.set_my_commands(
        [
            BotCommand("start",         "Показать приветствие"),
            BotCommand("play",          "Начать новую игру"),
            BotCommand("hint",    "Подсказка"),
            BotCommand("reset",         "Сбросить игру"),
            BotCommand("notification",         "Включить/Отключить уведомления"),
            BotCommand("my_stats",      "Ваша статистика"),
            BotCommand("global_stats",  "Глобальная статистика"),
            BotCommand("feedback", "Жалоба на слово"),
            BotCommand("dict_file",  "Посмотреть словарь"),
            BotCommand("dump_activity", "Скачать user_activity.json"),
            BotCommand("suggestions_view", "Посмотреть фидбек юзеров"),
            BotCommand("suggestions_remove", "Удалить что-то из фидбека"),
            BotCommand("suggestions_approve", "Внести изменения в словарь"),
            BotCommand("broadcast", "Отправить сообщение всем юзерам"),
            BotCommand("broadcast_cancel", "Отменить отправку")
        ],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID)
    )


def load_suggestions() -> dict[str, set[str]]:
    """Возвращает {'black': set(...), 'white': set(...)} без дубликатов."""
    if not SUGGESTIONS_FILE.exists():
        return {"black": set(), "white": set()}
    raw = SUGGESTIONS_FILE.read_text("utf-8").strip()
    if not raw:
        return {"black": set(), "white": set()}
    try:
        data = json.loads(raw)
        return {
            "black": set(data.get("black", [])),
            "white": set(data.get("white", [])),
        }
    except json.JSONDecodeError:
        return {"black": set(), "white": set()}



def save_suggestions(sugg: dict[str, set[str]]):
    """
    Сохраняет suggestions, конвертируя множества в отсортированные списки.
    """
    out = {
        "black": sorted(sugg["black"]),
        "white": sorted(sugg["white"]),
    }
    with SUGGESTIONS_FILE.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


# загружаем один раз при старте
suggestions = load_suggestions()

def load_store() -> dict:
    """
    Загружает user_activity.json.
    Если файла нет или он пуст/битый — возвращает чистый шаблон:
    {
      "users": {},
      "global": { "total_games":0, "total_wins":0, "total_losses":0, "win_rate":0.0 }
    }
    """
    template = {
        "users": {},
        "global": {
            "total_games": 0,
            "total_wins": 0,
            "total_losses": 0,
            "win_rate": 0.0
        }
    }
    if not USER_FILE.exists():
        return template

    raw = USER_FILE.read_text("utf-8").strip()
    if not raw:
        return template

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return template

    # Убедимся, что структура корректна
    if not isinstance(data, dict):
        return template

    # Проверим разделы
    if not isinstance(data.get("users"), dict):
        data["users"] = {}
    if not isinstance(data.get("global"), dict):
        data["global"] = template["global"].copy()

    # Подставим недостающие ключи в global
    for key, val in template["global"].items():
        data["global"].setdefault(key, val)

    return data

def save_store(store: dict) -> None:
    """
    Сохраняет переданный store в USER_FILE в JSON-формате с отступами.
    Ожидаем, что store имеет формат:
    {
      "users": { ... },
      "global": { ... }
    }
    """
    USER_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def update_user_activity(user) -> None:
    """
    Создает или обновляет запись user в store['users'], добавляя:
    - first_name, last_name, username
    - is_bot, is_premium, language_code
    - last_seen_msk (по московскому времени)
    - stats (если еще нет): games_played, wins, losses, win rate
    """
    store = load_store()
    uid = str(user.id)
    users = store["users"]

    # Если пользователь впервые — создаем базовую запись
    if uid not in users:
        users[uid] = {
            "first_name": user.first_name,
            "stats": {
                "games_played": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0
            }
        }

    u = users[uid]
    # Обновляем поля профиля
    u["first_name"]    = user.first_name
    u["last_name"]     = user.last_name
    u["username"]      = user.username
    u["is_bot"]        = user.is_bot
    u["is_premium"]    = getattr(user, "is_premium", False)
    u["language_code"] = user.language_code
    u["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()

    save_store(store)


def normalize(text: str) -> str:
    # переводим все в нижний регистр и убираем «е»
    return text.strip().lower().replace("ё", "е")


def compute_letter_status(secret: str, guesses: list[str]) -> dict[str, str]:
    """
    Для каждой буквы возвращает:
      - "green"  если была 🟩
      - "yellow" если была 🟨 (и не была 🟩)
      - "red"    если была ⬜ (и не была ни 🟩, ни 🟨)
    """
    status: dict[str,str] = {}
    for guess in guesses:
        fb = [] 
        s_chars = list(secret)
        # сначала зеленые
        for i,ch in enumerate(guess):
            if secret[i] == ch:
                fb.append("🟩")
                s_chars[i] = None
            else:
                fb.append(None)
        # затем желтые/красные
        for i,ch in enumerate(guess):
            if fb[i] is None:
                if ch in s_chars:
                    fb[i] = "🟨"
                    s_chars[s_chars.index(ch)] = None
                else:
                    fb[i] = "⬜"
        # обновляем глобальный статус
        for ch,sym in zip(guess, fb):
            prev = status.get(ch)
            if sym == "🟩":
                status[ch] = "green"
            elif sym == "🟨" and prev != "green":
                status[ch] = "yellow"
            elif sym == "⬜" and prev not in ("green","yellow"):
                status[ch] = "red"
    return status


# Русская раскладка виртуальной клавиатуры
KB_LAYOUT = [
    list("йцукенгшщзхъ"),
    list("фывапролджэ"),
    list("ячсмитьбю")
]

def render_full_board_with_keyboard(
    guesses: list[str],
    secret: str,
    total_rows: int = 6,
    max_width_px: int = 1080
) -> BytesIO:
    padding   = 6
    board_def = 80
    cols      = len(secret)
    total_pad = (cols + 1) * padding

    # размер квадратика доски
    board_sq = min(board_def, (max_width_px - total_pad) // cols)
    board_sq = max(20, board_sq)

    board_w = cols * board_sq + total_pad
    board_h = total_rows * board_sq + (total_rows + 1) * padding

    # выбираем масштаб клавиш по длине слова
    if cols >= 8:
        factor = 0.6
    elif cols == 7:
        factor = 0.5
    elif cols == 6:
        factor = 0.4
    elif cols == 5:
        factor = 0.3
    elif cols == 4:
        factor = 0.25

    kb_sq   = max(12, int(board_sq * factor))
    kb_rows = len(KB_LAYOUT)
    img_h   = board_h + kb_rows * kb_sq + (kb_rows + 1) * padding

    img        = Image.new("RGB", (board_w, img_h), (30, 30, 30))
    draw       = ImageDraw.Draw(img)
    font_board = ImageFont.truetype("DejaVuSans-Bold.ttf", int(board_sq * 0.6))
    font_kb    = ImageFont.truetype("DejaVuSans-Bold.ttf", int(kb_sq * 0.6))

    # --- игровая доска (6 строк) ---
    for r in range(total_rows):
        y0 = padding + r * (board_sq + padding)
        if r < len(guesses):
            guess = guesses[r]
            fb    = make_feedback(secret, guess)
        else:
            guess = None
            fb    = [None] * cols

        for c in range(cols):
            x0 = padding + c * (board_sq + padding)
            x1 = x0 + board_sq
            y1 = y0 + board_sq

            color = fb[c]
            if color == GREEN:
                bg = (106,170,100)
            elif color == YELLOW:
                bg = (201,180,88)
            elif color == WHITE:
                bg = (128,128,128)
            else:
                bg = (255,255,255)

            draw.rectangle([x0,y0,x1,y1], fill=bg, outline=(0,0,0), width=2)

            if guess:
                ch = guess[c].upper()
                tc = (0,0,0) if bg == (255,255,255) else (255,255,255)
                bbox = draw.textbbox((0,0), ch, font=font_board)
                w, h = bbox[2]-bbox[0], bbox[3]-bbox[1]
                draw.text(
                    (x0 + (board_sq-w)/2, y0 + (board_sq-h)/2),
                    ch, font=font_board, fill=tc
                )

    # --- мини-клавиатура ---
    letter_status = compute_letter_status(secret, guesses)
    for ri, row in enumerate(KB_LAYOUT):
        y0      = board_h + padding + ri * (kb_sq + padding)
        row_len = len(row)
        row_pad = (row_len + 1) * padding
        row_w   = row_len * kb_sq + row_pad
        x_off   = (board_w - row_w) // 2

        for i, ch in enumerate(row):
            x0 = x_off + padding + i * (kb_sq + padding)
            x1 = x0 + kb_sq
            y1 = y0 + kb_sq

            st = letter_status.get(ch)
            if st == "green":
                bg = (106,170,100)
            elif st == "yellow":
                bg = (201,180,88)
            elif st == "red":
                bg = (128,128,128)
            else:
                bg = (255,255,255)

            draw.rectangle([x0,y0,x1,y1], fill=bg, outline=(0,0,0), width=1)
            tc = (0,0,0) if bg == (255,255,255) else (255,255,255)
            letter = ch.upper()
            bbox   = draw.textbbox((0,0), letter, font=font_kb)
            w, h   = bbox[2]-bbox[0], bbox[3]-bbox[1]
            draw.text(
                (x0 + (kb_sq-w)/2, y0 + (kb_sq-h)/2),
                letter, font=font_kb, fill=tc
            )

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# --- Константы и словарь ---
ASK_LENGTH, GUESSING, FEEDBACK_CHOOSE, FEEDBACK_WORD, REMOVE_INPUT, BROADCAST= range(6)

# --- Загрузка и сортировка списка слов ---
BASE_FILE = Path("base_words.json")

# Читаем список слов из base_words.json
with BASE_FILE.open("r", encoding="utf-8") as f:
    base_words = json.load(f)

# Фильтруем по критериям: только буквы, длина 4–11 символов
filtered = [w for w in base_words if w.isalpha() and 4 <= len(w) <= 11]

# Сортируем список и записываем обратно в base_words.json
WORDLIST = sorted(filtered)
with BASE_FILE.open("w", encoding="utf-8") as f:
    json.dump(WORDLIST, f, ensure_ascii=False, indent=2)

GREEN, YELLOW, WHITE = "🟩", "🟨", "⬜"

def make_feedback(secret: str, guess: str) -> str:
    fb = [None] * len(guess)
    secret_chars = list(secret)
    # 1) зеленые
    for i, ch in enumerate(guess):
        if secret[i] == ch:
            fb[i] = GREEN
            secret_chars[i] = None
    # 2) желтые/красные
    for i, ch in enumerate(guess):
        if fb[i] is None:
            if ch in secret_chars:
                fb[i] = YELLOW
                secret_chars[secret_chars.index(ch)] = None
            else:
                fb[i] = WHITE
    return "".join(fb)


# --- Обработчики команд ---

async def send_activity_periodic(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодически (и сразу при старте) шлет user_activity.json администратору.
    Если файл слишком большой, шлет его как документ.
    """
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    activity_path = USER_FILE
    if not activity_path.exists():
        return

    content = activity_path.read_text(encoding="utf-8")
    # Ограничение Telegram — примерно 4096 символов
    MAX_LEN = 4000

    if len(content) <= MAX_LEN:
        # Можно втиснуть в одно сообщение
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📋 Текущий user_activity.json:\n<pre>{content}</pre>",
            parse_mode="HTML"
        )
    else:
        # Слишком длинное — отправляем как файл
        with activity_path.open("rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=InputFile(f, filename="user_activity.json"),
                caption="📁 user_activity.json (слишком большой для текста)"
            )


async def send_unfinished_games(context: ContextTypes.DEFAULT_TYPE):
    """
    Раз в 1 секунду после старта отправляем всем пользователям с current_game
    напоминание продолжить игру.
    """
    store = load_store()
    for uid, udata in store["users"].items():
        if not udata.get("notify_on_wakeup", True):
            continue
        if "current_game" in udata:
            cg = udata["current_game"]
            length = len(cg["secret"])
            attempts = cg["attempts"]
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=(
                        f"Я вернулся из спячки!\n"
                        f"⏳ У вас есть незавершенная игра:\n"
                        f"{length}-буквенное слово, вы на попытке {attempts}.\n"
                        "Нажмите /play или /start, чтобы продолжить!"
                    )
                )
            except Exception as e:
                logger.warning(f"Не смогли напомнить {uid}: {e}")


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # если сейчас в игре или в фидбеке — молчим
    if context.user_data.get("game_active") or context.user_data.get("in_feedback") or context.user_data.get("in_remove"):
        return
    if context.user_data.pop("just_done", False):
        return
    await update.message.reply_text(
        "Я не обрабатываю слова просто так😕\n"
        "Чтобы начать игру, введи /play."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        cg = u["current_game"]
        # заполняем context.user_data из cg:
        context.user_data.update({
            "secret": cg["secret"],
            "length": len(cg["secret"]),
            "attempts": cg["attempts"],
            "guesses": cg["guesses"],
        })
        await update.message.reply_text(
            f"Продолжаем игру: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING

    
    await update.message.reply_text(
        "Привет! Я Wordle Bot — угадай слово за 6 попыток.\n"
        "https://github.com/sqwirex/wordle-bot - ссылка на репозиторий с кодом бота\n\n"
        "/play — начать или продолжить игру\n"
        "/hint — дает слово в подсказку, если вы затрудняетесь ответить " \
        "(случайное слово в котором совпадают некоторые буквы с загаданным)\n"
        "/reset — сбросить текущую игру\n"
        "/notification — включить/отключить уведомления при пробуждении бота\n"
        "/my_stats — посмотреть свою статистику\n"
        "/global_stats — посмотреть глобальную статистику за все время\n"
        "/feedback — если ты встретил слово, которое не должно быть в словаре или не существует, введи его в Черный список, " \
        "если же наоборот, ты вбил слово, а бот его не признает, но ты уверен что оно существует, отправляй его в Белый список. " \
        "Администратор бота рассмотрит твое предложение и добавит в ближайшем обновлении, если оно действительно подходит!\n\n"
        "Только не забывай: я еще учусь и не знаю некоторых слов!\n"
        "Не расстраивайся, если я ругаюсь на твое слово — мне есть чему учиться :)\n\n"
        "Кстати, иногда я могу «выключаться», потому что живу в контейнере!\n"
        "Если я не отвечаю — попробуй позже и нажми /play или /start, чтобы продолжить прервавшуюся игру.\n\n"
    )


async def ask_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = ASK_LENGTH
    update_user_activity(update.effective_user)
    context.user_data["game_active"] = True
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        cg = u["current_game"]
        # заполняем context.user_data из cg:
        context.user_data.update({
            "secret": cg["secret"],
            "length": len(cg["secret"]),
            "attempts": cg["attempts"],
            "guesses": cg["guesses"],
        })
        await update.message.reply_text(
            f"Продолжаем игру: {len(cg['secret'])}-буквенное слово, ты на попытке {cg['attempts']}. Вводи догадку:"
        )
        return GUESSING
    
    await update.message.reply_text("Сколько букв в слове? (4–11)")
    return ASK_LENGTH


async def receive_length(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    text = update.message.text.strip()
    if not text.isdigit() or not 4 <= int(text) <= 11:
        await update.message.reply_text("Нужно число от 4 до 11.")
        return ASK_LENGTH

    length = int(text)
    candidates = [w for w in WORDLIST if len(w) == length]
    if not candidates:
        await update.message.reply_text("Не нашел слов такой длины. Попробуй еще:")
        return ASK_LENGTH

    secret = random.choice(candidates)
    
    store = load_store()
    u = store["users"].setdefault(str(update.effective_user.id), {"stats": {"games_played":0,"wins":0,"losses":0}})
    # Запись текущей игры
    u["current_game"] = {
        "secret": secret,
        "attempts": 0,
        "guesses": [],
    }
    save_store(store)

    context.user_data["secret"] = secret
    context.user_data["length"] = length
    context.user_data["attempts"] = 0
    context.user_data["guesses"] = []
    context.user_data["state"] = GUESSING

    await update.message.reply_text(
        f"Я загадал слово из {length} букв. У тебя 6 попыток. Введи первую догадку:"
    )
    
    return GUESSING


async def handle_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    store   = load_store()
    user    = store["users"].setdefault(user_id, {
        "first_name": update.effective_user.first_name,
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })

    # Обновляем время последнего визита
    user["last_seen_msk"] = datetime.now(ZoneInfo("Europe/Moscow")).isoformat()

    # Проверяем активную игру
    if "current_game" not in user:
        await update.message.reply_text("Нет активной игры, начни /play")
        return ConversationHandler.END

    cg     = user["current_game"]
    guess = normalize(update.message.text)
    secret = cg["secret"]
    length = len(secret)

    # Валидация
    if len(guess) != length or guess not in WORDLIST:
        await update.message.reply_text(f"Введите существующее слово из {length} букв.")
        return GUESSING

    # Сохраняем ход
    cg["guesses"].append(guess)
    cg["attempts"] += 1
    save_store(store)

    # Рендерим доску из 6 строк + мини-клавиатуру снизу.
    # Клавиатура будет крупнее для слов ≥8 букв, чуть меньше для 7 и еще меньше для 4–5.
    img_buf = render_full_board_with_keyboard(
        guesses=cg["guesses"],
        secret=secret,
        total_rows=6,
        max_width_px=1080
    )
    await update.message.reply_photo(
        photo=InputFile(img_buf, filename="wordle_board.png"),
        caption=f"Попытка {cg['attempts']} из 6"
    )

    # —— Победа ——
    if guess == secret:
        stats = user["stats"]
        stats["games_played"] += 1
        stats["wins"] += 1
        stats["win_rate"] = stats["wins"] / stats["games_played"]

        g = store["global"]
        g["total_games"] += 1
        g["total_wins"] += 1
        g["win_rate"] = g["total_wins"] / g["total_games"]

        top_uid, top_data = max(
            store["users"].items(),
            key=lambda kv: kv[1]["stats"]["wins"]
        )
        store["global"]["top_player"] = {
            "user_id":  top_uid,
            "username": top_data.get("username") or top_data.get("first_name", ""),
            "wins":     top_data["stats"]["wins"]
        }

        await update.message.reply_text(
            f"🎉 Поздравляю! Угадал за {cg['attempts']} "
            f"{'попытка' if cg['attempts']==1 else 'попытки' if 2<=cg['attempts']<=4 else 'попыток'}.\n"
            "Чтобы сыграть вновь, введи /play."
        )
        del user["current_game"]
        context.user_data.pop("game_active", None)
        context.user_data["just_done"] = True
        save_store(store)
        return ConversationHandler.END

    # —— Поражение ——
    if cg["attempts"] >= 6:
        stats = user["stats"]
        stats["games_played"] += 1
        stats["losses"] += 1
        stats["win_rate"] = stats["wins"] / stats["games_played"]

        g = store["global"]
        g["total_games"] += 1
        g["total_losses"] += 1
        g["win_rate"] = g["total_wins"] / g["total_games"]

        await update.message.reply_text(
            f"💔 Попытки закончились. Было слово «{secret}».\n"
            "Чтобы начать новую игру, введи /play."
        )
        del user["current_game"]
        context.user_data.pop("game_active", None)
        context.user_data["just_done"] = True
        save_store(store)
        return ConversationHandler.END

    # Игра продолжается
    return GUESSING

async def ignore_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды /start и /play не работают во время игры — сначала /reset.")
    return ASK_LENGTH


async def ignore_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команды /start и /play не работают во время игры — сначала /reset.")
    return GUESSING


async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    store = load_store()
    user_entry = store["users"].setdefault(user_id, {
        "stats": {"games_played": 0, "wins": 0, "losses": 0}
    })

    # Проверяем, есть ли активная игра
    if "current_game" not in user_entry:
        await update.message.reply_text("Эту команду можно использовать только во время игры.")
        return ConversationHandler.END

    cg = user_entry["current_game"]

    # Если подсказка уже взята — не даем еще одну
    if cg.get("hint_used", False):
        await update.message.reply_text("Подсказка уже использована в этой игре.")
        return GUESSING

    secret = cg["secret"]
    length = len(secret)

    # Сколько букв нужно подсказать
    hint_counts = {4:1, 5:2, 6:2, 7:3, 8:3, 9:4, 10:4, 11:5}
    num_letters = hint_counts.get(length, 1)

    # Считаем буквы в secret
    secret_counter = Counter(secret)

    # Выбираем кандидатов: разная позиция, но >= num_letters общих символов
    candidates = []
    for w in WORDLIST:
        if len(w) != length or w == secret:
            continue
        w_counter = Counter(w)
        # пересечение счетчиков по минимуму
        common = sum(min(secret_counter[ch], w_counter[ch]) for ch in w_counter)
        if common == num_letters:
            candidates.append(w)

    if not candidates:
        await update.message.reply_text("К сожалению, подходящих подсказок нет.")
        return GUESSING

    hint_word = random.choice(candidates)

    # Отмечаем в JSON, что подсказка взята
    cg["hint_used"] = True
    save_store(store)

    await update.message.reply_text(f"🔍 Подсказка: {hint_word}")
    return GUESSING


async def hint_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщение, если /hint вызвали не во время игры."""
    await update.message.reply_text("Эту команду можно использовать только во время игры.")
    # если сейчас выбираем длину — останемся в ASK_LENGTH, иначе в GUESSING
    return context.user_data.get("state", ASK_LENGTH)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)

    store = load_store()
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)
    if user and "current_game" in user:
        del user["current_game"]
        save_store(store)

    context.user_data.clear()
    await update.message.reply_text("Прогресс сброшен. Жду /play для новой игры.")
    return ConversationHandler.END


async def reset_global(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    await update.message.reply_text("Сейчас нечего сбрасывать — начните игру: /play")


async def notification_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    store = load_store()
    user = store["users"].setdefault(uid, {"stats": {...}})
    # Переключаем
    current = user.get("notify_on_wakeup", True)
    user["notify_on_wakeup"] = not current
    save_store(store)
    state = "включены" if not current else "отключены"
    await update.message.reply_text(f"Уведомления при пробуждении бота {state}.")


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает личную статистику — только вне игры."""
    update_user_activity(update.effective_user)
    store = load_store()
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)
    if not user or "current_game" in user:
        await update.message.reply_text("Эту команду можно использовать только вне игры.")
        return
    s = user.get("stats", {})
    await update.message.reply_text(
        "```"
        f"🧑 Ваши результаты:\n\n"
        f"🎲 Всего игр: {s.get('games_played',0)}\n"
        f"🏆 Побед: {s.get('wins',0)}\n"
        f"💔 Поражений: {s.get('losses',0)}\n"
        f"📊 Процент: {s.get('win_rate',0.0)*100:.2f}%"
        "```",
        parse_mode="Markdown"
    )


async def global_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_activity(update.effective_user)
    """Показывает глобальную статистику — только вне игры."""
    store = load_store()
    g = store["global"]
    # если во время партии — запрет
    uid = str(update.effective_user.id)
    user = store["users"].get(uid)
    if user and "current_game" in user:
        await update.message.reply_text("Эту команду можно использовать только вне игры.")
        return
    
    tp = g.get("top_player", {})
    if tp:
        top_line = f"Сильнейший: @{tp['username']} ({tp['wins']} побед)\n\n"
    else:
        top_line = ""
    
    await update.message.reply_text(
        "```"
        f"🌐 Глобальная статистика:\n\n"
        f"🎲 Всего игр: {g['total_games']}\n"
        f"🏆 Побед: {g['total_wins']}\n"
        f"💔 Поражений: {g['total_losses']}\n"
        f"📊 Процент: {g['win_rate']*100:.2f}%\n\n"
        f"{top_line}"
        "```",
        parse_mode="Markdown"
    )


async def only_outside_game(update, context):
    await update.message.reply_text("Эту команду можно использовать только вне игры.")
    # вернем то состояние, в котором сейчас юзер:
    return context.user_data.get("state", ConversationHandler.END)


async def feedback_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # запретим во время игры
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u:
        await update.message.reply_text(
            "Нельзя отправлять фидбек пока идет игра. Сначала закончи играть или нажми /reset.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    
    if context.user_data.get("game_active"):
        await update.message.reply_text(
            "Нельзя отправлять фидбек пока идет игра. Сначала закончи играть или нажми /reset.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    # предлагаем выбрать список
    keyboard = [
        ["Черный список", "Белый список"],
        ["Отмена"]
    ]
    markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Куда добавить слово?", reply_markup=markup)

    # запомним текущее состояние
    context.user_data["feedback_state"] = FEEDBACK_CHOOSE
    context.user_data["in_feedback"] = True
    return FEEDBACK_CHOOSE


async def feedback_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "Отмена":
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        context.user_data.pop("in_feedback", None)
        context.user_data["just_done"] = True
        return ConversationHandler.END

    if text not in ("Черный список", "Белый список"):
        await update.message.reply_text("Пожалуйста, нажимайте одну из кнопок.")
        return FEEDBACK_CHOOSE

    # куда кладем
    context.user_data["fb_target"] = "black" if text == "Черный список" else "white"
    # убираем клавиатуру и спрашиваем слово
    await update.message.reply_text(
        "Введите слово для предложения:", reply_markup=ReplyKeyboardRemove()
    )

    context.user_data["feedback_state"] = FEEDBACK_WORD
    return FEEDBACK_WORD


async def feedback_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = normalize(update.message.text)
    target = context.user_data["fb_target"]

    if SUGGESTIONS_FILE.exists() and SUGGESTIONS_FILE.stat().st_size >= 1_000_000:
        await update.message.reply_text(
            "Прости, сейчас нельзя добавить новое слово — файл предложений уже слишком большой."
        )
        context.user_data.pop("in_feedback", None)
        context.user_data["just_done"] = True
        return ConversationHandler.END

    suggestions = load_suggestions()

    # Черный список: добавляем, только если слово есть в словаре
    if target == "black":
        if word in WORDLIST:
            suggestions["black"].add(word)
            save_suggestions(suggestions)
            resp = "Спасибо, добавил в предложения для чёрного списка."
        else:
            resp = "Нельзя: слово должно быть в основном словаре."

    # Белый список: добавляем, только если слова нет в словаре и длина 4–11
    else:
        if 4 <= len(word) <= 11 and word not in WORDLIST:
            suggestions["white"].add(word)
            save_suggestions(suggestions)
            resp = "Спасибо, добавил в предложения для белого списка."
        else:
            if word in WORDLIST:
                resp = "Нельзя: такое слово уже есть в основном словаре."
            elif not (4 <= len(word) <= 11):
                resp = "Нельзя: длина слова должна быть от 4 до 11 символов."
            else:
                resp = "Нельзя: слово должно быть вне основного словаря и из 4–11 букв."

    await update.message.reply_text(resp)
    context.user_data.pop("in_feedback", None)
    context.user_data["just_done"] = True
    return ConversationHandler.END


async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def block_during_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # любой посторонний ввод заглушаем
    await update.message.reply_text(
        "Сейчас идет ввод для фидбека, нельзя использовать команды."
    )
    # возвращаемся в текущее состояние
    return context.user_data.get("feedback_state", FEEDBACK_CHOOSE)


async def feedback_not_allowed_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нельзя отправлять фидбек пока вы выбираете длину слова. "
        "Сначала укажите длину (4–11)."
    )
    return ASK_LENGTH


async def feedback_not_allowed_guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Нельзя отправлять фидбек во время игры. "
        "Сначала закончите игру или /reset."
    )
    return GUESSING


async def dict_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Только админу
    if update.effective_user.id != ADMIN_ID:
        return

    # Читаем свежий словарь из base_words.json
    with BASE_FILE.open("r", encoding="utf-8") as f:
        fresh_list = json.load(f)

    total = len(fresh_list)
    data = "\n".join(fresh_list)

    # Считаем количество слов каждой длины (4–11)
    length_counts = Counter(len(w) for w in fresh_list)
    stats_lines = [
        f"{length} букв: {length_counts.get(length, 0)}"
        for length in range(4, 12)
    ]
    stats_text = "\n".join(stats_lines)

    # Упаковываем весь список в файл
    bio = BytesIO(data.encode("utf-8"))
    bio.name = "wordlist.txt"

    # Отправляем документ с общей и детальной статистикой
    await update.message.reply_document(
        document=bio,
        filename="wordlist.txt",
        caption=(
            f"📚 В словаре всего {total} слов.\n\n"
            f"🔢 Распределение по длине:\n{stats_text}"
        )
    )


async def dump_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    path = USER_FILE  # это Path("user_activity.json")
    if not path.exists():
        return await update.message.reply_text("Файл user_activity.json не найден.")

    # прочитаем текст, и если короткий — отправим как сообщение
    content = path.read_text("utf-8")
    if len(content) < 3000:
        # отправляем в кодовом блоке
        return await update.message.reply_text(
            f"<pre>{content}</pre>", parse_mode="HTML"
        )

    # иначе — отправляем как документ
    with path.open("rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=path.name),
            caption="📁 user_activity.json"
        )


async def suggestions_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    if update.effective_user.id != ADMIN_ID:
        return
    sugg = load_suggestions()
    black = sugg.get("black", [])
    white = sugg.get("white", [])
    text = (
        "Предложения для черного списка:\n"
        + (", ".join(f'"{w}"' for w in black) if black else "— пусто")
        + "\n\nПредложения для белого списка:\n"
        + (", ".join(f'"{w}"' for w in white) if white else "— пусто")
    )
    await update.message.reply_text(text)


async def suggestions_remove_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Только админ
    if update.effective_user.id != ADMIN_ID:
        return

    # Блокируем во время игры
    store = load_store()
    u = store["users"].get(str(update.effective_user.id), {})
    if "current_game" in u or context.user_data.get("game_active"):
        await update.message.reply_text("Эту команду можно использовать только вне игры.")
        return ConversationHandler.END

    # Если все ок — запускаем диалог удаления
    await update.message.reply_text(
        "Введи, что удалить (формат):\n"
        "black: слово1, слово2\n"
        "white: слово3, слово4\n\n"
        "Или /cancel для отмены."
    )
    return REMOVE_INPUT


async def suggestions_remove_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    
    context.user_data["in_remove"] = True
    text = update.message.text.strip()
    sugg = load_suggestions()
    removed = {"black": [], "white": []}

    # парсим построчно
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, vals = line.split(":", 1)
        key = key.strip().lower()
        if key not in ("black", "white"):
            continue
        # извлекаем слова через запятую
        words = [w.strip().lower() for w in vals.split(",") if w.strip()]
        for w in words:
            if w in sugg[key]:
                sugg[key].remove(w)
                removed[key].append(w)

    save_suggestions(sugg)

    # формируем ответ
    parts = []
    if removed["black"]:
        parts.append(f'Из черного удалено: {", ".join(removed["black"])}')
    if removed["white"]:
        parts.append(f'Из белого удалено: {", ".join(removed["white"])}')
    if not parts:
        parts = ["Ничего не удалено."]
    await update.message.reply_text("\n".join(parts))
    context.user_data.pop("in_remove", None)
    context.user_data["just_done"] = True
    return ConversationHandler.END


async def suggestions_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    sugg = load_suggestions()  # получаем {'black': set(), 'white': set()}
    # Читаем текущий словарь
    with BASE_FILE.open("r", encoding="utf-8") as f:
        words = set(json.load(f))
    # Удаляем «чёрные»
    words -= sugg["black"]
    # Добавляем «белые»
    words |= sugg["white"]
    # Сортируем и сохраняем
    new_list = sorted(words)
    with BASE_FILE.open("w", encoding="utf-8") as f:
        json.dump(new_list, f, ensure_ascii=False, indent=2)
    # Очищаем suggestions.json
    save_suggestions({"black": set(), "white": set()})
    await update.message.reply_text(
        f"Словарь обновлён: +{len(sugg['white'])}, -{len(sugg['black'])}. Предложения очищены."
    )


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    context.user_data["in_broadcast"] = True
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Введите текст рассылки для всех пользователей:")
    return BROADCAST


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    store = load_store()      # берем тех, кого мы когда-то записали
    failed = []
    for uid in store["users"].keys():
        try:
            await context.bot.send_message(chat_id=int(uid), text=text)
        except Exception:
            failed.append(uid)
    msg = "✅ Рассылка успешно отправлена!"
    if failed:
        msg += f"\nНе удалось доставить сообщения пользователям: {', '.join(failed)}"
    await update.message.reply_text(msg)
    context.user_data.pop("in_broadcast", None)
    context.user_data["just_done"] = True
    return ConversationHandler.END


async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Рассылка отменена.")
    context.user_data.pop("in_broadcast", None)
    return ConversationHandler.END


def main():
    
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN не установлен")
        return

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(set_commands)
        .build()
    )
	
    store = load_store()

    # отправляем один раз при загрузке
    app.job_queue.run_once(send_activity_periodic, when=0)
    app.job_queue.run_once(send_unfinished_games, when=1)


    feedback_conv = ConversationHandler(
    entry_points=[CommandHandler("feedback", feedback_start)],
    states={
        FEEDBACK_CHOOSE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_choose),
            MessageHandler(filters.ALL, block_during_feedback),
        ],
        FEEDBACK_WORD: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_word),
            MessageHandler(filters.ALL, block_during_feedback),
        ],
    },
    fallbacks=[CommandHandler("cancel", feedback_cancel)],
    allow_reentry=True
    )
    app.add_handler(feedback_conv)
    
    
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("play", ask_length),
            CommandHandler("start", start),
        ],
        states={
            ASK_LENGTH: [
                CommandHandler("feedback", feedback_not_allowed_ask),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_length),
                CommandHandler("start", ignore_ask),
                CommandHandler("play", ignore_ask),
                CommandHandler("hint", hint_not_allowed),
                CommandHandler("reset", reset),
                CommandHandler("my_stats", only_outside_game),
                CommandHandler("global_stats", only_outside_game),
            ],
            GUESSING: [
                CommandHandler("feedback", feedback_not_allowed_guess),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess),
                CommandHandler("start", ignore_guess),
		        CommandHandler("play", ignore_guess),
                CommandHandler("hint", hint),
                CommandHandler("reset", reset),
                CommandHandler("my_stats", only_outside_game),
                CommandHandler("global_stats", only_outside_game),
            ],
        },
        fallbacks=[
            CommandHandler("reset", reset),
       ],
    )
    app.add_handler(conv)

    # 1) просмотр и подтверждение предложений
    app.add_handler(CommandHandler("suggestions_view", suggestions_view))
    app.add_handler(CommandHandler("suggestions_approve", suggestions_approve))

    # 2) удаление через ConversationHandler
    remove_conv = ConversationHandler(
        entry_points=[CommandHandler("suggestions_remove", suggestions_remove_start)],
        states={
            REMOVE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, suggestions_remove_process),
            ],
        },
        fallbacks=[CommandHandler("cancel", feedback_cancel)],
        allow_reentry=True,
    )
    app.add_handler(remove_conv)

    broadcast_conv = ConversationHandler(
    entry_points=[CommandHandler("broadcast", broadcast_start)],
    states={
        BROADCAST: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send),
        ],
    },
    fallbacks=[CommandHandler("broadcast_cancel", broadcast_cancel)],
    allow_reentry=True,
    )
    app.add_handler(broadcast_conv)

    app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text),
    group=99
    )

    # Глобальные
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hint", hint_not_allowed))
    app.add_handler(CommandHandler("reset", reset_global))
    app.add_handler(CommandHandler("notification", notification_toggle))
    app.add_handler(CommandHandler("my_stats", my_stats))
    app.add_handler(CommandHandler("global_stats", global_stats))
    app.add_handler(CommandHandler("dict_file", dict_file))
    app.add_handler(CommandHandler("dump_activity", dump_activity))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
