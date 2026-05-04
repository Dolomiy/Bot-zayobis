"""
Telegram-бот контролю обробки повернень на складі.
Запуск: python bot.py
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Налаштування логування
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("returns_bot")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Файловий обробник із ротацією (5 МБ × 3 файли)
    fh = RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Консольний обробник
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# Завантаження конфігурації
# ---------------------------------------------------------------------------

load_dotenv()

REQUIRED_VARS = [
    "BOT_TOKEN",
    "GROUP_CHAT_ID",
    "THREAD_ID",
    "RESPONSIBLE_USER_IDS",
    "RESPONSIBLE_USER_NAMES",
    "ADMIN_USER_IDS",
    "WEEKDAY_DEADLINES",
    "WEEKEND_DEADLINES",
    "TIMEZONE",
]


def load_config() -> dict:
    missing = [v for v in REQUIRED_VARS if not os.getenv(v)]
    if missing:
        log.error("Відсутні обов'язкові змінні середовища: %s", ", ".join(missing))
        sys.exit(1)

    def parse_ids(key: str) -> list[int]:
        return [int(x.strip()) for x in os.environ[key].split(",") if x.strip()]

    def parse_times(key: str) -> list[str]:
        return [t.strip() for t in os.environ[key].split(",") if t.strip()]

    tz_name = os.environ["TIMEZONE"]
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.error("Невідома таймзона: %s", tz_name)
        sys.exit(1)

    return {
        "BOT_TOKEN": os.environ["BOT_TOKEN"],
        "GROUP_CHAT_ID": int(os.environ["GROUP_CHAT_ID"]),
        "THREAD_ID": int(os.environ["THREAD_ID"]),
        "RESPONSIBLE_USER_IDS": parse_ids("RESPONSIBLE_USER_IDS"),
        # @username теги для публічних згадок у гілці
        "RESPONSIBLE_USER_NAMES": parse_times("RESPONSIBLE_USER_NAMES"),
        "ADMIN_USER_IDS": parse_ids("ADMIN_USER_IDS"),
        # Будні (пн–пт): 11:00, 14:00, 17:00, 20:00
        "WEEKDAY_DEADLINES": parse_times("WEEKDAY_DEADLINES"),
        # Вихідні (сб–нд): 11:00, 12:00, 18:00
        "WEEKEND_DEADLINES": parse_times("WEEKEND_DEADLINES"),
        "PRE_REMIND_MINUTES": int(os.getenv("PRE_REMIND_MINUTES", "60")),
        "FINAL_REMIND_MINUTES": int(os.getenv("FINAL_REMIND_MINUTES", "30")),
        # Якщо true — вихідні повністю пропускаються (WEEKEND_DEADLINES ігноруються)
        "WORKDAYS_ONLY": os.getenv("WORKDAYS_ONLY", "false").lower() == "true",
        "TIMEZONE": tz,
        "DB_PATH": os.getenv("DB_PATH", "returns.db"),
    }


CFG: dict = {}  # заповнюється у main()

# ---------------------------------------------------------------------------
# База даних
# ---------------------------------------------------------------------------

DB_PATH = ""  # визначається після завантаження CFG


async def init_db() -> None:
    """Ініціалізація схеми БД."""
    async with aiosqlite.connect(DB_PATH) as db:
        with open("schema.sql", encoding="utf-8") as f:
            await db.executescript(f.read())
        await db.commit()
    log.info("БД ініціалізовано: %s", DB_PATH)


async def ensure_task(task_date: str, deadline_time: str) -> int:
    """Повертає id рядка tasks, створює якщо немає."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO tasks (date, deadline_time) VALUES (?, ?)",
            (task_date, deadline_time),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM tasks WHERE date=? AND deadline_time=?",
            (task_date, deadline_time),
        ) as cur:
            row = await cur.fetchone()
            return row[0]


async def get_task(task_date: str, deadline_time: str) -> dict | None:
    """Отримати рядок task як dict або None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE date=? AND deadline_time=?",
            (task_date, deadline_time),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def set_notified_pre(task_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET notified_pre=1 WHERE id=?", (task_id,))
        await db.commit()


async def set_notified_final(task_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET notified_final=1 WHERE id=?", (task_id,))
        await db.commit()


async def mark_done(task_id: int, photo_file_id: str, user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE tasks
               SET status='done', photo_file_id=?, confirmed_by_user_id=?, confirmed_at=?
               WHERE id=?""",
            (photo_file_id, user_id, datetime.utcnow().isoformat(), task_id),
        )
        await db.commit()


