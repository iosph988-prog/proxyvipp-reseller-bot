from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from html import escape
from io import BytesIO

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from access_control import (
    approve,
    is_allowed,
    key_deletion_denial,
    list_active,
    mark_generated_key_deleted,
    record_generated_keys,
    revoke,
    should_alert,
)
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


def _access_request_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📨 Solicitar acesso", callback_data="request_access")]]
    )


def _home_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🔑 Gerar keys", callback_data="generate_menu")],
        [InlineKeyboardButton("🗑 Excluir minha key", callback_data="delete_key")],
        [InlineKeyboardButton("📘 Como usar", callback_data="help_generate")],
    ]
    if is_admin:
        rows.extend(
            [
                [
                    InlineKeyboardButton("➕ Adicionar revendedor", callback_data="add_reseller_help"),
                    InlineKeyboardButton("👥 Revendedores", callback_data="list_resellers"),
                ],
                [InlineKeyboardButton("📡 Status da sessão", callback_data="proxy_status")],
            ]
        )
    return InlineKeyboardMarkup(rows)


def _duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 Dia", callback_data="gen_duration_1"),
                InlineKeyboardButton("3 Dias", callback_data="gen_duration_3"),
            ],
            [
                InlineKeyboardButton("7 Dias", callback_data="gen_duration_7"),
                InlineKeyboardButton("15 Dias", callback_data="gen_duration_15"),
            ],
            [InlineKeyboardButton("30 Dias", callback_data="gen_duration_30")],
            [InlineKeyboardButton("↩️ Menu", callback_data="home")],
        ]
    )


def _quantity_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("1 key", callback_data="quantity_1"),
                InlineKeyboardButton("2 keys", callback_data="quantity_2"),
            ],
            [
                InlineKeyboardButton("5 keys", callback_data="quantity_5"),
                InlineKeyboardButton("10 keys", callback_data="quantity_10"),
            ],
            [InlineKeyboardButton("✍️ Outra quantidade", callback_data="quantity_custom")],
            [
                InlineKeyboardButton("↩️ Voltar", callback_data="quantity_cancel"),
                InlineKeyboardButton("🏠 Menu", callback_data="home"),
            ],
        ]
    )


def _custom_quantity_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Cancelar", callback_data="quantity_cancel")]]
    )


def _cancel_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✖️ Cancelar", callback_data="delete_cancel")]]
    )


def _confirm_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🗑 Confirmar exclusão", callback_data="delete_confirm"),
            InlineKeyboardButton("✖️ Cancelar", callback_data="delete_cancel"),
        ]]
    )


