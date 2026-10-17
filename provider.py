from __future__ import annotations

import asyncio
import io
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator


class ProviderIntegrationPending(RuntimeError):
    """Configuração privada do cliente Telegram ainda incompleta."""


class ProviderNotAuthorized(RuntimeError):
    """A conta Telegram ainda não autorizou a sessão do serviço."""


class ProviderAuthorizationError(RuntimeError):
    """A autorização QR expirou ou requer uma etapa não suportada."""


class ProviderResponseError(RuntimeError):
    """O bot fornecedor não retornou a quantidade esperada de keys."""


class ProxyVIPClient:
    """Automatiza o fluxo de geração no bot fornecedor com a conta autorizada.

    A sessão Telethon fica em arquivo, por padrão sob /data, e deve ser protegida
    como uma credencial de acesso completo à conta Telegram.
    """

    PROVIDER_USERNAME = "@proxyvipp_bot"
    MAX_KEYS = 100
    DURATION_LABELS = {
        1: "1 Dia",
        3: "3 Dias",
        7: "7 Dias",
        15: "15 Dias",
        30: "30 Dias",
    }
    KEY_PATTERN = re.compile(r"\bEXTERNAL-[A-Za-z0-9_-]{4,}\b")

    def __init__(
        self,
        *,
        api_id: int | None = None,
        api_hash: str | None = None,
        session_path: str | None = None,
    ) -> None:
        env_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        try:
            self.api_id = int(api_id if api_id is not None else env_api_id or "0")
        except (TypeError, ValueError):
            self.api_id = 0
        self.api_hash = (api_hash if api_hash is not None else os.getenv("TELEGRAM_API_HASH", "")).strip()
        self.session_path = session_path or os.getenv("TELEGRAM_SESSION_PATH", "/data/proxyvip_userbot")
        self._client: Any | None = None
        self._pending_qr: Any | None = None
        self._lock = asyncio.Lock()
        self._qr_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return self.api_id > 0 and bool(self.api_hash)

    @classmethod
    def duration_label(cls, duration_days: int) -> str:
        try:
            return cls.DURATION_LABELS[duration_days]
        except KeyError as exc:
            choices = ", ".join(str(day) for day in cls.DURATION_LABELS)
            raise ValueError(f"Duração indisponível. Escolha um destes prazos em dias: {choices}.") from exc

    @staticmethod
    def _find_button(message: Any, label: str) -> tuple[int, int, str] | None:
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "rows", []) or []
        target = " ".join(label.casefold().split())
        for row_index, row in enumerate(rows):
            for column_index, button in enumerate(getattr(row, "buttons", []) or []):
                button_text = str(getattr(button, "text", "") or "")
                normalized = " ".join(button_text.casefold().split())
                if target and target in normalized:
                    return row_index, column_index, button_text
        return None

    @staticmethod
    def _message_signature(message: Any) -> tuple[str, tuple[tuple[str, ...], ...]]:
        text = str(getattr(message, "message", "") or "")
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "rows", []) or []
        button_rows = tuple(
            tuple(str(getattr(button, "text", "") or "") for button in (getattr(row, "buttons", []) or []))
            for row in rows
        )
        return text, button_rows

    @staticmethod
    def _is_quantity_prompt(message: Any) -> bool:
        text = str(getattr(message, "message", "") or "").casefold()
        return ("quantidade" in text and "key" in text) or ("quantity" in text and "key" in text)

    async def _choose_duration_or_default(
        self,
        chat: Any,
        bot_id: int,
        message: Any,
        duration_days: int,
        incoming: asyncio.Queue[tuple[str, Any]] | None = None,
    ) -> Any:
        label = self.duration_label(duration_days)
        if self._find_button(message, label):
            return await self._choose_and_wait(chat, bot_id, message, label, incoming)
        if self._is_quantity_prompt(message):
            if duration_days != 1:
                raise ValueError(
                    "O fornecedor está oferecendo a duração padrão de 1 dia sem seletor. "
                    "Nenhuma key foi solicitada; escolha 1 dia ou aguarde um seletor de duração."
                )
            return message
        return await self._choose_and_wait(chat, bot_id, message, label, incoming)

    @classmethod
    def extract_keys(cls, text: str | None) -> list[str]:
        if not text:
            return []
        return cls.KEY_PATTERN.findall(text)

    @classmethod
    def normalize_key(cls, key: str) -> str:
        normalized = key.strip().upper()
        if not cls.KEY_PATTERN.fullmatch(normalized):
            raise ValueError("Informe uma key completa no formato EXTERNAL-XXXX-XXXX-XXXX.")
        return normalized

    async def start(self) -> None:
        """Conecta sem iniciar login interativo; a autorização é feita por QR."""
        if not self.configured or self._client is not None:
            return
        try:
            from telethon import TelegramClient

            session_base = Path(self.session_path)
            session_base.parent.mkdir(parents=True, exist_ok=True)
            self._client = TelegramClient(str(session_base), self.api_id, self.api_hash)
            await self._client.connect()
        except Exception:
            self._client = None
            raise

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    async def is_authorized(self) -> bool:
        if not self.configured:
            return False
        if self._client is None:
            await self.start()
        if self._client is None:
            return False
        return bool(await self._client.is_user_authorized())

    async def begin_qr_login(self) -> bytes:
        if not self.configured:
            raise ProviderIntegrationPending(
                "Configure TELEGRAM_API_ID e TELEGRAM_API_HASH como variáveis privadas no Railway."
            )
        if self._pending_qr is not None:
            raise ProviderAuthorizationError("Já existe um pedido de autorização em andamento.")
        if self._client is None:
            await self.start()
        if self._client is None:
            raise ProviderIntegrationPending("Não foi possível abrir o cliente Telegram.")
        if await self._client.is_user_authorized():
            raise ProviderAuthorizationError("A sessão do Telegram já está autorizada.")

        import qrcode

        async with self._qr_lock:
            if self._pending_qr is not None:
                raise ProviderAuthorizationError("Já existe um pedido de autorização em andamento.")
            try:
                self._pending_qr = await self._client.qr_login()
                image = qrcode.make(self._pending_qr.url)
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()
            except Exception:
                self._pending_qr = None
                raise

    async def wait_for_qr_login(self, timeout_seconds: int = 120) -> bool:
        async with self._qr_lock:
            qr = self._pending_qr
            client = self._client
        if qr is None or client is None:
            raise ProviderAuthorizationError("Não há uma autorização QR pendente.")
        try:
            from telethon.errors import SessionPasswordNeededError

            await asyncio.wait_for(qr.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ProviderAuthorizationError(
                "O QR expirou. Rode /conectar_proxy novamente e aprove pelo app do Telegram."
            ) from exc
        except SessionPasswordNeededError as exc:
            raise ProviderAuthorizationError(
                "O Telegram pediu uma etapa adicional. Aprove a sessão no app e tente novamente."
            ) from exc
        except Exception as exc:
            raise ProviderAuthorizationError("A autorização Telegram não foi concluída.") from exc
        finally:
            async with self._qr_lock:
                if self._pending_qr is qr:
                    self._pending_qr = None
        return bool(await client.is_user_authorized())

    async def generate_keys(self, *, duration_days: int, quantity: int) -> list[str]:
        if duration_days < 1 or quantity < 1:
            raise ValueError("Duração e quantidade devem ser positivas.")
        if quantity > self.MAX_KEYS:
            raise ValueError(f"A quantidade máxima por pedido é {self.MAX_KEYS}.")
        self.duration_label(duration_days)
        if not self.configured:
            raise ProviderIntegrationPending(
                "Configure TELEGRAM_API_ID e TELEGRAM_API_HASH no Railway e use /conectar_proxy para autorizar."
            )
        if self._client is None:
            await self.start()
        if self._client is None or not await self._client.is_user_authorized():
            raise ProviderNotAuthorized(
                "A conta ainda não está autorizada. O administrador deve usar /conectar_proxy."
            )

        async with self._lock:
            chat = await self._client.get_entity(self.PROVIDER_USERNAME)
            bot_id = int(chat.id)
            start_message = await self._send_and_wait(chat, bot_id, "/start")
            generation_menu = await self._choose_and_wait(chat, bot_id, start_message, "Gerar Key Proxy")
            async with self._bot_message_feed(chat, bot_id) as incoming:
                duration_menu = await self._choose_duration_or_default(
                    chat, bot_id, generation_menu, duration_days, incoming
                )
                duration_menu_id = int(duration_menu.id)
                original_signature = self._message_signature(duration_menu)
                await self._client.send_message(chat, str(quantity))
                return await self._wait_for_keys(
                    incoming,
                    quantity=quantity,
                    after_id=duration_menu_id,
                    original_signature=original_signature,
                )

    async def delete_key(self, *, key: str) -> str:
        """Delete one explicitly confirmed key through the provider's own menu."""
        normalized = self.normalize_key(key)
        if not self.configured:
            raise ProviderIntegrationPending(
                "Configure TELEGRAM_API_ID e TELEGRAM_API_HASH no Railway e use /conectar_proxy para autorizar."
            )
        if self._client is None:
            await self.start()
        if self._client is None or not await self._client.is_user_authorized():
            raise ProviderNotAuthorized(
                "A conta ainda não está autorizada. O administrador deve usar /conectar_proxy."
            )

        async with self._lock:
            chat = await self._client.get_entity(self.PROVIDER_USERNAME)
            bot_id = int(chat.id)
            start_message = await self._send_and_wait(chat, bot_id, "/start")
            delete_prompt = await self._choose_and_wait(
                chat, bot_id, start_message, "Deletar Key"
            )
            prompt_text = str(getattr(delete_prompt, "message", "") or "").casefold()
            if "key" not in prompt_text or not any(word in prompt_text for word in ("código", "codigo", "exact", "key:")):
                raise ProviderResponseError(
                    "O provedor não exibiu o pedido de confirmação/código para excluir a key."
                )
            result = await self._send_and_wait(chat, bot_id, normalized)
            result_text = str(getattr(result, "message", "") or "")
            normalized_result = result_text.casefold()
            success_words = ("deletad", "deleted", "apag", "removid")
            failure_words = ("não", "nao", "not found", "inexist", "inválid", "invalid", "falha", "error")
            if (
                normalized in result_text.upper()
                and any(word in normalized_result for word in success_words)
                and not any(word in normalized_result for word in failure_words)
            ):
                return result_text
            raise ProviderResponseError("O provedor não confirmou a exclusão da key solicitada.")

    @asynccontextmanager
    async def _bot_message_feed(self, chat: Any, bot_id: int) -> AsyncIterator[asyncio.Queue[tuple[str, Any]]]:
        """Temporarily receive provider updates without polling GetHistoryRequest."""
        from telethon import events

        incoming: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def on_new_message(event: Any) -> None:
            if int(getattr(event, "sender_id", 0) or 0) == bot_id:
                incoming.put_nowait(("new", event.message))

        async def on_edited_message(event: Any) -> None:
            if int(getattr(event, "sender_id", 0) or 0) == bot_id:
                incoming.put_nowait(("edited", event.message))

        handlers = (
            (on_new_message, events.NewMessage(chats=chat)),
            (on_edited_message, events.MessageEdited(chats=chat)),
        )
        registered: list[tuple[Any, Any]] = []
        try:
            for callback, builder in handlers:
                self._client.add_event_handler(callback, builder)
                registered.append((callback, builder))
            yield incoming
        finally:
            for callback, builder in registered:
                self._client.remove_event_handler(callback, builder)

    async def _wait_for_bot_message(
        self,
        incoming: asyncio.Queue[tuple[str, Any]],
        *,
        timeout: float = 35.0,
        edited_message_id: int | None = None,
        original_signature: tuple[str, tuple[tuple[str, ...], ...]] | None = None,
        allow_any_edit: bool = False,
    ) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderResponseError("O bot fornecedor não respondeu dentro do tempo esperado.")
            try:
                event_type, message = await asyncio.wait_for(incoming.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise ProviderResponseError("O bot fornecedor não respondeu dentro do tempo esperado.") from exc

            if event_type == "new":
                return message
            if event_type == "edited":
                if allow_any_edit:
                    return message
                if edited_message_id is not None and int(message.id) == edited_message_id:
                    if self._message_signature(message) != original_signature:
                        return message

    async def _send_and_wait(self, chat: Any, bot_id: int, text: str) -> Any:
        async with self._bot_message_feed(chat, bot_id) as incoming:
            await self._client.send_message(chat, text)
            return await self._wait_for_bot_message(incoming, allow_any_edit=True)

    async def _perform_button_action(self, chat: Any, message: Any, label: str, incoming: asyncio.Queue[tuple[str, Any]]) -> Any:
        from telethon.tl.types import ReplyInlineMarkup

        original_signature = self._message_signature(message)
        button = self._find_button(message, label)
        if isinstance(getattr(message, "reply_markup", None), ReplyInlineMarkup):
            if button is None:
                raise ProviderResponseError(f"Não encontrei o botão esperado no fornecedor: {label}.")
            await message.click(button[0], button[1])
        else:
            await self._client.send_message(chat, button[2] if button is not None else label)
        return await self._wait_for_bot_message(
            incoming,
            edited_message_id=int(message.id),
            original_signature=original_signature,
        )

    async def _choose_and_wait(
        self,
        chat: Any,
        bot_id: int,
        message: Any,
        label: str,
        incoming: asyncio.Queue[tuple[str, Any]] | None = None,
    ) -> Any:
        if incoming is not None:
            return await self._perform_button_action(chat, message, label, incoming)
        async with self._bot_message_feed(chat, bot_id) as feed:
            return await self._perform_button_action(chat, message, label, feed)

    async def _wait_for_keys(
        self,
        incoming: asyncio.Queue[tuple[str, Any]],
        *,
        after_id: int,
        quantity: int,
        timeout: float = 60.0,
        original_signature: tuple[str, tuple[tuple[str, ...], ...]] | None = None,
    ) -> list[str]:
        deadline = asyncio.get_running_loop().time() + timeout
        keys: list[str] = []
        seen_events: set[tuple[int, tuple[str, tuple[tuple[str, ...], ...]]]] = set()
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderResponseError("A resposta não continha a quantidade esperada de keys EXTERNAL-.")
            try:
                event_type, message = await asyncio.wait_for(incoming.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise ProviderResponseError("A resposta não continha a quantidade esperada de keys EXTERNAL-.") from exc

            message_id = int(message.id)
            signature = self._message_signature(message)
            if event_type == "new" and message_id <= after_id:
                continue
            if event_type == "edited":
                if message_id < after_id:
                    continue
                if message_id == after_id and signature == original_signature:
                    continue
            event_key = (message_id, signature)
            if event_key in seen_events:
                continue
            seen_events.add(event_key)
            keys.extend(self.extract_keys(getattr(message, "message", "")))
            unique = list(dict.fromkeys(keys))
            if len(unique) >= quantity:
                return unique[:quantity]