async def mark_missed(task_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET status='missed' WHERE id=?", (task_id,))
        await db.commit()


async def mark_skipped(task_id: int, reason: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tasks SET status='skipped', skip_reason=? WHERE id=?",
            (reason, task_id),
        )
        await db.commit()


async def get_tasks_for_date(task_date: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tasks WHERE date=? ORDER BY deadline_time",
            (task_date,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    """Поточний час у налаштованій таймзоні."""
    return datetime.now(CFG["TIMEZONE"])


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def is_workday(dt: datetime | None = None) -> bool:
    """Повертає True якщо день — пн–пт."""
    return (dt or now_local()).weekday() < 5


def get_todays_deadlines() -> list[str]:
    """Повертає список дедлайнів залежно від типу дня."""
    if CFG["WORKDAYS_ONLY"] and not is_workday():
        return []
    return CFG["WEEKDAY_DEADLINES"] if is_workday() else CFG["WEEKEND_DEADLINES"]


def deadline_dt(dl_time: str, ref_date: date | None = None) -> datetime:
    """Повертає datetime дедлайну у локальній TZ."""
    if ref_date is None:
        ref_date = now_local().date()
    h, m = map(int, dl_time.split(":"))
    return datetime(ref_date.year, ref_date.month, ref_date.day, h, m, tzinfo=CFG["TIMEZONE"])


def format_status_icon(status: str) -> str:
    return {"done": "✅", "pending": "⏳", "missed": "❌", "skipped": "⏭️"}.get(status, "❓")


def mention_users() -> str:
    """Рядок з @username тегами відповідальних."""
    return " ".join(CFG["RESPONSIBLE_USER_NAMES"])


# ---------------------------------------------------------------------------
# Планувальник завдань
# ---------------------------------------------------------------------------

def schedule_day_jobs(app: Application) -> None:
    """Планує завдання на сьогоднішній день."""
    deadlines = get_todays_deadlines()
    if not deadlines:
        log.info("Сьогодні вихідний і WORKDAYS_ONLY=true — завдання не плануються.")
        return

    day_label = "будній" if is_workday() else "вихідний"
    log.info("Планування на %s день: %s", day_label, deadlines)

    now = now_local()
    jq = app.job_queue
    pre_min = CFG["PRE_REMIND_MINUTES"]
    final_min = CFG["FINAL_REMIND_MINUTES"]

    for dl_time in deadlines:
        dl = deadline_dt(dl_time)

        # Перше нагадування
        pre_time = dl - timedelta(minutes=pre_min)
        if pre_time > now:
            jq.run_once(
                job_pre_remind,
                when=pre_time,
                name=f"pre_{dl_time}",
                data=dl_time,
            )
            log.info("Заплановано перше нагадування %s о %s", dl_time, pre_time.strftime("%H:%M"))

        # Друге нагадування
        final_time = dl - timedelta(minutes=final_min)
        if final_time > now:
            jq.run_once(
                job_final_remind,
                when=final_time,
                name=f"final_{dl_time}",
                data=dl_time,
            )
            log.info("Заплановано друге нагадування %s о %s", dl_time, final_time.strftime("%H:%M"))

        # Перевірка на дедлайн
        if dl > now:
            jq.run_once(
                job_check_deadline,
                when=dl,
                name=f"check_{dl_time}",
                data=dl_time,
            )
            log.info("Заплановано перевірку дедлайну %s о %s", dl_time, dl.strftime("%H:%M"))

    # Щодня о 00:01 перепланувати завдання на наступний день
    tomorrow_midnight = now.replace(hour=0, minute=1, second=0, microsecond=0) + timedelta(days=1)
    jq.run_once(
        job_reschedule_day,
        when=tomorrow_midnight,
        name="daily_reschedule",
        data=None,
    )


async def job_pre_remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перше нагадування за PRE_REMIND_MINUTES до дедлайну."""
    dl_time: str = context.job.data
    task_id = await ensure_task(today_str(), dl_time)
    task = await get_task(today_str(), dl_time)

    if task and task["status"] in ("done", "skipped"):
        return
    if task and task["notified_pre"]:
        return

    log.info("Надсилаю перше нагадування для дедлайну %s", dl_time)
    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=f"⏰ <b>Нагадування</b> (за {CFG['PRE_REMIND_MINUTES']} хв до {dl_time})\n\nОбробіть повернення.",
        parse_mode=ParseMode.HTML,
    )
    await set_notified_pre(task_id)


async def job_final_remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Друге нагадування за FINAL_REMIND_MINUTES до дедлайну."""
    dl_time: str = context.job.data
    task_id = await ensure_task(today_str(), dl_time)
    task = await get_task(today_str(), dl_time)

    if task and task["status"] in ("done", "skipped"):
        return
    if task and task["notified_final"]:
        return

    log.info("Надсилаю друге нагадування для дедлайну %s", dl_time)
    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=(
            f"⚠️ <b>Термінове нагадування</b> (за {CFG['FINAL_REMIND_MINUTES']} хв до {dl_time})\n\n"
            "Обробіть повернення та надайте фото.\n\n"
            "<i>Надішліть фото підтвердження у цю гілку.</i>"
        ),
        parse_mode=ParseMode.HTML,
    )
    await set_notified_final(task_id)


async def job_check_deadline(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перевірка виконання на момент дедлайну."""
    dl_time: str = context.job.data
    task_id = await ensure_task(today_str(), dl_time)
    task = await get_task(today_str(), dl_time)

    if task and task["status"] in ("done", "skipped"):
        log.info("Дедлайн %s — вже виконано/пропущено.", dl_time)
        return

    log.info("Дедлайн %s — фото не надійшло, позначаю як missed.", dl_time)
    await mark_missed(task_id)

    mentions = mention_users()

    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=(
            f"❌ <b>Дедлайн {dl_time} прострочено!</b>\n\n"
            f"{mentions}\n"
            "Повернення не оброблено вчасно."
        ),
        parse_mode=ParseMode.HTML,
    )


async def job_reschedule_day(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Щоденне перепланування завдань на новий день."""
    log.info("Перепланування завдань на новий день.")
    schedule_day_jobs(context.application)


# ---------------------------------------------------------------------------
# Відновлення стану після рестарту
# ---------------------------------------------------------------------------

async def restore_today_tasks(app: Application) -> None:
    """Відновлює активні слоти на сьогодні після перезапуску."""
    deadlines = get_todays_deadlines()
    if not deadlines:
        log.info("Відновлення: сьогодні вихідний і WORKDAYS_ONLY=true.")
        return

    today = today_str()
    now = now_local()
    pre_min = CFG["PRE_REMIND_MINUTES"]
    final_min = CFG["FINAL_REMIND_MINUTES"]
    jq = app.job_queue

    for dl_time in deadlines:
        dl = deadline_dt(dl_time)
        task = await get_task(today, dl_time)

        if task and task["status"] in ("done", "skipped", "missed"):
            log.info("Відновлення %s: статус %s — пропускаємо.", dl_time, task["status"])
            continue

        pre_time = dl - timedelta(minutes=pre_min)
        final_time = dl - timedelta(minutes=final_min)

        if pre_time > now:
            jq.run_once(job_pre_remind, when=pre_time, name=f"pre_{dl_time}", data=dl_time)
            log.info("Відновлено: перше нагадування %s о %s", dl_time, pre_time.strftime("%H:%M"))

        if final_time > now:
            jq.run_once(job_final_remind, when=final_time, name=f"final_{dl_time}", data=dl_time)
            log.info("Відновлено: друге нагадування %s о %s", dl_time, final_time.strftime("%H:%M"))

        if dl > now:
            jq.run_once(job_check_deadline, when=dl, name=f"check_{dl_time}", data=dl_time)
            log.info("Відновлено: перевірка дедлайну %s о %s", dl_time, dl.strftime("%H:%M"))
        elif task is None or task["status"] == "pending":
            # Дедлайн минув під час офлайну — фіксуємо missed
            task_id = await ensure_task(today, dl_time)
            await mark_missed(task_id)
            log.info("Відновлення: дедлайн %s минув офлайн — позначено missed.", dl_time)


# ---------------------------------------------------------------------------
# Обробка фотографій
# ---------------------------------------------------------------------------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка вхідних фото у гілці групи."""
    msg = update.message
    if msg is None:
        return

    user_id = msg.from_user.id if msg.from_user else None

    # Ігноруємо фото не від відповідальних мовчки
    if user_id not in CFG["RESPONSIBLE_USER_IDS"]:
        return

    # Фото повинно бути надіслано саме в наш THREAD_ID
    if msg.message_thread_id != CFG["THREAD_ID"]:
        return

    today = today_str()
    now = now_local()
    final_min = CFG["FINAL_REMIND_MINUTES"]
    deadlines = get_todays_deadlines()

    # Шукаємо активний слот, де відкрите вікно прийому фото
    active_slot: dict | None = None
    for dl_time in deadlines:
        dl = deadline_dt(dl_time)
        window_start = dl - timedelta(minutes=final_min)

        if window_start <= now <= dl:
            task = await get_task(today, dl_time)
            if task and task["status"] == "pending" and task["notified_final"]:
                active_slot = task
                active_slot["_dl_time"] = dl_time
                break

    photo_file_id = msg.photo[-1].file_id  # найбільша версія

    # --- Перевірка тестового вікна (відповідь без запису в БД) ---
    test_until: datetime | None = context.application.bot_data.get("test_photo_window")
    if test_until is not None and now_local() <= test_until:
        await context.bot.send_message(
            chat_id=CFG["GROUP_CHAT_ID"],
            message_thread_id=CFG["THREAD_ID"],
            text="✅ [ТЕСТ] Фото отримано — бот бачить скрін і може його обробити.",
        )
        log.info("[ТЕСТ] Фото від %d прийнято в тестовому вікні.", user_id)
        return

    if active_slot is None:
        await msg.reply_text("📸 Скрін отримано, але поза розкладом.")
        log.info("Фото від %d поза вікном прийому.", user_id)
        return

    dl_time = active_slot["_dl_time"]
    task_id = active_slot["id"]
    await mark_done(task_id, photo_file_id, user_id)

    # Підтвердження у гілку
    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=f"✅ Дедлайн {dl_time} — оброблено.",
    )

    # Скасовуємо заплановану перевірку
    for job in context.job_queue.get_jobs_by_name(f"check_{dl_time}"):
        job.schedule_removal()

    log.info("Фото від %d зараховано для дедлайну %s.", user_id, dl_time)


# ---------------------------------------------------------------------------
# Команди
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — статус усіх слотів за сьогодні."""
    if not await _is_allowed_context(update):
        return

    today = today_str()
    deadlines = get_todays_deadlines()
    tasks = await get_tasks_for_date(today)

    # Створюємо рядки для слотів, яких ще немає в БД
    existing = {t["deadline_time"] for t in tasks}
    for dl_time in deadlines:
        if dl_time not in existing:
            await ensure_task(today, dl_time)
    tasks = await get_tasks_for_date(today)

    if not tasks:
        day_label = "будній" if is_workday() else "вихідний"
        await update.message.reply_text(f"Сьогодні {day_label} день без активних слотів.")
        return

    day_label = "будній" if is_workday() else "вихідний"
    lines = [f"📋 <b>Статус повернень за {today}</b> ({day_label})\n"]
    for t in tasks:
        icon = format_status_icon(t["status"])
        skip_note = f" — {t['skip_reason']}" if t.get("skip_reason") else ""
        lines.append(f"{icon} <code>{t['deadline_time']}</code> — {t['status']}{skip_note}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report YYYY-MM-DD — звіт за вказану дату."""
    if not await _is_allowed_context(update):
        return

    if not context.args:
        await update.message.reply_text("Використання: /report YYYY-MM-DD")
        return

    report_date = context.args[0]
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        await update.message.reply_text("Невірний формат дати. Використовуйте YYYY-MM-DD.")
        return

    tasks = await get_tasks_for_date(report_date)
    if not tasks:
        await update.message.reply_text(f"Немає даних за {report_date}.")
        return

    lines = [f"📊 <b>Звіт за {report_date}</b>\n"]
    for t in tasks:
        icon = format_status_icon(t["status"])
        confirmed = f" (підтв. {t['confirmed_at'][:16]})" if t.get("confirmed_at") else ""
        skip_note = f" — {t['skip_reason']}" if t.get("skip_reason") else ""
        lines.append(f"{icon} <code>{t['deadline_time']}</code> — {t['status']}{confirmed}{skip_note}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/skip HH:MM причина — позначити слот як пропущений (тільки адміни)."""
    user_id = update.effective_user.id if update.effective_user else None

    if user_id not in CFG["ADMIN_USER_IDS"]:
        await update.message.reply_text("⛔ Ця команда доступна тільки адміністраторам.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Використання: /skip HH:MM причина")
        return

    dl_time = context.args[0]
    reason = " ".join(context.args[1:])

    try:
        datetime.strptime(dl_time, "%H:%M")
    except ValueError:
        await update.message.reply_text("Невірний формат часу. Використовуйте HH:MM.")
        return

    # Перевіряємо проти розкладу поточного дня
    todays_deadlines = get_todays_deadlines()
    if dl_time not in todays_deadlines:
        await update.message.reply_text(
            f"Дедлайн {dl_time} не знайдено в розкладі сьогодні.\n"
            f"Доступні: {', '.join(todays_deadlines) or 'немає'}"
        )
        return

    today = today_str()
    task_id = await ensure_task(today, dl_time)
    await mark_skipped(task_id, reason)

    jq = context.job_queue
    for suffix in ("pre", "final", "check"):
        for job in jq.get_jobs_by_name(f"{suffix}_{dl_time}"):
            job.schedule_removal()

    await update.message.reply_text(
        f"⏭️ Слот {dl_time} позначено як пропущений.\nПричина: {reason}"
    )
    log.info("Адмін %d пропустив слот %s: %s", user_id, dl_time, reason)


# ---------------------------------------------------------------------------
# Тестові команди (тільки для адмінів, не змінюють БД)
# ---------------------------------------------------------------------------

# Тривалість тестового вікна прийому фото (хвилини)
TEST_PHOTO_WINDOW_MINUTES = 5


def _is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in CFG["ADMIN_USER_IDS"]


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/test — довідка по тестових командах."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступно тільки адміністраторам.")
        return

    text = (
        "🧪 <b>Тестові команди</b> (лише адміни, не впливають на БД)\n\n"
        "/test_remind — надіслати перше нагадування [ТЕСТ] у гілку\n"
        "/test_final — надіслати друге нагадування [ТЕСТ] у гілку "
        f"+ відкрити вікно фото на {TEST_PHOTO_WINDOW_MINUTES} хв\n"
        "/test_missed — симулювати прострочення з тегами у гілці\n"
        "/test_photo_window — статус тестового вікна фото"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_test_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_remind — надіслати тестове перше нагадування у гілку."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступно тільки адміністраторам.")
        return

    pre_min = CFG["PRE_REMIND_MINUTES"]
    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=(
            f"⏰ <b>[ТЕСТ] Нагадування</b> (за {pre_min} хв до дедлайну)\n\n"
            "Обробіть повернення."
        ),
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text("✅ Тестове перше нагадування надіслано в гілку.")
    log.info("[ТЕСТ] Перше нагадування надіслано адміном %d.", update.effective_user.id)


async def cmd_test_final(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_final — тестове друге нагадування + відкриття вікна прийому фото."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступно тільки адміністраторам.")
        return

    final_min = CFG["FINAL_REMIND_MINUTES"]
    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=(
            f"⚠️ <b>[ТЕСТ] Термінове нагадування</b> (за {final_min} хв до дедлайну)\n\n"
            "Обробіть повернення та надайте фото.\n\n"
            "<i>Надішліть фото підтвердження у цю гілку.</i>"
        ),
        parse_mode=ParseMode.HTML,
    )

    # Відкриваємо тестове вікно прийому фото
    window_until = now_local() + timedelta(minutes=TEST_PHOTO_WINDOW_MINUTES)
    context.application.bot_data["test_photo_window"] = window_until

    # Автозакриття вікна через TEST_PHOTO_WINDOW_MINUTES хвилин
    context.job_queue.run_once(
        _job_close_test_window,
        when=timedelta(minutes=TEST_PHOTO_WINDOW_MINUTES),
        name="close_test_window",
    )

    until_str = window_until.strftime("%H:%M:%S")
    await update.message.reply_text(
        f"✅ Тестове друге нагадування надіслано в гілку.\n"
        f"📸 Вікно прийому тестового фото відкрито до {until_str} (місцевий час).\n"
        f"Попросіть відповідального надіслати фото в гілку.",
    )
    log.info("[ТЕСТ] Вікно фото відкрито до %s адміном %d.", until_str, update.effective_user.id)


async def cmd_test_missed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_missed — симуляція прострочення у гілці з тегами, без запису в БД."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступно тільки адміністраторам.")
        return

    fake_time = now_local().strftime("%H:%M")
    mentions = mention_users()

    await context.bot.send_message(
        chat_id=CFG["GROUP_CHAT_ID"],
        message_thread_id=CFG["THREAD_ID"],
        text=(
            f"❌ <b>[ТЕСТ] Дедлайн {fake_time} прострочено!</b>\n\n"
            f"{mentions}\n"
            "Повернення не оброблено вчасно."
        ),
        parse_mode=ParseMode.HTML,
    )

    await update.message.reply_text("✅ Тестове повідомлення про прострочення надіслано у гілку.")
    log.info("[ТЕСТ] Симуляція прострочення виконана адміном %d.", update.effective_user.id)


