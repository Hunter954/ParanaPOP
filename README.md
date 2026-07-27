# Paraná Pop - Serviço WhatsApp

Serviço separado em **Node.js + Baileys** para enviar as artes geradas pelo admin do Paraná Pop para um grupo do WhatsApp.

> Importante: Baileys usa WhatsApp Web de forma não oficial. Use um número dedicado, como combinado, e mantenha volume persistente para a sessão.

## Endpoints compatíveis com o admin Flask

```txt
GET  /status
GET  /qr
GET  /groups
POST /send-message
POST /send-image
POST /send-news
POST /logout
```

Todos aceitam proteção por token usando:

```http
Authorization: Bearer SEU_TOKEN
```

ou:

```http
X-Service-Token: SEU_TOKEN
```

## Variáveis de ambiente

Copie `.env.example` para `.env` localmente.

```env
PORT=3001
SERVICE_TOKEN=troque-essa-chave
CORS_ORIGINS=*
AUTH_DIR=/app/auth
BOT_NAME=ParanaPOP Bot
MEDIA_DOWNLOAD_TIMEOUT_MS=30000
MAX_MEDIA_BYTES=15000000
```

No Flask/Admin do Paraná Pop, use o mesmo token:

```env
WHATSAPP_SERVICE_TOKEN=troque-essa-chave
WHATSAPP_SERVICE_URL=https://url-do-servico-whatsapp.railway.app
```

## Rodando local

```bash
npm install
cp .env.example .env
npm start
```

Acesse:

```txt
http://localhost:3001/status
http://localhost:3001/qr
```

Abra `/qr`, copie o `qr_data_url` ou veja pelo admin do Paraná Pop se ele já estiver apontando para este serviço.

## Railway

Crie um repositório separado no GitHub com estes arquivos.

No Railway:

1. New Project
2. Deploy from GitHub repo
3. Selecione este repositório
4. Configure as variáveis de ambiente
5. Crie um volume persistente
6. Monte o volume em:

```txt
/app/auth
```

Start command:

```bash
npm start
```

## Payload esperado em `/send-news`

O admin Flask envia algo parecido com:

```json
{
  "group_id": "1203630xxxxx@g.us",
  "title": "Título da matéria",
  "summary": "Resumo curto da matéria",
  "url": "https://www.paranapop.com.br/noticia/exemplo",
  "instagram_description": "Texto sugerido para Instagram/Facebook",
  "images": [
    { "format": "feed", "url": "https://.../feed.jpg" },
    { "format": "stories", "url": "https://.../stories.jpg" },
    { "format": "facebook", "url": "https://.../facebook.jpg" }
  ]
}
```

O serviço envia no grupo:

1. Arte Feed
2. Arte Stories
3. Arte Facebook
4. Mensagem com título, resumo, descrição sugerida e link

## Primeiro pareamento

Depois de subir no Railway:

1. Coloque a URL do serviço no admin `/admin/whatsapp`
2. Salve o token, se configurado
3. Abra a tela de WhatsApp do admin
4. Escaneie o QR Code com o número dedicado
5. Clique para carregar os grupos
6. Salve o grupo padrão
7. Envie uma mensagem de teste

## Observações

- Use um número dedicado para reduzir risco operacional.
- Não apague o volume `/app/auth`, senão precisará escanear QR Code novamente.
- Não use para disparos em massa. Este serviço é para grupo interno da equipe.

## Gerador de vídeo padrão

No grupo do Paraná Pop, a opção **8 - Vídeo Padrão** do menu recebe um vídeo, título e categoria, e devolve somente o formato vertical 1080x1920 para Reels/Stories.

Variáveis novas:

```env
MAX_VIDEO_BYTES=80000000
VIDEO_PARANAPOP_API_URL=https://seu-site.com/admin/api/whatsapp-bot/generate-video
VIDEO_PARANAPOP_TOKEN=mesmo-token-do-bot

# Portal Trivox (/video com detecção automática de formato):
VIDEO_TRIVOX_GROUP_ID=1203630xxxx@g.us
VIDEO_TRIVOX_API_URL=https://seu-site.com/admin/api/whatsapp-bot/generate-trivox-video
VIDEO_TRIVOX_TOKEN=mesmo-token-do-bot
```

No grupo do Trivox, a Natália envia o vídeo, o bot detecta sozinho se ele é vertical (stories) ou horizontal, pede o título da matéria em seguida e devolve o vídeo no padrão do Portal Trivox.
