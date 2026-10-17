import asyncio
import unittest

from provider import ProviderIntegrationPending, ProxyVIPClient


class ProviderTests(unittest.TestCase):
    def test_known_day_durations_match_provider_menu(self):
        expected = {1: "1 Dia", 3: "3 Dias", 7: "7 Dias", 15: "15 Dias", 30: "30 Dias"}
        for days, label in expected.items():
            with self.subTest(days=days):
                self.assertEqual(ProxyVIPClient.duration_label(days), label)

    def test_rejects_duration_not_shown_by_provider(self):
        with self.assertRaisesRegex(ValueError, "Duração indisponível"):
            ProxyVIPClient.duration_label(2)

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
