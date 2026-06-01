# Patch do Admin - Paraná POP

## O que foi implementado

- Painel administrativo redesenhado com navegação lateral.
- Sessões separadas para:
  - Dashboard
  - Matérias
  - Categorias
  - Biblioteca de mídia
  - Configurações
  - Publicidade
- CRUD de matérias locais.
- CRUD de categorias.
- Upload de imagens/arquivos para volume persistente do Railway.
- Uso de arquivo **ou** link direto para:
  - logo do site
  - imagem destacada da matéria
  - banner/publicidade
- Rota pública para servir uploads locais: `/media/...`
- Configuração de `MEDIA_ROOT` para funcionar com volume.
- Ação manual para sincronizar WordPress pelo admin.

## Variáveis importantes no Railway

Configure no serviço:

- `MEDIA_ROOT=/data/uploads`
- `MEDIA_URL_PREFIX=/media`

Depois crie/mapeie um volume no Railway apontando para:

- `/data/uploads`

## Observação

Este patch foi focado apenas no **painel admin** e no fluxo de mídia persistente.
A home e a página pública de post não tiveram o layout alterado.

## WhatsApp automático para matérias

Adicionado painel `/admin/whatsapp` para configurar um serviço externo de WhatsApp/Baileys.

### O que foi incluído

- Menu lateral **WhatsApp** no admin.
- Configuração da URL do serviço WhatsApp externo.
- Campo para grupo padrão (`120...@g.us`) e nome do grupo.
- Opções para enviar artes de Feed, Stories e Facebook.
- Template de mensagem com variáveis: `{{titulo}}`, `{{resumo}}`, `{{url}}`, `{{categoria}}`.
- Botão de teste para enviar mensagem ao grupo selecionado.
- Botão manual em matéria publicada: **Gerar e enviar WhatsApp**.
- Envio automático quando uma matéria local nova é publicada ou quando um rascunho vira publicação.
- Integração também no fluxo de Matérias API quando as matérias são criadas já publicadas.

### Contrato esperado do serviço externo

O serviço separado de WhatsApp deve expor:

- `GET /status`
- `GET /groups`
- `POST /send-message`
- `POST /send-news`

Payload enviado para `/send-news`:

```json
{
  "group_id": "1203630xxxx@g.us",
  "group_name": "Equipe Paraná POP",
  "post": {
    "id": 123,
    "title": "Título da matéria",
    "summary": "Resumo da matéria",
    "url": "https://www.paranapop.com.br/p/slug",
    "category": "Categoria",
    "published_at": "2026-06-01T15:00:00"
  },
  "images": [
    {"type": "feed", "label": "Feed", "size": "1080x1440", "url": "https://.../arte.png"},
    {"type": "stories", "label": "Stories", "size": "1080x1920", "url": "https://.../arte.png"},
    {"type": "facebook", "label": "Facebook", "size": "1080x1080", "url": "https://.../arte.png"}
  ],
  "caption": "Mensagem pronta",
  "description": "Título + resumo + link",
  "send_as_separate_messages": true
}
```