def _result_keyboard(keys: list[str]) -> InlineKeyboardMarkup:
    rows = []
    copy_text = keys[0] if len(keys) == 1 else "\n".join(keys)
    if len(copy_text) <= 256:
        label = "📋 Copiar key" if len(keys) == 1 else "📋 Copiar keys"
        rows.append(
            [InlineKeyboardButton(label, copy_text=CopyTextButton(text=copy_text))]
        )
    rows.append(
        [
            InlineKeyboardButton("🔑 Gerar mais", callback_data="generate_menu"),
            InlineKeyboardButton("🏠 Menu", callback_data="home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _welcome_content(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    is_admin = user_id == ADMIN_ID
    if is_admin:
        text = (
            "<b>✨ PROXY VIP • Painel do administrador</b>\n\n"
            "Escolha uma opção abaixo para gerar keys ou gerenciar os revendedores.\n\n"
            "<b>Adicionar revendedor</b>\n"
            "<code>/add_revendedor ID DIAS</code>\n"
            "Acesso temporário de 1 a 30 dias.\n\n"
            "Também é possível gerar diretamente com "
            "<code>/gerar DIAS QUANTIDADE</code>."
        )
    else:
        text = (
            "<b>✨ PROXY VIP • Área do revendedor</b>\n\n"
            "✅ Seu acesso está ativo.\n\n"
            "Toque em <b>Gerar keys</b> para seguir as etapas ou use:\n"
            "<code>/gerar DIAS QUANTIDADE</code>\n\n"
            "Prazos disponíveis: 1, 3, 7, 15 ou 30 dias.\n"
            "Você também pode excluir uma key emitida por você neste bot."
        )
    return text, _home_keyboard(is_admin)


async def _notify_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not should_alert(user.id):
        return
    try:
        await context.bot.send_message(
            ADMIN_ID,
            "📨 Pedido de acesso ao bot de keys\n"
            f"ID: {user.id}\nUsuário: {_user_label(user)}\n\n"
            f"Para liberar: /add_revendedor {user.id} DIAS (1–30)",
        )
    except Exception:
        logger.exception("Falha ao notificar o administrador")


async def _access_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message:
        await message.reply_text(
            "<b>🔒 Acesso controlado</b>\n\n"
            "Seu ID ainda não está liberado. Toque no botão abaixo ou use /pedir_acesso.\n"
            "Após a aprovação do administrador, você poderá solicitar keys.",
            parse_mode=ParseMode.HTML,
            reply_markup=_access_request_keyboard(),
        )
    await _notify_admin(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    context.user_data.pop("pending_generation_days", None)
    context.user_data.pop("awaiting_delete_key", None)
    context.user_data.pop("pending_delete_key", None)
    context.user_data.pop("pending_delete_expiry", None)
    if not _is_allowed(user.id):
        await _access_denied(update, context)
        return
    text, keyboard = _welcome_content(user.id)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if _is_allowed(user.id):
        await message.reply_text(
            "<b>✅ Seu acesso está ativo.</b> Use <code>/gerar DIAS QUANTIDADE</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    await _access_denied(update, context)


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await message.reply_text(
            "<b>Como adicionar um revendedor</b>\n"
            "<code>/add_revendedor ID DIAS</code>\n"
            "Exemplo: <code>/add_revendedor 123456789 7</code>\n"
            "O prazo deve ser de 1 a 30 dias.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        user_id, days = map(int, context.args)
    except ValueError:
        await message.reply_text("Use um ID numérico e um prazo de 1 a 30 dias.")
        return
    if user_id <= 0:
        await message.reply_text("O ID do Telegram precisa ser um número positivo.")
        return
    if not 1 <= days <= 30:
        await message.reply_text("⏳ O prazo do revendedor deve ficar entre 1 e 30 dias.")
        return
    try:
        expires_at = approve(user_id, days, ADMIN_ID)
    except ValueError as exc:
        await message.reply_text(f"Não foi possível adicionar: {exc}")
        return
    expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    await message.reply_text(
        "<b>✅ Revendedor liberado</b>\n\n"
        f"ID: <code>{user_id}</code>\n"
        f"Validade: <b>{days} dia(s)</b>\n"
        f"Expira em: <code>{expiry}</code>",
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            user_id,
            f"✅ Seu acesso ao bot de keys foi liberado por {days} dia(s).\n"
            "Use /start para ver as instruções.",
        )
    except Exception:
        logger.info("Não foi possível enviar aviso de aprovação ao ID %s", user_id)


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await message.reply_text("Uso: /revogar ID\nExemplo: /revogar 123456789")
        return
    user_id = int(context.args[0])
    if user_id <= 0:
        await message.reply_text("O ID do Telegram precisa ser um número positivo.")
        return
    revoke(user_id)
    await message.reply_text(
        f"🗑 Acesso do ID <code>{user_id}</code> revogado.",
        parse_mode=ParseMode.HTML,
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    rows = list_active()
    if not rows:
        await message.reply_text(
            "<b>👥 Revendedores ativos</b>\n\nNenhum acesso ativo no momento.",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = [f"<b>👥 Revendedores ativos: {len(rows)}</b>"]
    for user_id, expires_at in rows:
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        lines.append(f"• <code>{user_id}</code> — até <code>{expiry}</code>")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _show_duration_menu(query) -> None:
    await query.edit_message_text(
        "<b>🔑 GERAR KEYS</b>\n\n⏱ Escolha a duração:",
        parse_mode=ParseMode.HTML,
        reply_markup=_duration_keyboard(),
    )


async def _generate_and_reply(message, duration_days: int, quantity: int, user_id: int) -> None:
    try:
        keys = await provider.generate_keys(duration_days=duration_days, quantity=quantity)
    except (ProviderIntegrationPending, ProviderNotAuthorized) as exc:
        await message.reply_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML)
        return
    except ValueError as exc:
        await message.reply_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML)
        return
    except Exception:
        logger.exception("Falha ao solicitar keys ao bot fornecedor")
        await message.reply_text("❌ Não consegui gerar as keys agora. Tente novamente mais tarde.")
        return
    if not keys:
        await message.reply_text("⚠️ O bot fornecedor não retornou keys.")
        return

    record_generated_keys(user_id, keys)

    count = len(keys)
    key_word = "key" if count == 1 else "keys"
    verb = "criada" if count == 1 else "criadas"
    duration = provider.DURATION_LABELS[duration_days]
    key_lines = "\n".join(f"<code>{escape(key)}</code>" for key in keys)
    text = (
        f"<b>✅ {count} {key_word} {verb} com sucesso</b>\n\n"
        f"⏰ <b>Duração:</b> {escape(duration)}\n"
        "📱 <b>IP:</b> 1 por vez, atualizável\n\n"
        f"🔑 <b>Keys:</b>\n{key_lines}"
    )
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=_result_keyboard(keys),
    )


async def quantity_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if context.user_data.get("awaiting_delete_key"):
        if not _is_allowed(user.id):
            context.user_data.pop("awaiting_delete_key", None)
            await _access_denied(update, context)
            return
        context.user_data.pop("awaiting_delete_key", None)
        try:
            key = provider.normalize_key(message.text or "")
        except ValueError as exc:
            await message.reply_text(str(exc))
            return
        denial = key_deletion_denial(user.id, ADMIN_ID, key)
        if denial == "already_deleted":
            await message.reply_text("⚠️ Esta key já consta como excluída neste bot.")
            return
        if denial:
            await message.reply_text(
                "🔒 Só é possível excluir keys geradas pelo seu próprio ID neste bot."
            )
            return
        context.user_data["pending_delete_key"] = key
        context.user_data["pending_delete_expiry"] = int(time.time()) + 300
        await message.reply_text(
            "<b>⚠️ Confirmar exclusão</b>\n\n"
            "A exclusão é permanente no provedor e pode interromper o acesso associado.\n"
            f"Key: <code>{escape(key)}</code>\n\n"
            "Confirme somente se esta é a key correta.",
            parse_mode=ParseMode.HTML,
            reply_markup=_confirm_delete_keyboard(),
        )
        return
    if "pending_generation_days" not in context.user_data:
        return
    if not _is_allowed(user.id):
        context.user_data.pop("pending_generation_days", None)
        await _access_denied(update, context)
        return
    raw_quantity = (message.text or "").strip()
    if not raw_quantity.isdigit():
        await message.reply_text("Envie somente a quantidade, de 1 a 100.")
        return
    quantity = int(raw_quantity)
    if not 1 <= quantity <= MAX_KEY_BATCH:
        await message.reply_text("A quantidade precisa ficar entre 1 e 100.")
        return
    duration_days = context.user_data.pop("pending_generation_days")
    await message.reply_text("⏳ Pedido recebido. Preparando suas keys...")
    await _generate_and_reply(message, duration_days, quantity, user.id)


async def delete_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not _is_allowed(user.id):
        await _access_denied(update, context)
        return
    context.user_data.pop("pending_generation_days", None)
    context.user_data.pop("pending_delete_key", None)
    context.user_data.pop("pending_delete_expiry", None)
    context.user_data["awaiting_delete_key"] = True
    await message.reply_text(
        "<b>🗑 EXCLUIR KEY</b>\n\n"
        "Envie o código exato da key. Revendedores só podem excluir keys geradas pelo próprio ID neste bot.",
        parse_mode=ParseMode.HTML,
        reply_markup=_cancel_delete_keyboard(),
    )


async def _confirm_delete_key(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    key = context.user_data.pop("pending_delete_key", None)
    expiry = context.user_data.pop("pending_delete_expiry", 0)
    if not key or int(expiry) < int(time.time()):
        await query.edit_message_text("A confirmação expirou. Inicie novamente pelo menu.")
        return
    denial = key_deletion_denial(user_id, ADMIN_ID, key)
    if denial == "already_deleted":
        await query.edit_message_text("⚠️ Esta key já consta como excluída neste bot.")
        return
    if denial:
        await query.edit_message_text("🔒 Você não tem permissão para excluir essa key.")
        return

    await query.edit_message_text("⏳ Confirmado. Solicitando a exclusão ao provedor...")
    try:
        await provider.delete_key(key=key)
    except (ProviderIntegrationPending, ProviderNotAuthorized, ProviderAuthorizationError, ValueError) as exc:
        await query.edit_message_text(f"⚠️ {escape(str(exc))}", parse_mode=ParseMode.HTML)
        return
    except Exception:
        logger.error("Falha ao solicitar exclusão ao bot fornecedor; detalhes ocultados para proteger a key")
        await query.edit_message_text(
            "❌ O provedor não confirmou a exclusão. A key não foi marcada como excluída; confira o status antes de tentar novamente."
        )
        return

    mark_generated_key_deleted(key, owner_id=user_id)
    await query.edit_message_text(
        "<b>✅ Key excluída no provedor</b>\n\n"
        f"<code>{escape(key)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Menu", callback_data="home")]]
        ),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    message = update.effective_message
    if not query or not user or not message:
        return
    data = query.data or ""
    admin_actions = {"add_reseller_help", "list_resellers", "proxy_status"}
    if data in admin_actions and user.id != ADMIN_ID:
        await query.answer("Esta opção é exclusiva do administrador.", show_alert=True)
        return
    if data != "request_access" and data not in admin_actions and not _is_allowed(user.id):
        await query.answer()
        await _access_denied(update, context)
        return

    await query.answer()
    if data == "request_access":
        if _is_allowed(user.id):
            await message.reply_text("✅ Seu acesso está ativo. Use /gerar DIAS QUANTIDADE.")
        else:
            await _access_denied(update, context)
    elif data == "home":
        context.user_data.pop("pending_generation_days", None)
        context.user_data.pop("awaiting_delete_key", None)
        context.user_data.pop("pending_delete_key", None)
        context.user_data.pop("pending_delete_expiry", None)
        if not _is_allowed(user.id):
            await _access_denied(update, context)
            return
        text, keyboard = _welcome_content(user.id)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif data == "delete_key":
        context.user_data.pop("pending_generation_days", None)
        context.user_data.pop("pending_delete_key", None)
        context.user_data.pop("pending_delete_expiry", None)
        context.user_data["awaiting_delete_key"] = True
        await query.edit_message_text(
            "<b>🗑 EXCLUIR KEY</b>\n\n"
            "Envie o código exato da key. Revendedores só podem excluir keys geradas pelo próprio ID neste bot.",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_delete_keyboard(),
        )
    elif data == "delete_cancel":
        context.user_data.pop("awaiting_delete_key", None)
        context.user_data.pop("pending_delete_key", None)
        context.user_data.pop("pending_delete_expiry", None)
        await query.edit_message_text(
            "Exclusão cancelada.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Menu", callback_data="home")]]
            ),
        )
    elif data == "delete_confirm":
        await _confirm_delete_key(query, context, user.id)
    elif data == "generate_menu" or data == "quantity_cancel":
        context.user_data.pop("pending_generation_days", None)
        context.user_data.pop("awaiting_delete_key", None)
        context.user_data.pop("pending_delete_key", None)
        context.user_data.pop("pending_delete_expiry", None)
        await _show_duration_menu(query)
    elif data == "help_generate":
        await query.edit_message_text(
            "<b>📘 COMO GERAR</b>\n\n"
            "Escolha <b>Gerar keys</b> no menu e siga as etapas.\n"
            "Ou envie <code>/gerar DIAS QUANTIDADE</code>.\n\n"
            "Prazos: 1, 3, 7, 15 ou 30 dias.\n"
            "Quantidade: de 1 a 100 por pedido.\n\n"
            "Para excluir uma key própria, toque em <b>Excluir minha key</b> ou use /deletar_key; "
            "o bot pedirá confirmação antes de apagar.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔑 Gerar keys", callback_data="generate_menu")],
                    [InlineKeyboardButton("🗑 Excluir minha key", callback_data="delete_key")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="home")],
                ]
            ),
        )
    elif data.startswith("gen_duration_"):
        duration_days = int(data.rsplit("_", 1)[1])
        if duration_days not in provider.DURATION_LABELS:
            await query.edit_message_text("Esse prazo não está disponível. Use /gerar para tentar novamente.")
            return
        context.user_data["pending_generation_days"] = duration_days
        await query.edit_message_text(
            f"<b>🎟 GERAR KEYS</b>\n\n"
            f"⏱ Duração: <b>{escape(provider.DURATION_LABELS[duration_days])}</b>\n"
            "🔢 Agora escolha a quantidade:",
            parse_mode=ParseMode.HTML,
            reply_markup=_quantity_keyboard(),
        )
    elif data == "quantity_custom":
        if "pending_generation_days" not in context.user_data:
            await _show_duration_menu(query)
            return
        await query.edit_message_text(
            "<b>🔢 QUANTIDADE</b>\n\n"
            "Envie agora um número de 1 a 100.\n"
            "Exemplo: <code>3</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_custom_quantity_keyboard(),
        )
    elif data.startswith("quantity_"):
        if "pending_generation_days" not in context.user_data:
            await _show_duration_menu(query)
            return
        quantity = int(data.rsplit("_", 1)[1])
        duration_days = context.user_data.pop("pending_generation_days")
        await query.edit_message_text(
            "<b>⏳ PEDIDO EM ANDAMENTO</b>\n\nConsultando o provedor...",
            parse_mode=ParseMode.HTML,
        )
        await _generate_and_reply(message, duration_days, quantity, user.id)
    elif data == "add_reseller_help":
        await message.reply_text(
            "<b>➕ ADICIONAR REVENDEDOR</b>\n\n"
            "Envie: <code>/add_revendedor ID DIAS</code>\n"
            "Exemplo: <code>/add_revendedor 123456789 7</code>\n"
            "Validade permitida: 1 a 30 dias.\n"
            "Para remover: <code>/revogar ID</code>.",
            parse_mode=ParseMode.HTML,
        )
    elif data == "list_resellers":
        await users_command(update, context)
    elif data == "proxy_status":
        await proxy_status_command(update, context)


