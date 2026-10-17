# Bot de revendedores Proxy VIP

Projeto inicial separado do bot `.3105`. Usa `python-telegram-bot` e SQLite no volume persistente do Railway.

## Estado atual

- O controle de revendedores por ID e prazo está implementado.
- Só o administrador configurado pode aprovar, revogar ou listar acessos.
- O bot aceita pedidos de geração em formato provisório `/gerar DIAS QUANTIDADE`, com limite de lote 100 baseado no vídeo recebido.
- **A emissão real está deliberadamente desativada** até a API oficial do Proxy VIP ser documentada e integrada. O vídeo mostra o fluxo manual no Telegram, não uma API. Este projeto não automatiza uma conta pessoal nem envia comandos para outro bot via chat.
- As opções de duração e o contrato final de geração precisam ser alinhados com a documentação do fornecedor.

## Comandos

Para revendedores aprovados:

- `/start` — instruções
- `/pedir_acesso` — solicita acesso ao administrador
- `/gerar DIAS QUANTIDADE` — pedido de geração; permanece bloqueado até a API oficial estar ligada

Para o administrador (`ADMIN_ID`):

- `/aprovar ID DIAS` — concede acesso por prazo
- `/revogar ID` — revoga imediatamente
- `/usuarios` — lista autorizações ativas

Configure o ID do administrador apenas como variável de ambiente privada no Railway. A autorização expira automaticamente. Reaprovar substitui o prazo anterior por um novo prazo contado a partir da aprovação.

## Segredos e Railway

Configure no Railway **Variables** (não neste repositório):

- `KEY_BOT_TOKEN` — token do bot criado no BotFather
- `ADMIN_ID` — ID numérico do administrador
- `ACCESS_DB=/data/proxyvip_reseller_access.sqlite3` — banco persistente no volume montado em `/data`

Não publique ou envie `KEY_BOT_TOKEN`, tokens da API, keys geradas ou códigos de login em mensagens. Se um token já foi compartilhado, revogue-o no serviço emissor e gere outro.

## API do provedor

Para habilitar geração, forneça documentação oficial com URL-base, autenticação (sem o valor do segredo), endpoint/método, parâmetros de duração/quantidade, resposta de sucesso/erro, e operações autorizadas. O adaptador `provider.py` só deve ser implementado a partir dessa especificação. Depois, os segredos de API serão adicionados como variáveis protegidas no Railway.

## Executar localmente

```bash
python -m pip install -r requirements.txt
export KEY_BOT_TOKEN='(configurar fora do código)'
export ADMIN_ID='<seu ID numérico>'
export ACCESS_DB=/tmp/proxyvip-access.sqlite3
python main.py
```
