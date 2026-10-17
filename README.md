# Bot de revendedores Proxy VIP

Projeto separado do bot `.3105`. O bot aplica controle de acesso temporário por ID e, quando configurado, solicita keys ao `@proxyvipp_bot` usando uma sessão Telegram pessoal autorizada. O resultado é entregue no chat do bot de revendedores.

## Como funciona

A Bot API do Telegram não permite que um bot converse diretamente com outro bot. Por isso, este projeto usa Telethon como cliente da conta Telegram autorizada pelo administrador. A conta envia somente o fluxo de geração mostrado no vídeo: `/start` → **Gerar Key Proxy** → duração → quantidade. O bot não automatiza as funções de banir, apagar ou resetar keys.

O vídeo mostra as durações de 1, 3, 7, 15 e 30 dias e um limite máximo de 100 keys por pedido. A automação aceita esses prazos; horas e duração personalizada ainda não estão implementadas.

## Controle de acesso e comandos

Revendedores sem acesso usam `/pedir_acesso`. O administrador configurado por `ADMIN_ID` pode usar:

- `/aprovar ID DIAS` — libera um revendedor pelo prazo indicado.
- `/revogar ID` — remove o acesso imediatamente.
- `/usuarios` — lista os acessos ativos.
- `/status_proxy` — verifica a configuração e a autorização da conta Telegram.
- `/conectar_proxy` — envia ao administrador um QR para autorizar a sessão da conta.

Para solicitar keys, um usuário aprovado usa `/gerar DIAS QUANTIDADE`. A saída é enviada de volta no mesmo chat. O primeiro uso do bot deve começar por `/start`.

## Acesso da conta Telegram

A sessão Telethon permite ao serviço **ler e enviar mensagens como a conta pessoal** para executar o fluxo com `@proxyvipp_bot`. O QR de `/conectar_proxy` deve ser escaneado somente pelo administrador no app do Telegram: **Configurações → Dispositivos → Vincular dispositivo**. Não encaminhe o QR.

O arquivo de sessão (`TELEGRAM_SESSION_PATH`) e o banco (`ACCESS_DB`) devem ficar em um volume persistente montado em `/data` no Railway. Para revogar a sessão, use Telegram → Configurações → Dispositivos e encerre a sessão do serviço. Nunca publique o arquivo `.session`, `api_hash`, token do BotFather, códigos de login ou keys geradas.

## Variáveis privadas do Railway

Configure em **Variables**; não coloque valores reais neste repositório:

- `KEY_BOT_TOKEN` — token do bot criado no BotFather.
- `ADMIN_ID` — ID numérico do administrador.
- `TELEGRAM_API_ID` — ID do app Telegram Developers já existente na conta.
- `TELEGRAM_API_HASH` — hash secreto do mesmo app; mantenha privado.
- `ACCESS_DB=/data/proxyvip_reseller_access.sqlite3` — banco de autorizações.
- `TELEGRAM_SESSION_PATH=/data/proxyvip_userbot` — base do arquivo de sessão persistente.

O volume `/data` precisa estar montado antes do uso para que aprovações e sessão sobrevivam aos reinícios. Verifique a cobrança do volume no Railway antes de criá-lo.

## Estado/limites

O código não armazena segredos no GitHub. A emissão depende das variáveis privadas, de um volume persistente e da autorização QR pelo administrador. As mensagens de keys não são gravadas em logs. O bot de revendedores não modifica o projeto `.3105`.
