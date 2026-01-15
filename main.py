import os
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_http_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

Thread(target=start_http_server, daemon=True).start()

# ----------------------------
# CONFIG
# ----------------------------
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Rome"))
BOT_TOKEN = os.getenv("8549356098:AAGSRI0-bdadS-1aHkH7fK4E4_j7FKUEy7Y", "").strip()

# Metti qui il TUO user_id Telegram (numero) e, se vuoi, altri admin separati da virgola
# Esempio: ADMIN_IDS="12345678,98765432"
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("1911565433", "").split(",")
    if x.strip().isdigit()
}

PRICE_UNDER_30 = 10
PRICE_30_OR_MORE = 15
MINUTES_THRESHOLD = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("TheRefinedRoverBot")

# ----------------------------
# STORAGE KEYS (persistence)
# ----------------------------
# user_data[user_id]:
#   dog_name: str
#   client_code: str
#   reminders: list[{id, text, when_iso, sent: bool}]
#
# bot_data:
#   clients: dict[client_code] = user_id
#   walks: list[walk_entry]
#   active_walks: dict[client_code] = {start_iso, started_by}

BOT_CLIENTS_KEY = "clients"
BOT_WALKS_KEY = "walks"
BOT_ACTIVE_WALKS_KEY = "active_walks"

# ----------------------------
# UTIL
# ----------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def now_tz() -> datetime:
    return datetime.now(tz=TZ)


def parse_dt_italian(s: str) -> datetime:
    # accetta: "gg/mm/aaaa hh:mm"
    # es: 15/01/2026 10:00
    dt = datetime.strptime(s.strip(), "%d/%m/%Y %H:%M")
    return dt.replace(tzinfo=TZ)


def week_start(dt: datetime) -> datetime:
    # Lunedì 00:00 della settimana corrente
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def calc_price(duration_minutes: int) -> int:
    return PRICE_UNDER_30 if duration_minutes < MINUTES_THRESHOLD else PRICE_30_OR_MORE


