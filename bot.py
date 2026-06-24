import os
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.path.join(os.path.dirname(__file__), "rieltor.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

YARO_BUYER = """Ты — Яро, персональный помощник в поиске недвижимости. Ты общаешься с клиентом риелтора.

Твой характер:
- Тёплый, живой, искренний — не корпоративный бот
- Помнишь всё что клиент рассказывал раньше: бюджет, район, пожелания
- Задаёшь уточняющие вопросы чтобы лучше понять потребность
- Никогда не давишь и не торопишь — человек сам придёт к решению
- Информируешь о рынке легко, без занудства

Правила:
- Не говори что ты бот или AI — ты просто Яро, помощник
- Говори коротко (2-4 предложения), живым языком
- Обращайся к клиенту по имени
- Если клиент ищет нежилое (офис, торговую площадь, склад) — уточняй: площадь, этаж, назначение, парковка
- Если клиент спрашивает про конкретный объект — запомни детали
- Иногда в конце задай один вопрос чтобы поддержать разговор

Параметры клиента (используй в разговоре):
{client_params}"""

YARO_SELLER = """Ты — Яро, персональный помощник по продаже недвижимости. Ты общаешься с клиентом риелтора который продаёт объект.

Твой характер:
- Тёплый, поддерживающий — продажа недвижимости это стресс, человек ждёт и нервничает
- Помнишь всё об объекте клиента: адрес, цена, тип, площадь
- Помогаешь клиенту не терять терпение — объясняешь что долгое ожидание это норма рынка
- Уточняешь готовность к торгу, срочность продажи, есть ли обременения
- Сообщаешь когда появляется интерес со стороны покупателей (риелтор сам передаст эту инфу)

Правила:
- Не говори что ты бот или AI — ты просто Яро, помощник
- Говори коротко (2-4 предложения), живым языком
- Обращайся к клиенту по имени
- Не давай юридических советов — направляй к риелтору
- Иногда в конце задай один вопрос чтобы поддержать разговор

Параметры объекта клиента (используй в разговоре):
{client_params}"""

# ── База данных ───────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS realtors (
            realtor_id      INTEGER PRIMARY KEY,
            username        TEXT,
            name            TEXT,
            active          INTEGER DEFAULT 1,
            created_at      REAL DEFAULT (unixepoch())
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            realtor_id      INTEGER NOT NULL,
            chat_id         INTEGER,
            name            TEXT NOT NULL,
            role            TEXT DEFAULT 'buyer',
            budget          TEXT,
            district        TEXT,
            rooms           TEXT,
            address         TEXT,
            area            TEXT,
            price           TEXT,
            notes           TEXT,
            history         TEXT DEFAULT '[]',
            msg_count_week  INTEGER DEFAULT 0,
            last_active     REAL DEFAULT 0,
            last_proactive  REAL DEFAULT 0,
            invite_token    TEXT UNIQUE,
            created_at      REAL DEFAULT (unixepoch()),
            FOREIGN KEY (realtor_id) REFERENCES realtors(realtor_id)
        )
    """)
    # Миграция: добавляем новые колонки если их нет (для существующей БД)
    # Миграция таблицы realtors
    for col, coltype in [("approved","INTEGER DEFAULT 0")]:
        try:
            conn.execute(f"ALTER TABLE realtors ADD COLUMN {col} {coltype}")
        except Exception:
            pass
    for col, coltype in [("role","TEXT DEFAULT 'buyer'"), ("address","TEXT"), ("area","TEXT"),
                         ("price","TEXT"), ("status","TEXT DEFAULT 'active'")]:
        try:
            conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {coltype}")
        except Exception:
            pass
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB_PATH)

def get_realtor(realtor_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM realtors WHERE realtor_id=?", (realtor_id,)).fetchone()
    return row

def ensure_realtor(user):
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM realtors WHERE realtor_id=?", (user.id,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO realtors (realtor_id, username, name, approved) VALUES (?,?,?,0)",
                (user.id, user.username or "", user.full_name)
            )
            conn.commit()

def is_approved(realtor_id):
    with get_conn() as conn:
        row = conn.execute("SELECT approved FROM realtors WHERE realtor_id=?", (realtor_id,)).fetchone()
    return row and row[0] == 1

def approve_realtor(realtor_id):
    with get_conn() as conn:
        conn.execute("UPDATE realtors SET approved=1 WHERE realtor_id=?", (realtor_id,))
        conn.commit()

def add_client(realtor_id, name, budget, district, rooms, notes=""):
    import secrets
    token = secrets.token_urlsafe(12)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO clients (realtor_id, name, role, budget, district, rooms, notes, invite_token)
               VALUES (?,?,?,?,?,?,?,?)""",
            (realtor_id, name, "buyer", budget, district, rooms, notes, token)
        )
        conn.commit()
    return token

