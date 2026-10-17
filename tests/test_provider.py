import asyncio
import inspect
import unittest
from types import SimpleNamespace

from provider import ProviderIntegrationPending, ProxyVIPClient


class _FakeProviderClient:
    def __init__(self):
        self.bot_id = 123456
        self.handlers = []
        self.sent_messages = []
        self.history_reads = 0
        self.last_message = None
        self.next_message_id = 1

    async def is_user_authorized(self):
        return True

    async def get_entity(self, username):
        if username != "@proxyvipp_bot":
            raise AssertionError("unexpected provider")
        return SimpleNamespace(id=self.bot_id)

    async def get_messages(self, chat, limit=1):
        self.history_reads += 1
        return [self.last_message] if self.last_message else []

    def add_event_handler(self, callback, builder):
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback, builder):
        self.handlers = [
            item for item in self.handlers
            if not (item[0] is callback and item[1] is builder)
        ]

    def _message(self, text, buttons=None, message_id=None):
        if message_id is None:
            message_id = self.next_message_id
            self.next_message_id += 1
        markup = None
        if buttons:
            markup = SimpleNamespace(
                rows=[SimpleNamespace(buttons=[SimpleNamespace(text=label) for label in buttons])]
            )
        return SimpleNamespace(
            id=message_id,
            sender_id=self.bot_id,
            message=text,
            reply_markup=markup,
        )

    async def _emit(self, kind, message):
        event = SimpleNamespace(sender_id=self.bot_id, message=message)
        builder_name = "NewMessage" if kind == "new" else "MessageEdited"
        for callback, builder in list(self.handlers):
            if type(builder).__name__ == builder_name:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result

    async def send_message(self, chat, text):
        self.sent_messages.append(text)
        if text == "/start":
            self.last_message = self._message("Main menu", ["Gerar Key Proxy"])
            await self._emit("new", self.last_message)
        elif text == "Gerar Key Proxy":
            # Simulate the provider editing its menu instead of sending a new message.
            self.last_message.message = "Escolha a duração da key:"
            self.last_message.reply_markup = SimpleNamespace(
                rows=[SimpleNamespace(buttons=[
                    SimpleNamespace(text="1 Dia"),
                    SimpleNamespace(text="3 Dias"),
                ])]
            )
            await self._emit("edited", self.last_message)
        elif text == "1 Dia":
            self.last_message = self._message("Envie a quantidade de keys para gerar:")
            await self._emit("new", self.last_message)
        elif text == "1":
            self.last_message = self._message("Key criada com sucesso\nEXTERNAL-AB12CD34")
            await self._emit("new", self.last_message)
        else:
            raise AssertionError(f"unexpected provider message: {text}")


class ProviderTests(unittest.TestCase):
    def test_known_day_durations_match_provider_menu(self):
        expected = {1: "1 Dia", 3: "3 Dias", 7: "7 Dias", 15: "15 Dias", 30: "30 Dias"}
        for days, label in expected.items():
            with self.subTest(days=days):
                self.assertEqual(ProxyVIPClient.duration_label(days), label)

    def test_rejects_duration_not_shown_by_provider(self):
        with self.assertRaisesRegex(ValueError, "Duração indisponível"):
            ProxyVIPClient.duration_label(2)

    def test_button_matching_ignores_leading_emoji(self):
        button_text = "🔑 Gerar Key Proxy"
        message = SimpleNamespace(
            reply_markup=SimpleNamespace(
                rows=[SimpleNamespace(buttons=[SimpleNamespace(text=button_text)])]
            )
        )
        self.assertEqual(
            ProxyVIPClient._find_button(message, "Gerar Key Proxy"),
            (0, 0, button_text),
        )

    def test_default_one_day_quantity_prompt_is_supported_but_other_durations_fail_closed(self):
        client = ProxyVIPClient()
        prompt = SimpleNamespace(
            message="📄 Gerar Keys\n⏱ Duracao: 1 Dia\nEnvie a quantidade de keys para gerar (Max: 100):",
            reply_markup=None,
        )
        selected = asyncio.run(client._choose_duration_or_default(None, 0, prompt, 1))
        self.assertIs(selected, prompt)
        with self.assertRaisesRegex(ValueError, "duração padrão de 1 dia"):
            asyncio.run(client._choose_duration_or_default(None, 0, prompt, 3))

    def test_quantity_prompt_detection(self):
        prompt = SimpleNamespace(message="Envie a quantidade de keys para gerar (Max: 100):")
        self.assertTrue(ProxyVIPClient._is_quantity_prompt(prompt))

    def test_extracts_only_external_keys(self):
        text = (
            "keys criadas com sucesso\n"
            "🔑 EXTERNAL-AB12CD34\n"
            "🔑 EXTERNAL-EF56GH78\n"
            "Prazo: 1 Dia; limite de IP: 1"
        )
        self.assertEqual(
            ProxyVIPClient.extract_keys(text),
            ["EXTERNAL-AB12CD34", "EXTERNAL-EF56GH78"],
        )

    def test_generation_uses_events_and_handles_edited_provider_menu(self):
        client = ProxyVIPClient(api_id=123, api_hash="test-hash")
        fake_provider = _FakeProviderClient()
        client._client = fake_provider

        keys = asyncio.run(client.generate_keys(duration_days=1, quantity=1))

        self.assertEqual(keys, ["EXTERNAL-AB12CD34"])
        self.assertEqual(
            fake_provider.sent_messages,
            ["/start", "Gerar Key Proxy", "1 Dia", "1"],
        )
        self.assertEqual(fake_provider.history_reads, 0)
        self.assertEqual(fake_provider.handlers, [])

    def test_missing_telegram_credentials_fail_closed(self):
        client = ProxyVIPClient(api_id=0, api_hash="")
        self.assertFalse(client.configured)
        with self.assertRaises(ProviderIntegrationPending):
            asyncio.run(client.generate_keys(duration_days=1, quantity=1))

    def test_rejects_invalid_request_before_network_access(self):
        client = ProxyVIPClient(api_id=0, api_hash="")
        with self.assertRaises(ValueError):
            asyncio.run(client.generate_keys(duration_days=0, quantity=1))
        with self.assertRaises(ValueError):
            asyncio.run(client.generate_keys(duration_days=1, quantity=101))


if __name__ == "__main__":
    unittest.main()