async def _finish_proxy_qr_login(bot, login_task) -> None:
    try:
        authorized = await login_task
        if authorized:
            await bot.send_message(
                ADMIN_ID,
                "✅ Conta Telegram conectada. O serviço poderá enviar comandos a @proxyvipp_bot "
                "e receber as respostas para entregar as keys neste bot.",
            )
        else:
            await bot.send_message(
                ADMIN_ID,
                "Não consegui confirmar a autorização Telegram. Tente /conectar_proxy de novo.",
            )
    except ProviderAuthorizationError as exc:
        await bot.send_message(ADMIN_ID, str(exc))
    except Exception:
        logger.exception("Falha ao concluir autorização da conta Telegram")
        await bot.send_message(
            ADMIN_ID,
            "A autorização falhou. Confira os logs do serviço e tente novamente.",
        )


async def connect_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.id != ADMIN_ID:
        return
    try:
        if await provider.is_authorized():
            await message.reply_text("✅ A conta Telegram já está conectada.")
            return
        qr_image = await provider.begin_qr_login()
    except (ProviderIntegrationPending, ProviderAuthorizationError) as exc:
        await message.reply_text(str(exc))
        return
    except Exception:
        logger.exception("Falha ao iniciar autorização QR")
        await message.reply_text(
            "Não consegui iniciar a autorização agora. Confira as variáveis privadas no Railway."
        )
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
        await message.reply_text(
            "⚠️ Não consegui consultar a sessão. Confira as variáveis privadas e o volume /data."
        )
        return
    if not provider.configured:
        await message.reply_text(
            "⚠️ <b>Integração incompleta</b>\nFaltam TELEGRAM_API_ID e TELEGRAM_API_HASH no Railway.",
            parse_mode=ParseMode.HTML,
        )
    elif authorized:
        await message.reply_text(
            "✅ <b>Sessão conectada</b>\nA conta Telegram está autorizada para conversar com @proxyvipp_bot.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "🔒 <b>Sessão não autorizada</b>\nUse /conectar_proxy para vincular a conta.",
            parse_mode=ParseMode.HTML,
        )


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not _is_allowed(user.id):
        await _access_denied(update, context)
        return
    context.user_data.pop("awaiting_delete_key", None)
    context.user_data.pop("pending_delete_key", None)
    context.user_data.pop("pending_delete_expiry", None)
    context.user_data.pop("pending_generation_days", None)
    if len(context.args) != 2 or not all(arg.isdigit() for arg in context.args):
        await message.reply_text(
            "<b>Como gerar</b>\n"
            "Use <code>/gerar DIAS QUANTIDADE</code>.\n"
            "Prazos: 1, 3, 7, 15 ou 30 dias; máximo: 100 keys.",
            parse_mode=ParseMode.HTML,
        )
        return
    duration_days, quantity = map(int, context.args)
    if duration_days not in provider.DURATION_LABELS:
        await message.reply_text("⏳ Prazo de key disponível: 1, 3, 7, 15 ou 30 dias.")
        return
    if not 1 <= quantity <= MAX_KEY_BATCH:
        await message.reply_text(f"A quantidade deve ficar entre 1 e {MAX_KEY_BATCH}.")
        return
    await message.reply_text("⏳ Pedido recebido. Preparando suas keys...")
    await _generate_and_reply(message, duration_days, quantity, user.id)


