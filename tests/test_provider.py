import asyncio
import unittest

from provider import ProxyVIPClient, ProviderIntegrationPending


class ProviderTests(unittest.TestCase):
    def test_provider_is_fail_closed_until_official_api_is_implemented(self):
        client = ProxyVIPClient()
        with self.assertRaises(ProviderIntegrationPending):
            asyncio.run(client.generate_keys(duration_days=1, quantity=2))

    def test_rejects_invalid_request(self):
        client = ProxyVIPClient()
        with self.assertRaises(ValueError):
            asyncio.run(client.generate_keys(duration_days=0, quantity=1))


if __name__ == "__main__":
    unittest.main()
