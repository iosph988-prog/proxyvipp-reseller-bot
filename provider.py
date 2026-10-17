from __future__ import annotations

import asyncio
import io
import os
import re
from pathlib import Path
from typing import Any


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

    @classmethod
    def extract_keys(cls, text: str | None) -> list[str]:
        if not text:
            return []
        return cls.KEY_PATTERN.findall(text)

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

        try:
            import qrcode

            self._pending_qr = await self._client.qr_login()
            image = qrcode.make(self._pending_qr.url)
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
        except Exception:
            self._pending_qr = None
            raise

    async def wait_for_qr_login(self, timeout_seconds: int = 120) -> bool:
        qr, self._pending_qr = self._pending_qr, None
        if qr is None or self._client is None:
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
        return bool(await self._client.is_user_authorized())

    async def generate_keys(self, *, duration_days: int, quantity: int) -> list[str]:
        if duration_days < 1 or quantity < 1:
            raise ValueError("Duração e quantidade devem ser positivas.")
        if quantity > self.MAX_KEYS:
            raise ValueError(f"A quantidade máxima por pedido é {self.MAX_KEYS}.")
        duration_label = self.duration_label(duration_days)
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
            duration_menu = await self._choose_and_wait(chat, bot_id, generation_menu, duration_label)
            before_quantity_id = int(duration_menu.id)
            await self._client.send_message(chat, str(quantity))
            return await self._wait_for_keys(chat, bot_id, before_quantity_id, quantity)

    async def _latest_message_id(self, chat: Any) -> int:
        messages = await self._client.get_messages(chat, limit=1)
        return int(messages[0].id) if messages else 0

    async def _wait_for_bot_message(self, chat: Any, bot_id: int, after_id: int, timeout: float = 35.0) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            messages = await self._client.get_messages(chat, limit=50)
            candidates = [
                message for message in messages
                if int(message.id) > after_id and int(getattr(message, "sender_id", 0) or 0) == bot_id
            ]
            if candidates:
                return min(candidates, key=lambda message: int(message.id))
            await asyncio.sleep(0.5)
        raise ProviderResponseError("O bot fornecedor não respondeu dentro do tempo esperado.")

    async def _send_and_wait(self, chat: Any, bot_id: int, text: str) -> Any:
        after_id = await self._latest_message_id(chat)
        await self._client.send_message(chat, text)
        return await self._wait_for_bot_message(chat, bot_id, after_id)

    async def _choose_and_wait(self, chat: Any, bot_id: int, message: Any, label: str) -> Any:
        from telethon.tl.types import ReplyInlineMarkup

        after_id = int(message.id)
        markup = getattr(message, "reply_markup", None)
        if isinstance(markup, ReplyInlineMarkup):
            await message.click(text=label)
        else:
            await self._client.send_message(chat, label)
        return await self._wait_for_bot_message(chat, bot_id, after_id)

    async def _wait_for_keys(
        self,
        chat: Any,
        bot_id: int,
        after_id: int,
        quantity: int,
        timeout: float = 60.0,
    ) -> list[str]:
        deadline = asyncio.get_running_loop().time() + timeout
        seen_ids: set[int] = set()
        keys: list[str] = []
        while asyncio.get_running_loop().time() < deadline:
            messages = await self._client.get_messages(chat, limit=100)
            for message in sorted(messages, key=lambda item: int(item.id)):
                message_id = int(message.id)
                if message_id <= after_id or message_id in seen_ids:
                    continue
                if int(getattr(message, "sender_id", 0) or 0) != bot_id:
                    continue
                seen_ids.add(message_id)
                keys.extend(self.extract_keys(getattr(message, "message", "")))
                if len(keys) >= quantity:
                    # Deduplicate while preserving order, then return no more than requested.
                    unique = list(dict.fromkeys(keys))
                    if len(unique) >= quantity:
                        return unique[:quantity]
            await asyncio.sleep(0.5)
        raise ProviderResponseError("A resposta não continha a quantidade esperada de keys EXTERNAL-.")
