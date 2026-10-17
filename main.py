from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from access_control import approve, is_allowed, list_active, revoke, should_alert
from provider import ProviderIntegrationPending, ProxyVIPClient

ADMIN_ID = int(os.environ["ADMIN_ID"])
BOT_TOKEN = os.getenv("KEY_BOT_TOKEN", "")
MAX_KEY_BATCH = 100  # limite mostrado no vídeo de referência
provider = ProxyVIPClient()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("proxyvip-reseller-bot")


def _is_allowed(user_id: int) -> bool:
    return is_allowed(user_id, ADMIN_ID)


def _user_label(user) -> str:
    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    username = f"@{user.username}" if user.username else "sem username"
    return f"{name or 'Sem nome'} ({username})"


async def _notify_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not should_alert(user.id):
        return
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"Pedido de acesso ao bot de keys\nID: {user.id}\nUsuário: {_user_label(user)}\n\n"
            f"Para liberar: /aprovar {user.id} DIAS",
        )
    except Exception:
        logger.exception("Falha ao notificar o administrador")


async def _access_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text("Seu ID ainda não tem acesso. Use /pedir_acesso para solicitar ao administrador.")
    await _notify_admin(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not _is_allowed(user.id):
        await _access_denied(update, context)
        return
    await message.reply_text(
        "Bot de geração de keys Proxy VIP\n\n"
        "Use /gerar DIAS QUANTIDADE para pedir keys.\n"
        "A emissão real será ativada após integrar a API oficial."
    )


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if _is_allowed(user.id):
        await message.reply_text("Seu acesso já está ativo. Use /gerar DIAS QUANTIDADE.")
        return
    await _access_denied(update, context)


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    if len(context.args) != 2 or not all(arg.isdigit() for arg in context.args):
        await message.reply_text("Uso: /aprovar ID DIAS\nExemplo: /aprovar 123456789 30")
        return
    user_id, days = map(int, context.args)
    try:
        expires_at = approve(user_id, days, ADMIN_ID)
    except ValueError as exc:
        await message.reply_text(f"Não foi possível aprovar: {exc}")
        return
    expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    await message.reply_text(f"Acesso aprovado para {user_id} por {days} dia(s). Expira em {expiry}.")
    try:
        await context.bot.send_message(user_id, f"Seu acesso ao bot de keys foi aprovado por {days} dia(s).")
    except Exception:
        logger.info("Não foi possível enviar aviso de aprovação ao ID %s", user_id)


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await message.reply_text("Uso: /revogar ID")
        return
    user_id = int(context.args[0])
    revoke(user_id)
    await message.reply_text(f"Acesso de {user_id} revogado.")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    rows = list_active()
    if not rows:
        await message.reply_text("Não há revendedores com acesso ativo.")
        return
    lines = ["Revendedores ativos:"]
    for user_id, expires_at in rows:
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        lines.append(f"• {user_id} — até {expiry}")
    await message.reply_text("\n".join(lines))


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not _is_allowed(user.id):
        await _access_denied(update, context)
        return
    if len(context.args) != 2 or not all(arg.isdigit() for arg in context.args):
        await message.reply_text("Uso provisório: /gerar DIAS QUANTIDADE\nQuantidade máxima por pedido: 100.")
        return
    duration_days, quantity = map(int, context.args)
    if not 1 <= duration_days <= 3650:
        await message.reply_text("A duração deve ficar entre 1 e 3650 dias.")
        return
    if not 1 <= quantity <= MAX_KEY_BATCH:
        await message.reply_text(f"A quantidade deve ficar entre 1 e {MAX_KEY_BATCH}.")
        return
    try:
        keys = await provider.generate_keys(duration_days=duration_days, quantity=quantity)
    except ProviderIntegrationPending:
        await message.reply_text(
            "A emissão automática ainda não está configurada. Ela será ativada após conectar a API oficial do Proxy VIP."
        )
        return
    except Exception:
        logger.exception("Falha na API oficial de geração")
        await message.reply_text("Não consegui gerar as keys agora. Tente novamente mais tarde.")
        return
    if not keys:
        await message.reply_text("A API não retornou keys.")
        return
    await message.reply_text("Keys geradas:\n" + "\n".join(str(key) for key in keys))


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Configure KEY_BOT_TOKEN como variável secreta antes de iniciar o bot.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pedir_acesso", request_access))
    app.add_handler(CommandHandler("aprovar", approve_command))
    app.add_handler(CommandHandler("revogar", revoke_command))
    app.add_handler(CommandHandler("usuarios", users_command))
    app.add_handler(CommandHandler("gerar", generate_command))
    return app


if __name__ == "__main__":
    build_application().run_polling(drop_pending_updates=True)