async def _post_init(application: Application) -> None:
    try:
        public_commands = [
            BotCommand("start", "Abrir o painel"),
            BotCommand("ajuda", "Ver instruções"),
            BotCommand("gerar", "Gerar keys"),
            BotCommand("deletar_key", "Excluir uma key própria"),
            BotCommand("pedir_acesso", "Solicitar acesso"),
        ]
        admin_commands = public_commands + [
            BotCommand("add_revendedor", "Liberar ID por 1 a 30 dias"),
            BotCommand("revendedores", "Listar acessos ativos"),
            BotCommand("revogar", "Revogar acesso por ID"),
            BotCommand("status_proxy", "Ver status da sessão"),
            BotCommand("conectar_proxy", "Conectar sessão Telegram"),
        ]
        await application.bot.set_my_commands(public_commands)
        await application.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
    except Exception:
        logger.exception("Falha ao atualizar o menu de comandos do Telegram")
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
    app.add_handler(CommandHandler(["start", "ajuda"], help_command))
    app.add_handler(CommandHandler("pedir_acesso", request_access))
    app.add_handler(CommandHandler(["add_revendedor", "aprovar"], approve_command))
    app.add_handler(CommandHandler(["revogar", "remover_revendedor"], revoke_command))
    app.add_handler(CommandHandler(["revendedores", "usuarios"], users_command))
    app.add_handler(CommandHandler("conectar_proxy", connect_proxy_command))
    app.add_handler(CommandHandler("status_proxy", proxy_status_command))
    app.add_handler(CommandHandler("gerar", generate_command))
    app.add_handler(CommandHandler(["deletar_key", "excluir_key"], delete_key_command))
    app.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=(
                r"^(request_access|help_generate|add_reseller_help|list_resellers|proxy_status|"
                r"delete_key|delete_confirm|delete_cancel|"
                r"generate_menu|home|gen_duration_(1|3|7|15|30)|"
                r"quantity_(1|2|5|10|custom|cancel))$"
            ),
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, quantity_input_handler))
    return app


if __name__ == "__main__":
    build_application().run_polling(drop_pending_updates=True)
