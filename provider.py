from __future__ import annotations


class ProviderIntegrationPending(RuntimeError):
    """A API oficial ainda precisa de documentação para implementar o contrato."""


class ProxyVIPClient:
    """Interface do serviço externo; não automatiza o chat nem inventa endpoints."""

    async def generate_keys(self, *, duration_days: int, quantity: int) -> list[str]:
        if duration_days < 1 or quantity < 1:
            raise ValueError("Duração e quantidade devem ser positivas")
        raise ProviderIntegrationPending(
            "A emissão está aguardando a documentação oficial da API Proxy VIP."
        )