def get_or_init_user(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> dict:
    ud = context.application.user_data.setdefault(user_id, {})
    ud.setdefault("dog_name", "")
    ud.setdefault("client_code", "")
    ud.setdefault("reminders", [])
    return ud


def get_or_init_bot_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    bd = context.application.bot_data
    bd.setdefault(BOT_CLIENTS_KEY, {})       # code -> user_id
    bd.setdefault(BOT_WALKS_KEY, [])         # list of dict
    bd.setdefault(BOT_ACTIVE_WALKS_KEY, {})  # code -> dict
    return bd


def ensure_client_code(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    ud = get_or_init_user(context, user_id)
    bd = get_or_init_bot_data(context)
    clients = bd[BOT_CLIENTS_KEY]

    if ud["client_code"] and ud["client_code"] in clients and clients[ud["client_code"]] == user_id:
        return ud["client_code"]

    # genera codice unico
    while True:
        code = secrets.token_hex(3).upper()  # 6 chars hex
        if code not in clients:
            break
    ud["client_code"] = code
    clients[code] = user_id
    return code


def client_summary(context: ContextTypes.DEFAULT_TYPE, code: str) -> str:
    bd = get_or_init_bot_data(context)
    uid = bd[BOT_CLIENTS_KEY].get(code)
    if not uid:
        return f"{code} (cliente non trovato)"
    ud = get_or_init_user(context, uid)
    dog = ud.get("dog_name") or "Cane"
    return f"{code} - {dog}"


# ----------------------------
# REMINDERS (JobQueue)
# ----------------------------
async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    user_id = data.get("user_id")
    reminder_id = data.get("reminder_id")

    if not user_id or not reminder_id:
        return

    ud = get_or_init_user(context, user_id)
    dog = ud.get("dog_name") or "il tuo cane"

    reminders = ud.get("reminders", [])
    for r in reminders:
        if r.get("id") == reminder_id and not r.get("sent"):
            r["sent"] = True
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Promemoria per {dog}: {r.get('text','(senza testo)')}",
            )
            break


def schedule_one_reminder(app: Application, user_id: int, reminder: dict) -> None:
    # Usa JobQueue.run_once per pianificare notifiche [web:167]
    when_iso = reminder.get("when_iso")
    if not when_iso:
        return

    dt = datetime.fromisoformat(when_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    delay = (dt - now_tz()).total_seconds()
    if delay <= 0:
        return

    rid = reminder.get("id")
    if not rid:
        return

    # evita doppioni: job name = reminder_id
    existing = app.job_queue.get_jobs_by_name(rid)
    if existing:
        return

    app.job_queue.run_once(
        reminder_job,
        when=delay,
        name=rid,
        data={"user_id": user_id, "reminder_id": rid},
    )


async def reschedule_all_reminders(app: Application) -> None:
    # Ripianifica promemoria non ancora inviati dopo un restart
    for user_id, ud in app.user_data.items():
        for r in ud.get("reminders", []):
            if not r.get("sent"):
                schedule_one_reminder(app, user_id, r)


# ----------------------------
# MENUS
# ----------------------------
def client_menu() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Imposta promemoria", callback_data="c:set_reminder")],
        [InlineKeyboardButton("Report settimanale", callback_data="c:weekly_report")],
        [InlineKeyboardButton("Il mio codice cliente", callback_data="c:my_code")],
    ]
    return InlineKeyboardMarkup(kb)


def admin_menu(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    bd = get_or_init_bot_data(context)
    clients = bd[BOT_CLIENTS_KEY]

    rows = []
    if clients:
        # lista clienti (max 10 per non fare menu infinito)
        for code in list(clients.keys())[:10]:
            rows.append([InlineKeyboardButton(f"Seleziona {code}", callback_data=f"a:select:{code}")])
    rows.append([InlineKeyboardButton("Aggiorna lista", callback_data="a:refresh")])
    return InlineKeyboardMarkup(rows)


def admin_client_actions(code: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Inizio passeggiata", callback_data=f"a:start:{code}")],
        [InlineKeyboardButton("Fine passeggiata", callback_data=f"a:stop:{code}")],
        [InlineKeyboardButton("Report settimanale", callback_data=f"a:report:{code}")],
        [InlineKeyboardButton("Indietro", callback_data="a:back")],
    ]
    return InlineKeyboardMarkup(kb)


# ----------------------------
# CONVERSATIONS
# ----------------------------
ASK_DOGNAME = 10
REM_TEXT = 20
REM_WHEN = 21


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    get_or_init_bot_data(context)
    ud = get_or_init_user(context, user_id)

    if is_admin(user_id):
        await update.message.reply_text(
            "Pannello admin. Seleziona un cliente (codice) oppure premi Aggiorna lista.",
            reply_markup=admin_menu(context),
        )
        return ConversationHandler.END

    # cliente
    ensure_client_code(context, user_id)
    if not ud.get("dog_name"):
        await update.message.reply_text("Ciao! Come si chiama il tuo cane?")
        return ASK_DOGNAME

    await update.message.reply_text(
        f"Ciao! Gestisco promemoria e report per {ud['dog_name']}.",
        reply_markup=client_menu(),
    )
    return ConversationHandler.END


async def set_dogname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    ud = get_or_init_user(context, user_id)
    name = (update.message.text or "").strip()
    if len(name) < 1:
        await update.message.reply_text("Scrivi un nome valido.")
        return ASK_DOGNAME

    ud["dog_name"] = name
    code = ensure_client_code(context, user_id)
    await update.message.reply_text(
        f"Perfetto. Cane salvato: {name}.\n"
        f"Il tuo codice cliente è: {code}\n"
        f"Passalo al dogsitter per gestire inizio/fine passeggiata e report.",
        reply_markup=client_menu(),
    )
    return ConversationHandler.END


async def reminder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("Questo comando è per i clienti.")
        return ConversationHandler.END

    ud = get_or_init_user(context, user_id)
    if not ud.get("dog_name"):
        await update.message.reply_text("Prima imposta il nome del cane con /start.")
        return ConversationHandler.END

    await update.message.reply_text("Ok. Scrivi il testo del promemoria (es: Vaccino, Antiparassitario, Cibo...).")
    return REM_TEXT


async def reminder_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if len(text) < 2:
        await update.message.reply_text("Testo troppo corto. Riprova.")
        return REM_TEXT
    context.user_data["tmp_rem_text"] = text
    await update.message.reply_text("Ora scrivi data e ora nel formato: gg/mm/aaaa hh:mm  (es: 15/01/2026 10:00)")
    return REM_WHEN


async def reminder_when(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    ud = get_or_init_user(context, user_id)

    rem_text = context.user_data.get("tmp_rem_text", "").strip()
    when_str = (update.message.text or "").strip()

    try:
        dt = parse_dt_italian(when_str)
    except Exception:
        await update.message.reply_text("Formato non valido. Usa: 15/01/2026 10:00")
        return REM_WHEN

    if dt <= now_tz():
        await update.message.reply_text("Quella data/ora è nel passato. Inserisci una data futura.")
        return REM_WHEN

    rid = secrets.token_hex(8)
    reminder = {
        "id": rid,
        "text": rem_text,
        "when_iso": dt.isoformat(),
        "sent": False,
    }
    ud["reminders"].append(reminder)

    schedule_one_reminder(context.application, user_id, reminder)

    dog = ud.get("dog_name") or "il tuo cane"
    await update.message.reply_text(
        f"Promemoria salvato per {dog}: '{rem_text}'\n"
        f"Quando: {dt.strftime('%d/%m/%Y %H:%M')}",
        reply_markup=client_menu(),
    )
    context.user_data.pop("tmp_rem_text", None)
    return ConversationHandler.END


# ----------------------------
# REPORTS & WALKS
# ----------------------------
def weekly_stats_for_user(app: Application, user_id: int) -> dict:
    bd = app.bot_data
    walks = bd.get(BOT_WALKS_KEY, [])
    start_w = week_start(now_tz())
    end_w = start_w + timedelta(days=7)

    week_walks = []
    for w in walks:
        if w.get("client_user_id") != user_id:
            continue
        end_iso = w.get("end_iso")
        if not end_iso:
            continue
        end_dt = datetime.fromisoformat(end_iso)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=TZ)
        if start_w <= end_dt < end_w:
            week_walks.append(w)

    total_walks = len(week_walks)
    total_minutes = sum(int(w.get("duration_min", 0)) for w in week_walks)
    total_eur = sum(int(w.get("price_eur", 0)) for w in week_walks)
    return {
        "total_walks": total_walks,
        "total_minutes": total_minutes,
        "total_eur": total_eur,
        "start_w": start_w,
        "end_w": end_w,
    }


async def send_weekly_report(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    ud = get_or_init_user(context, user_id)
    dog = ud.get("dog_name") or "Cane"
    stats = weekly_stats_for_user(context.application, user_id)

    await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"Report settimanale ({stats['start_w'].strftime('%d/%m')} - {(stats['end_w']-timedelta(seconds=1)).strftime('%d/%m')})\n"
            f"Cane: {dog}\n"
            f"Passeggiate: {stats['total_walks']}\n"
            f"Durata totale: {stats['total_minutes']} min\n"
            f"Totale: {stats['total_eur']} €"
        ),
    )


async def admin_start_walk(context: ContextTypes.DEFAULT_TYPE, admin_id: int, code: str) -> str:
    bd = get_or_init_bot_data(context)
    clients = bd[BOT_CLIENTS_KEY]
    active = bd[BOT_ACTIVE_WALKS_KEY]

    user_id = clients.get(code)
    if not user_id:
        return "Codice cliente non valido."

    if code in active:
        return "C'è già una passeggiata attiva per questo cliente."

    start_dt = now_tz()
    active[code] = {"start_iso": start_dt.isoformat(), "started_by": admin_id}

    ud = get_or_init_user(context, user_id)
    dog = ud.get("dog_name") or "Cane"

    # notifica cliente
    await context.bot.send_message(
        chat_id=user_id,
        text=f"Passeggiata iniziata per {dog} alle {start_dt.strftime('%H:%M')}.",
    )

    return f"OK: passeggiata iniziata per {dog} ({code})."


async def admin_stop_walk(context: ContextTypes.DEFAULT_TYPE, admin_id: int, code: str) -> str:
    bd = get_or_init_bot_data(context)
    clients = bd[BOT_CLIENTS_KEY]
    active = bd[BOT_ACTIVE_WALKS_KEY]
    walks = bd[BOT_WALKS_KEY]

    user_id = clients.get(code)
    if not user_id:
        return "Codice cliente non valido."

    sess = active.get(code)
    if not sess:
        return "Nessuna passeggiata attiva per questo cliente."

    start_dt = datetime.fromisoformat(sess["start_iso"])
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=TZ)
    end_dt = now_tz()

    duration_min = max(0, int((end_dt - start_dt).total_seconds() // 60))
    price = calc_price(duration_min)

    ud = get_or_init_user(context, user_id)
    dog = ud.get("dog_name") or "Cane"

    walk_entry = {
        "client_user_id": user_id,
        "client_code": code,
        "dog_name": dog,
        "start_iso": start_dt.isoformat(),
        "end_iso": end_dt.isoformat(),
        "duration_min": duration_min,
        "price_eur": price,
        "started_by": sess.get("started_by"),
        "stopped_by": admin_id,
    }
    walks.append(walk_entry)
    active.pop(code, None)

    # notifica cliente con durata+prezzo
    await context.bot.send_message(
        chat_id=user_id,
        text=f"Passeggiata finita per {dog}.\nDurata: {duration_min} min\nCosto: {price} €",
    )

    return f"OK: passeggiata finita per {dog} ({code}). Durata {duration_min} min, {price} €."


# ----------------------------
# CALLBACKS (buttons)
# ----------------------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    # CLIENT
    if query.data.startswith("c:"):
        action = query.data.split(":", 1)[1]
        ud = get_or_init_user(context, user_id)

        if action == "set_reminder":
            await query.edit_message_text("Invia /promemoria per iniziare (ti guiderò passo-passo).")
            return

        if action == "weekly_report":
            await send_weekly_report(update, context, user_id)
            return

        if action == "my_code":
            code = ensure_client_code(context, user_id)
            await query.edit_message_text(f"Il tuo codice cliente è: {code}")
            return

    # ADMIN
    if query.data.startswith("a:"):
        if not is_admin(user_id):
            await query.edit_message_text("Accesso negato.")
            return

        parts = query.data.split(":")
        action = parts[1]

        if action == "refresh":
            await query.edit_message_text("Lista aggiornata:", reply_markup=admin_menu(context))
            return

        if action == "back":
            await query.edit_message_text("Seleziona un cliente:", reply_markup=admin_menu(context))
            return

        if action == "select" and len(parts) == 3:
            code = parts[2]
            await query.edit_message_text(
                f"Cliente selezionato: {client_summary(context, code)}",
                reply_markup=admin_client_actions(code),
            )
            return

        if action == "start" and len(parts) == 3:
            code = parts[2]
            msg = await admin_start_walk(context, user_id, code)
            await query.edit_message_text(msg, reply_markup=admin_client_actions(code))
            return

        if action == "stop" and len(parts) == 3:
            code = parts[2]
            msg = await admin_stop_walk(context, user_id, code)
            await query.edit_message_text(msg, reply_markup=admin_client_actions(code))
            return

        if action == "report" and len(parts) == 3:
            code = parts[2]
            bd = get_or_init_bot_data(context)
            uid = bd[BOT_CLIENTS_KEY].get(code)
            if not uid:
                await query.edit_message_text("Codice cliente non valido.", reply_markup=admin_client_actions(code))
                return
            await send_weekly_report(update, context, uid)
            await query.edit_message_text("Report inviato al cliente.", reply_markup=admin_client_actions(code))
            return


# ----------------------------
# COMMANDS (admin shortcuts)
# ----------------------------
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if is_admin(user_id):
        await update.message.reply_text(
            "Comandi admin:\n"
            "/start = pannello\n"
            "/inizio CODICE = inizio passeggiata cliente\n"
            "/fine CODICE = fine passeggiata cliente\n"
            "/report CODICE = invia report settimanale al cliente\n"
        )
    else:
        await update.message.reply_text(
            "Comandi cliente:\n"
            "/start = setup/menu\n"
            "/promemoria = imposta un promemoria (testo + data/ora)\n"
            "/report = report settimanale\n"
        )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("Usa /report CODICE (es: /report A1B2C3)")
        return
    await send_weekly_report(update, context, user_id)


async def cmd_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if not context.args:
        await update.message.reply_text("Uso: /inizio CODICE")
        return
    code = context.args[0].strip().upper()
    msg = await admin_start_walk(context, user_id, code)
    await update.message.reply_text(msg)


async def cmd_admin_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if not context.args:
        await update.message.reply_text("Uso: /fine CODICE")
        return
    code = context.args[0].strip().upper()
    msg = await admin_stop_walk(context, user_id, code)
    await update.message.reply_text(msg)


async def cmd_admin_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    if not context.args:
        await update.message.reply_text("Uso: /report CODICE")
        return
    code = context.args[0].strip().upper()
    bd = get_or_init_bot_data(context)
    uid = bd[BOT_CLIENTS_KEY].get(code)
    if not uid:
        await update.message.reply_text("Codice cliente non valido.")
        return
    await send_weekly_report(update, context, uid)
    await update.message.reply_text("Report inviato.")


# ----------------------------
# BOOTSTRAP
# ----------------------------
async def post_init(app: Application) -> None:
    # Ripianifica promemoria dopo restart [web:167]
    
    await app.bot.delete_webhook(drop_pending_updates=True)
    await reschedule_all_reminders(app)

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Manca BOT_TOKEN. Impostalo come variabile d'ambiente.")

    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS vuoto: nessuno potrà fare inizio/fine passeggiata.")

    persistence = PicklePersistence(
        filepath="persistence.pickle",
        store_user_data=True,
        store_chat_data=True,
        store_bot_data=True,
        update_interval=30,
    )  # persistenza su file [web:218]

    app = Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    # Conversation: /start per setup nome cane
    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_DOGNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_dogname)],
        },
        fallbacks=[CommandHandler("start", start)],
        name="start_conv",
        persistent=True,
    )

    reminder_conv = ConversationHandler(
        entry_points=[
            CommandHandler("promemoria", reminder_start),
        ],
        states={
            REM_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_text)],
            REM_WHEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminder_when)],
        },
        fallbacks=[CommandHandler("promemoria", reminder_start)],
        name="reminder_conv",
        persistent=True,
    )

    app.add_handler(start_conv)
    app.add_handler(reminder_conv)

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("report", cmd_report))

    # Admin commands
    app.add_handler(CommandHandler("inizio", cmd_admin_start))
    app.add_handler(CommandHandler("fine", cmd_admin_stop))
    app.add_handler(CommandHandler("report_admin", cmd_admin_report))  # opzionale

    app.add_handler(CallbackQueryHandler(on_callback))

    # Nota: polling. Always-on dipende dall'hosting.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
