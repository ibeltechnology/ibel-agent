import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response, status
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import aiosqlite
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Configuration via variables d'environnement
# Configuration
load_dotenv()


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_NAME = "users.db"

# CHANGER ICI : Votre identifiant Telegram pour administrer le bot
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# --- INITIALISATION DE L'APPLICATION FASTAPI ---
app = FastAPI()

# --- BASE DE DONNÉES ET GESTION REQUÊTES SQL ---
async def init_db():
    """Initialise la base de données (Utilisateurs + Historique)."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Table des utilisateurs autorisés
        await db.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Table pour la mémoire contextuelle du bot
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # L'administrateur est automatiquement inséré et autorisé au démarrage
        await db.execute(
            "INSERT OR IGNORE INTO allowed_users (user_id, username) VALUES (?, ?)", 
            (ADMIN_ID, "Admin")
        )
        await db.commit()

async def is_user_allowed(user_id: int) -> bool:
    """Vérifie si l'ID utilisateur existe dans la base de données."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None

async def add_user_to_db(user_id: int, username: str = None) -> bool:
    """Ajoute un utilisateur dans la base de données. Retourne False si déjà présent."""
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO allowed_users (user_id, username) VALUES (?, ?)", (user_id, username))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def remove_user_from_db(user_id: int) -> bool:
    """Supprime un utilisateur de la base de données. Retourne True si supprimé, False sinon."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,)) as cursor:
            await db.commit()
            return cursor.rowcount > 0

async def get_all_allowed_users() -> list:
    """Récupère tous les utilisateurs autorisés ordonnés par date d'ajout."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, username, added_at FROM allowed_users ORDER BY added_at ASC") as cursor:
            return await cursor.fetchall()

async def save_message(user_id: int, role: str, content: str):
    """Enregistre un message (user ou assistant) dans la base de données."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content))
        await db.commit()

async def get_chat_context(user_id: int, limit: int = 10) -> list:
    """Récupère les X derniers messages d'un utilisateur au format attendu par OpenAI."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT role, content FROM (
                SELECT role, content, id FROM chat_history 
                WHERE user_id = ? 
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
        """, (user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            return [{"role": row[0], "content": row[1]} for row in rows]

async def clear_chat_history(user_id: int):
    """Efface entièrement la mémoire d'un utilisateur."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
        await db.commit()


# --- CONFIGURATION CLIENTS TELEGRAM & OPENAI ---
tg_app = Application.builder().token(TOKEN).build()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# --- GESTIONNAIRE DE SÉCURITÉ (RE-POSITIONNÉ EN PREMIER) ---

async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche un message de refus aux utilisateurs non enregistrés."""
    user = update.effective_user
    print(f"⚠️ Accès refusé (DB) : {user.first_name} (ID: {user.id})")
    await update.message.reply_text(f"⛔ Accès refusé. Votre identifiant (`{user.id}`) n'est pas enregistré.", parse_mode="Markdown")


# --- GESTIONNAIRES DE COMMANDES (HANDLERS) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_allowed(user_id):
        await handle_unauthorized(update, context)
        return
    await update.message.reply_text("Bonjour ! Je me souviendrai du contexte de notre discussion. Utilisez /clear pour repartir de zéro.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_allowed(user_id):
        await handle_unauthorized(update, context)
        return
    await clear_chat_history(user_id)
    await update.message.reply_text("🔄 Mémoire réinitialisée ! Notre conversation repart à zéro.")

async def authorize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Vous n'avez pas les droits d'administration pour exécuter cette commande.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("💡 Usage correct : `/authorize <ID_TELEGRAM>`", parse_mode="Markdown")
        return
    target_id = int(context.args[0])
    if await add_user_to_db(target_id, "Ajouté par admin"):
        await update.message.reply_text(f"✅ L'utilisateur avec l'ID `{target_id}` a été ajouté et autorisé.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ L'ID `{target_id}` est déjà présent dans la base de données.", parse_mode="Markdown")

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Vous n'avez pas les droits d'administration pour exécuter cette commande.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("💡 Usage correct : `/revoke <ID_TELEGRAM>`", parse_mode="Markdown")
        return
    target_id = int(context.args[0])
    if target_id == ADMIN_ID:
        await update.message.reply_text("❌ Action impossible : vous ne pouvez pas révoquer vos propres accès.")
        return
    if await remove_user_from_db(target_id):
        await clear_chat_history(target_id)
        await update.message.reply_text(f"🗑️ L'accès pour l'ID `{target_id}` a été révoqué avec succès.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ L'ID `{target_id}` n'est pas présent dans la liste des utilisateurs autorisés.", parse_mode="Markdown")

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("⛔ Vous n'avez pas les droits d'administration.")
        return
        
    users = await get_all_allowed_users()
    if not users:
        await update.message.reply_text("La liste des utilisateurs est vide.")
        return
        
    message_lines = ["📋 *Utilisateurs autorisés :*\n"]
    for idx, (uid, username, date) in enumerate(users, start=1):
        name = username if username else "Sans pseudo"
        message_lines.append(f"{idx}. ID: `{uid}` | Nom: *{name}*")
        
    await update.message.reply_text("\n".join(message_lines), parse_mode="Markdown")


# --- GESTIONNAIRE DE MESSAGES TEXTE ---

async def handle_openai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_user_allowed(user_id):
        await handle_unauthorized(update, context)
        return

    user_text = update.message.text
    await save_message(user_id, "user", user_text)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    history_messages = await get_chat_context(user_id, limit=10)
    system_prompt = {"role": "system", "content": "Tu es un assistant utile doté d'une mémoire contextuelle."}
    full_messages = [system_prompt] + history_messages
    
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=full_messages,
            max_tokens=500
        )
        ai_reply = response.choices[0].message.content
        await save_message(user_id, "assistant", ai_reply)
        await update.message.reply_text(ai_reply)
    except Exception as e:
        print(f"Erreur OpenAI : {e}")
        await update.message.reply_text("Une erreur est survenue lors de la génération de la réponse.")


# --- CYCLE DE VIE DE L'APPLICATION FASTAPI (LIFESPAN) ---
# 2. On définit le lifespan en l'associant à notre instance app
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialisation de la BDD
    await init_db()
    
    # Enregistrement de tous les gestionnaires de commandes
    tg_app.add_handler(CommandHandler("start", start_command))
    tg_app.add_handler(CommandHandler("clear", clear_command))
    tg_app.add_handler(CommandHandler("authorize", authorize_command))
    tg_app.add_handler(CommandHandler("revoke", revoke_command))
    tg_app.add_handler(CommandHandler("list", list_command))
    
    # Enregistrement des messages textuels
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_openai_chat))
    
    # Configuration du webhook Telegram
    await tg_app.initialize()
    print(f"Configuration du webhook sur : {WEBHOOK_URL}")
    await tg_app.bot.set_webhook(url=WEBHOOK_URL)
    await tg_app.start()
    
    yield
    
    # Arrêt propre
    await tg_app.bot.delete_webhook()
    await tg_app.stop()
    await tg_app.shutdown()

# 3. On associe le gestionnaire de cycle de vie à l'application
app.router.lifespan_context = lifespan


# --- ENDPOINTS API ---

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        json_data = await request.json()
        update = Update.de_json(data=json_data, bot=tg_app.bot)
        await tg_app.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        print(f"Erreur webhook : {e}")
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