async def cmd_test_photo_window(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/test_photo_window — показати статус тестового вікна або закрити його."""
    if not _is_admin(update):
        await update.message.reply_text("⛔ Доступно тільки адміністраторам.")
        return

    test_until: datetime | None = context.application.bot_data.get("test_photo_window")
    now = now_local()

    if test_until is None or now > test_until:
        context.application.bot_data.pop("test_photo_window", None)
        await update.message.reply_text(
            "📸 Тестове вікно прийому фото <b>закрито</b>.\n\n"
            "Щоб відкрити: /test_final",
            parse_mode=ParseMode.HTML,
        )
    else:
        remaining = int((test_until - now).total_seconds())
        until_str = test_until.strftime("%H:%M:%S")
        await update.message.reply_text(
            f"📸 Тестове вікно <b>відкрито</b> до {until_str}.\n"
            f"Залишилось: {remaining} сек.\n\n"
            "Надішліть фото у гілку, щоб перевірити прийом.",
            parse_mode=ParseMode.HTML,
        )


async def _job_close_test_window(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автоматично закриває тестове вікно прийому фото."""
    context.application.bot_data.pop("test_photo_window", None)
    log.info("[ТЕСТ] Тестове вікно фото автоматично закрито.")


# ---------------------------------------------------------------------------
# Допоміжна перевірка контексту для команд
# ---------------------------------------------------------------------------

async def _is_allowed_context(update: Update) -> bool:
    """Команда дозволена у правильній гілці або в DM."""
    msg = update.message
    if msg is None:
        return False

    if msg.chat.type == "private":
        return True

    if (
        msg.chat_id == CFG["GROUP_CHAT_ID"]
        and msg.message_thread_id == CFG["THREAD_ID"]
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Перевірка прав бота
# ---------------------------------------------------------------------------

async def check_bot_permissions(bot: Bot) -> None:
    """Перевіряє, що бот є адміном у групі."""
    try:
        member = await bot.get_chat_member(CFG["GROUP_CHAT_ID"], bot.id)
        if member.status not in ("administrator", "creator"):
            log.warning(
                "Бот не є адміністратором групи %d! "
                "Надайте боту права адміністратора з дозволом на управління топіками.",
                CFG["GROUP_CHAT_ID"],
            )
    except Exception as e:
        log.error("Не вдалося перевірити права бота: %s", e)


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    """Виконується після ініціалізації Application."""
    await init_db()
    await check_bot_permissions(app.bot)
    await restore_today_tasks(app)
    schedule_day_jobs(app)
    log.info("Бот успішно запущено та налаштовано.")


def main() -> None:
    global CFG, DB_PATH

    CFG = load_config()
    DB_PATH = CFG["DB_PATH"]

    log.info("Запуск бота...")
    log.info(
        "Конфіг: GROUP=%d THREAD=%d БУДНІ=%s ВИХІДНІ=%s TZ=%s",
        CFG["GROUP_CHAT_ID"],
        CFG["THREAD_ID"],
        CFG["WEEKDAY_DEADLINES"],
        CFG["WEEKEND_DEADLINES"],
        CFG["TIMEZONE"],
    )

    app = (
        Application.builder()
        .token(CFG["BOT_TOKEN"])
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("skip", cmd_skip))

    # Тестові команди
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("test_remind", cmd_test_remind))
    app.add_handler(CommandHandler("test_final", cmd_test_final))
    app.add_handler(CommandHandler("test_missed", cmd_test_missed))
    app.add_handler(CommandHandler("test_photo_window", cmd_test_photo_window))

    # Фото приймаємо тільки з супергрупи
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.ChatType.SUPERGROUP,
            handle_photo,
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
