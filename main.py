from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from io import BytesIO

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from access_control import approve, is_allowed, list_active, revoke, should_alert
from provider import (
    ProviderAuthorizationError,
    ProviderIntegrationPending,
    ProviderNotAuthorized,
    ProxyVIPClient,
)

ADMIN_ID = int(os.environ["ADMIN_ID"])
BOT_TOKEN = os.getenv("KEY_BOT_TOKEN", "")
MAX_KEY_BATCH = 100
provider = ProxyVIPClient()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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
        "Durações disponíveis: 1, 3, 7, 15 ou 30 dias; máximo de 100 keys por pedido."
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


async def _finish_proxy_qr_login(bot, login_task) -> None:
    try:
        authorized = await login_task
        if authorized:
            await bot.send_message(
                ADMIN_ID,
                "Conta Telegram conectada. O serviço poderá enviar comandos a @proxyvipp_bot "
                "e receber as respostas para entregar as keys neste bot.",
            )
        else:
            await bot.send_message(ADMIN_ID, "Não consegui confirmar a autorização Telegram. Tente /conectar_proxy de novo.")
    except ProviderAuthorizationError as exc:
        await bot.send_message(ADMIN_ID, str(exc))
    except Exception:
        logger.exception("Falha ao concluir autorização da conta Telegram")
        await bot.send_message(ADMIN_ID, "A autorização falhou. Confira os logs do serviço e tente novamente.")


async def connect_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    try:
        if await provider.is_authorized():
            await message.reply_text("A conta Telegram já está conectada.")
            return
        qr_image = await provider.begin_qr_login()
    except (ProviderIntegrationPending, ProviderAuthorizationError) as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Falha ao iniciar autorização QR")
        await message.reply_text("Não consegui iniciar a autorização agora. Confira as variáveis privadas no Railway.")
        return

    # Telethon exige que QRLogin.wait esteja ativo antes que o QR seja escaneado.
    login_task = context.application.create_task(provider.wait_for_qr_login())
    await asyncio.sleep(0)
    try:
        await message.reply_photo(
            photo=BytesIO(qr_image),
            caption=(
                "Escaneie este QR no app do Telegram em Configurações → Dispositivos → Vincular dispositivo.\n"
                "Ele autoriza uma sessão persistente do serviço para ler e enviar mensagens como sua conta. "
                "Não encaminhe o QR. Se expirar, use /conectar_proxy novamente."
            ),
        )
    except Exception:
        login_task.cancel()
        raise
    context.application.create_task(_finish_proxy_qr_login(context.bot, login_task))


async def proxy_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    try:
        authorized = await provider.is_authorized()
    except Exception:
        logger.exception("Falha ao consultar status da sessão Proxy VIP")
        await message.reply_text("Não consegui consultar a sessão. Confira as variáveis privadas e o volume /data.")
        return
    if not provider.configured:
        await message.reply_text("Integração incompleta: faltam TELEGRAM_API_ID e TELEGRAM_API_HASH no Railway.")
    elif authorized:
        await message.reply_text("A sessão do Telegram está conectada.")
    else:
        await message.reply_text("A sessão ainda não foi autorizada. Use /conectar_proxy.")


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not _is_allowed(user.id):
        await _access_denied(update, context)
        return
    if len(context.args) != 2 or not all(arg.isdigit() for arg in context.args):
        await message.reply_text(
            "Uso: /gerar DIAS QUANTIDADE\nDurações: 1, 3, 7, 15 ou 30 dias.\nQuantidade máxima: 100."
        )
        return
    duration_days, quantity = map(int, context.args)
    if duration_days not in provider.DURATION_LABELS:
        await message.reply_text("Duração disponível: 1, 3, 7, 15 ou 30 dias.")
        return
    if not 1 <= quantity <= MAX_KEY_BATCH:
        await message.reply_text(f"A quantidade deve ficar entre 1 e {MAX_KEY_BATCH}.")
        return
    try:
        keys = await provider.generate_keys(duration_days=duration_days, quantity=quantity)
    except (ProviderIntegrationPending, ProviderNotAuthorized) as exc:
        await message.reply_text(str(exc))
        return
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Falha ao solicitar keys ao bot fornecedor")
        await message.reply_text("Não consegui gerar as keys agora. Tente novamente mais tarde.")
        return
    if not keys:
        await message.reply_text("O bot fornecedor não retornou keys.")
        return
    await message.reply_text("Keys geradas:\n" + "\n".join(keys))


async def _post_init(application: Application) -> None:
    try:
        await provider.start()
    except Exception:
        # O bot de acesso continua disponível, mas geração e login QR ficam indisponíveis.
        logger.exception("Cliente Telegram pessoal não iniciou; verifique as variáveis e o volume /data")


async def _post_shutdown(application: Application) -> None:
    try:
        await provider.stop()
    except Exception:
        logger.exception("Falha ao encerrar cliente Telegram pessoal")


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Configure KEY_BOT_TOKEN como variável secreta antes de iniciar o bot.")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pedir_acesso", request_access))
    app.add_handler(CommandHandler("aprovar", approve_command))
    app.add_handler(CommandHandler("revogar", revoke_command))
    app.add_handler(CommandHandler("usuarios", users_command))
    app.add_handler(CommandHandler("conectar_proxy", connect_proxy_command))
    app.add_handler(CommandHandler("status_proxy", proxy_status_command))
    app.add_handler(CommandHandler("gerar", generate_command))
    return app


if __name__ == "__main__":
    build_application().run_polling(drop_pending_updates=True)