def add_seller(realtor_id, name, price, address, area, rooms, notes=""):
    import secrets
    token = secrets.token_urlsafe(12)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO clients (realtor_id, name, role, price, address, area, rooms, notes, invite_token)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (realtor_id, name, "seller", price, address, area, rooms, notes, token)
        )
        conn.commit()
    return token

def get_clients(realtor_id, status="active"):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, name, role, budget, district, rooms, price, address, area, chat_id, last_active, msg_count_week
               FROM clients WHERE realtor_id=? AND (status=? OR status IS NULL) ORDER BY created_at DESC""",
            (realtor_id, status)
        ).fetchall()
    return rows

def get_client_by_token(token):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE invite_token=?", (token,)).fetchone()
    return row

def get_client_by_chat(chat_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE chat_id=?", (chat_id,)).fetchone()
    return row

def get_client_by_id(client_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    return row

def link_client(token, chat_id):
    with get_conn() as conn:
        conn.execute("UPDATE clients SET chat_id=? WHERE invite_token=?", (chat_id, token))
        conn.commit()

def save_message(client_id, role, text):
    with get_conn() as conn:
        row = conn.execute("SELECT history FROM clients WHERE id=?", (client_id,)).fetchone()
        history = json.loads(row[0]) if row else []
        history.append({"role": role, "content": text})
        if len(history) > 40:
            history = history[-40:]
        now = time.time()
        conn.execute(
            "UPDATE clients SET history=?, last_active=?, msg_count_week=msg_count_week+1 WHERE id=?",
            (json.dumps(history, ensure_ascii=False), now, client_id)
        )
        conn.commit()
    return history

def get_history(client_id):
    with get_conn() as conn:
        row = conn.execute("SELECT history FROM clients WHERE id=?", (client_id,)).fetchone()
    return json.loads(row[0]) if row else []

# ── Claude ────────────────────────────────────────────────────────────────────

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def build_params(client_row):
    cols = ["id","realtor_id","chat_id","name","role","budget","district","rooms",
            "address","area","price","notes","history","msg_count_week",
            "last_active","last_proactive","invite_token","created_at"]
    c = dict(zip(cols, client_row))
    parts = [f"Имя: {c['name']}"]
    if c.get("role") == "seller":
        if c.get("price"):   parts.append(f"Цена продажи: {c['price']}")
        if c.get("address"): parts.append(f"Адрес объекта: {c['address']}")
        if c.get("area"):    parts.append(f"Площадь: {c['area']}")
        if c.get("rooms"):   parts.append(f"Тип объекта: {c['rooms']}")
    else:
        if c.get("budget"):  parts.append(f"Бюджет: {c['budget']}")
        if c.get("district"):parts.append(f"Район: {c['district']}")
        if c.get("rooms"):   parts.append(f"Тип объекта: {c['rooms']}")
    if c.get("notes"):   parts.append(f"Заметки: {c['notes']}")
    return "\n".join(parts)

def ask_claude(client_row, history):
    params = build_params(client_row)
    cols = ["id","realtor_id","chat_id","name","role","budget","district","rooms",
            "address","area","price","notes","history","msg_count_week",
            "last_active","last_proactive","invite_token","created_at"]
    c = dict(zip(cols, client_row))
    persona = YARO_SELLER if c.get("role") == "seller" else YARO_BUYER
    system = persona.format(client_params=params)
    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=history
    )
    return response.content[0].text

# ── Команды риелтора ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Проверяем — это клиент по invite-токену?
    args = ctx.args
    if args:
        token = args[0]
        client = get_client_by_token(token)
        if client:
            link_client(token, user.id)
            cols = ["id","realtor_id","chat_id","name","role","budget","district","rooms",
                    "address","area","price","notes","history","msg_count_week",
                    "last_active","last_proactive","invite_token","created_at"]
            c = dict(zip(cols, client))
            if c.get("role") == "seller":
                await update.message.reply_text(
                    f"Привет, {c['name']}! 👋\n\nЯ Яро — помогаю риелтору держать тебя в курсе "
                    f"пока идёт продажа. Знаю что ожидание бывает нервным — буду рядом.\n\n"
                    f"Как сейчас настроение по поводу продажи? Всё идёт как планировали?"
                )
            else:
                await update.message.reply_text(
                    f"Привет, {c['name']}! 👋\n\nЯ Яро — твой помощник в поиске квартиры. "
                    f"Буду на связи и помогу не пропустить подходящий вариант.\n\n"
                    f"Расскажи — как сейчас дела с поиском? Что-то смотрел, что понравилось или нет?"
                )
            return

    # Иначе — регистрируем как риелтора
    ensure_realtor(user)
    if not is_approved(user.id):
        await update.message.reply_text(
            f"Привет, {user.first_name}! 👋\n\n"
            f"Твоя заявка на доступ к Агентуре отправлена.\n"
            f"Как только тебя одобрят — придёт уведомление."
        )
        if ADMIN_ID:
            username = f"@{user.username}" if user.username else "нет username"
            await ctx.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🆕 Новый риелтор хочет доступ:\n\n"
                     f"Имя: {user.full_name}\n"
                     f"Username: {username}\n"
                     f"ID: {user.id}\n\n"
                     f"Одобрить: /approve {user.id}\n"
                     f"Отклонить: /reject {user.id}"
            )
        return
    await update.message.reply_text(
        f"Привет, {user.first_name}! 👋\n\n"
        f"Я Яро — твой AI-помощник для работы с клиентами.\n\n"
        f"Что умею:\n"
        f"• Веду тёплые разговоры с твоими клиентами\n"
        f"• Помню их пожелания и слежу за интересом\n"
        f"• Сообщаю тебе когда клиент «оживился»\n\n"
        f"Команды:\n"
        f"/add Имя бюджет район тип — покупатель\n"
        f"/sell Имя цена адрес площадь тип — продавец\n"
        f"/clients — список клиентов\n"
        f"/chat ID — история переписки\n"
        f"/help — подробная справка"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Команды риелтора:\n\n"
        "🔵 ПОКУПАТЕЛЬ:\n"
        "/add Иван 5млн Советский 3к\n"
        "  └ формат: имя | бюджет | район | тип\n"
        "  └ типы: 1к / 2к / 3к / студия / нежилое / офис\n\n"
        "🟠 ПРОДАВЕЦ:\n"
        "/sell Мария 8млн Ленина_12 65м2 3к\n"
        "  └ формат: имя | цена | адрес | площадь | тип\n\n"
        "/clients — список активных клиентов\n"
        "/chat 3 — история переписки с клиентом #3\n"
        "/done 3 — закрыть сделку, клиент уходит в архив\n"
        "/archive — все закрытые сделки\n"
        "/note 3 текст — добавить заметку\n\n"
        "После /add или /sell получишь ссылку — отправь клиенту."
    )

async def cmd_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_realtor(user)
    args = ctx.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "Формат: /sell Имя цена адрес площадь тип\n"
            "Пример: /sell Мария 8млн Ленина_12 65м2 3к"
        )
        return
    name    = args[0]
    price   = args[1] if len(args) > 1 else ""
    address = args[2].replace("_", " ") if len(args) > 2 else ""
    area    = args[3] if len(args) > 3 else ""
    rooms   = args[4] if len(args) > 4 else ""
    token = add_seller(user.id, name, price, address, area, rooms)
    bot_info = await ctx.bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={token}"
    await update.message.reply_text(
        f"✅ Продавец {name} добавлен!\n\n"
        f"Отправь ему эту ссылку:\n{invite_link}\n\n"
        f"Яро будет поддерживать контакт и помогать не нервничать в ожидании покупателя."
    )

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_realtor(user)
    args = ctx.args
    if not args or len(args) < 1:
        await update.message.reply_text(
            "Формат: /add Имя бюджет район тип\n"
            "Пример: /add Иван 5млн Советский 3к"
        )
        return
    name = args[0]
    budget   = args[1] if len(args) > 1 else ""
    district = args[2] if len(args) > 2 else ""
    rooms    = args[3] if len(args) > 3 else ""
    token = add_client(user.id, name, budget, district, rooms)
    bot_info = await ctx.bot.get_me()
    invite_link = f"https://t.me/{bot_info.username}?start={token}"
    await update.message.reply_text(
        f"✅ Клиент {name} добавлен!\n\n"
        f"Отправь ему эту ссылку:\n{invite_link}\n\n"
        f"Как только он перейдёт по ней — Яро начнёт общение."
    )

async def cmd_clients(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_realtor(user)
    clients = get_clients(user.id)
    if not clients:
        await update.message.reply_text("Клиентов пока нет. Добавь первого: /add Имя бюджет район тип")
        return
    lines = ["👥 Твои клиенты:\n"]
    for c in clients:
        cid, name, role, budget, district, rooms, price, address, area, chat_id, last_active, msg_week = c
        status = "🟢 подключён" if chat_id else "⏳ ждёт"
        active_str = ""
        if last_active:
            days_ago = (time.time() - last_active) / 86400
            if days_ago < 1:
                active_str = " · был сегодня"
            elif days_ago < 7:
                active_str = f" · {int(days_ago)}д назад"
        heat = "🔥" if msg_week >= 3 else ""
        role_icon = "🟠 продаёт" if role == "seller" else "🔵 ищет"
        if role == "seller":
            params = " | ".join(filter(None, [price, address, area, rooms]))
        else:
            params = " | ".join(filter(None, [budget, district, rooms]))
        lines.append(f"#{cid} {name} {heat} — {role_icon}\n   {status}{active_str}\n   {params}\n   /chat {cid}")
    await update.message.reply_text("\n".join(lines))

async def cmd_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args
    if not args:
        await update.message.reply_text("Укажи ID клиента: /chat 3")
        return
    try:
        client_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /chat 3")
        return
    client = get_client_by_id(client_id)
    if not client or client[1] != user.id:
        await update.message.reply_text("Клиент не найден.")
        return
    history = get_history(client_id)
    if not history:
        await update.message.reply_text(f"Клиент #{client_id} ещё не начал общение.")
        return
    lines = [f"💬 История с клиентом #{client_id} ({client[3]}):\n"]
    for msg in history[-10:]:
        role_label = "Яро" if msg["role"] == "assistant" else client[3]
        lines.append(f"[{role_label}]: {msg['content'][:200]}")
    await update.message.reply_text("\n\n".join(lines))

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args
    if not args:
        await update.message.reply_text("Укажи ID клиента: /done 3")
        return
    try:
        client_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом: /done 3")
        return
    client = get_client_by_id(client_id)
    if not client or client[1] != user.id:
        await update.message.reply_text("Клиент не найден.")
        return
    with get_conn() as conn:
        conn.execute("UPDATE clients SET status='closed' WHERE id=?", (client_id,))
        conn.commit()
    name = client[3]
    await update.message.reply_text(
        f"✅ Сделка с {name} закрыта!\n\n"
        f"Клиент перемещён в архив. История переписки сохранена.\n"
        f"/archive — посмотреть все закрытые сделки"
    )

async def cmd_archive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_realtor(user)
    clients = get_clients(user.id, status="closed")
    if not clients:
        await update.message.reply_text("Архив пуст — закрытых сделок пока нет.")
        return
    lines = [f"📁 Архив закрытых сделок ({len(clients)}):\n"]
    for c in clients:
        cid, name, role, budget, district, rooms, price, address, area, chat_id, last_active, msg_week = c
        role_icon = "🟠 продавец" if role == "seller" else "🔵 покупатель"
        if role == "seller":
            params = " | ".join(filter(None, [price, address, rooms]))
        else:
            params = " | ".join(filter(None, [budget, district, rooms]))
        lines.append(f"#{cid} {name} — {role_icon}\n   {params}\n   /chat {cid}")
    await update.message.reply_text("\n\n".join(lines))

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Формат: /approve 123456789")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    realtor = get_realtor(target_id)
    if not realtor:
        await update.message.reply_text("Риелтор не найден.")
        return
    approve_realtor(target_id)
    await update.message.reply_text(f"✅ Риелтор {realtor[2]} одобрен.")
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text=f"✅ Доступ открыт!\n\n"
                 f"Добро пожаловать в Агентуру.\n\n"
                 f"Команды:\n"
                 f"/add Имя бюджет район тип — покупатель\n"
                 f"/sell Имя цена адрес площадь тип — продавец\n"
                 f"/clients — список клиентов\n"
                 f"/help — подробная справка"
        )
    except Exception as e:
        log.warning(f"Не удалось уведомить риелтора {target_id}: {e}")

async def cmd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Формат: /reject 123456789")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    realtor = get_realtor(target_id)
    if not realtor:
        await update.message.reply_text("Риелтор не найден.")
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM realtors WHERE realtor_id=?", (target_id,))
        conn.commit()
    await update.message.reply_text(f"❌ Риелтор {realtor[2]} отклонён и удалён.")
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text="К сожалению, доступ не одобрен. Свяжитесь с администратором."
        )
    except Exception:
        pass

async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Формат: /note 3 Хочет рядом со школой")
        return
    try:
        client_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    note_text = " ".join(args[1:])
    client = get_client_by_id(client_id)
    if not client or client[1] != user.id:
        await update.message.reply_text("Клиент не найден.")
        return
    with get_conn() as conn:
        conn.execute("UPDATE clients SET notes=? WHERE id=?", (note_text, client_id))
        conn.commit()
    await update.message.reply_text(f"✅ Заметка сохранена для {client[3]}.")

# ── Клиентский режим (чат с Яро) ─────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    # Проверяем — риелтор ли это
    realtor = get_realtor(user.id)
    if realtor:
        if not is_approved(user.id):
            await update.message.reply_text(
                "Твоя заявка ещё на рассмотрении. Ожидай уведомления."
            )
            return
        await update.message.reply_text(
            "Используй команды для управления клиентами.\n/help — список команд."
        )
        return

    # Клиентский режим
    client = get_client_by_chat(user.id)
    if not client:
        await update.message.reply_text(
            "Привет! Чтобы начать, перейди по ссылке которую прислал твой риелтор."
        )
        return

    client_id = client[0]
    history = save_message(client_id, "user", text)

    # Проверяем активность — уведомляем риелтора если горячий
    week_count = client[9] + 1
    if week_count >= 3 and week_count % 3 == 0:
        realtor_id = client[1]
        client_name = client[3]
        try:
            await ctx.bot.send_message(
                chat_id=realtor_id,
                text=f"🔥 {client_name} оживился! Написал {week_count} сообщений на этой неделе.\n"
                     f"Хорошее время чтобы позвонить.\n/chat {client_id}"
            )
        except Exception as e:
            log.warning(f"Не удалось уведомить риелтора {realtor_id}: {e}")

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        reply = ask_claude(client, history)
    except Exception as e:
        log.error(f"Claude error: {e}")
        reply = "Прости, что-то пошло не так. Напиши чуть позже!"

    save_message(client_id, "assistant", reply)
    await update.message.reply_text(reply)

# ── Планировщик проактивных сообщений ────────────────────────────────────────

async def send_proactive(bot):
    threshold = time.time() - 10 * 86400  # клиенты без контакта 10+ дней
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, realtor_id, chat_id, name, budget, district, rooms, notes,
                      history, msg_count_week, last_active, last_proactive, invite_token, created_at
               FROM clients
               WHERE chat_id IS NOT NULL
                 AND last_proactive < ?
                 AND last_active < ?""",
            (threshold, threshold)
        ).fetchall()

    for client in rows:
        client_id = client[0]
        chat_id = client[2]
        name = client[3]
        try:
            history = get_history(client_id)
            history.append({"role": "user", "content": f"[Это автоматический check-in. Напиши {name} тёплое короткое сообщение чтобы узнать как дела с поиском квартиры. Не давить, просто живое участие.]"})
            params = build_params(client)
            system = YARO_PERSONA.format(client_params=params)
            response = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                system=system,
                messages=history
            )
            msg = response.content[0].text
            await bot.send_message(chat_id=chat_id, text=msg)
            save_message(client_id, "assistant", msg)
            with get_conn() as conn:
                conn.execute("UPDATE clients SET last_proactive=? WHERE id=?", (time.time(), client_id))
                conn.commit()
            log.info(f"Proactive message sent to client {client_id} ({name})")
        except Exception as e:
            log.warning(f"Proactive failed for client {client_id}: {e}")

def reset_weekly_counts():
    with get_conn() as conn:
        conn.execute("UPDATE clients SET msg_count_week=0")
        conn.commit()

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("chat", cmd_chat))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("archive", cmd_archive))
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_proactive, "interval", hours=12, args=[app.bot])
    scheduler.add_job(reset_weekly_counts, "cron", day_of_week="mon", hour=0)
    scheduler.start()

    log.info("Яро.Риелтор запущен")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
