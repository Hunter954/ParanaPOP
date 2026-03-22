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
