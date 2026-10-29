# Aplicativo Paraná Pop

O portal pode ser instalado pelo botão flutuante no canto inferior direito. O manifesto usa o ícone fornecido em 192 e 512 pixels, uma versão maskable e um ícone próprio para a Tela de Início do iOS.

Dentro do aplicativo instalado, o botão flutuante e a oferta de instalação ficam ocultos. O leitor gerencia os alertas pelo botão **Notificações** no rodapé. A primeira página da sessão mostra somente a marca sobre fundo azul, com uma entrada breve do ícone e saída suave; navegar entre matérias não repete a abertura. A preferência de movimento reduzido é respeitada. A tela nativa anterior ao carregamento é gerada pelo sistema operacional a partir do manifesto.

Os ícones `icon-v3-*` usam transparência real; o maskable e o ícone iOS usam o mesmo azul sólido `#00c8e8` do manifesto e da animação, sem a antiga imagem retangular por trás. Os URLs dos ícones foram versionados para acionar a atualização da identidade instalada. O Android pode conservar a imagem anterior até atualizar o WebAPK; reinstalar pelo site aplica os ícones atuais. A logo transparente foi extraída da imagem original pela ferramenta integrada ImageGen e normalizada para os tamanhos nativos. Prompt: remover somente o fundo ciano externo, preservando o P, seus contornos e as faixas amarela, rosa e ciano, sem adicionar moldura ou sombra.

O widget v4 renova uma vez o cache HTTP do manifesto com `fetch(..., {cache: 'reload'})` e reapresenta o mesmo link ao navegador, para substituir respostas antigas que tinham `max-age=3600`. Não altera o URL do manifesto, o `id`, inscrições push ou dados locais. Isso atualiza a metadata disponível ao navegador; a reconstrução da primeira tela nativa ainda depende do WebAPK. A versão anterior da abertura pode continuar no pacote mesmo após a atualização do HTML. Diagnóstico manual no Android/Chrome: `chrome://webapks`, localizar o Paraná Pop e solicitar `Update`, conforme o procedimento oficial do Chrome; não confundir esta tela nativa com a animação HTML posterior.

No Android/Chrome/Edge, o botão abre a instalação nativa quando disponível. No iPhone/iPad, a tela explica **Compartilhar → Adicionar à Tela de Início**. Abra pelo novo ícone para permitir notificações no iOS. Navegadores internos de redes sociais recebem instruções para abrir no navegador do aparelho.

Os alertas são opcionais. No primeiro acesso pelo aplicativo instalado, após a animação de abertura, aparece um convite com **Ativar notificações** e **Agora não**. O pedido nativo de permissão ocorre somente ao tocar em ativar, como exigem Android/iOS. O convite não se repete após ser visto/dispensado e não aparece para visitantes do navegador, leitores já inscritos ou que bloquearam a permissão. O leitor pode ativar/desativar depois pelo rodapé. A instalação e a autorização de notificações são ações distintas.

## Operação

- `/manifest.webmanifest`: identidade e configuração do aplicativo.
- `/sw.js`: service worker com escopo `/`; não armazena páginas de notícias, admin, APIs ou mídia no cache. Armazena apenas a tela offline e os ícones.
- `/api/pwa/config`: somente a chave pública VAPID.
- `/api/pwa/subscribe` e `/api/pwa/unsubscribe`: cadastro e cancelamento com validação de origem e da inscrição.
- As chaves VAPID são geradas uma única vez e ficam no PostgreSQL (`site_setting`, chave `pwa_identity_v1`). Não apagar/alterar esse registro: instalações existentes dependem da mesma chave. O backup existente do banco inclui essas chaves e as inscrições.
- `push_subscription` guarda inscrições; `push_delivery` guarda tentativas e confirmações. Cancelamentos removem os registros associados. Endpoints expirados (404/410) são removidos automaticamente.
- O dispatcher roda em uma thread a cada 30 segundos. Um advisory lock do PostgreSQL impede envio concorrente entre workers. Cada ciclo processa no máximo 50 entregas; erros temporários têm até três tentativas com intervalo crescente. Recibos antigos são removidos após sete dias.
- Só recebem alertas publicações posteriores à ativação do serviço e à inscrição daquele aparelho, com no máximo 24 horas. Rascunhos, publicações futuras e edições já notificadas não disparam. O dispatcher cobre publicação pelo admin, bot, hub, importação e agendamento sem depender de uma rota de publicação específica.
- `PWA_PUSH_WORKER=0` desativa o envio automático; use em staging. `flask --app wsgi push-dispatch` executa um ciclo manual. A entrega segue a disponibilidade e as permissões do navegador/aparelho.

## Validação

```sh
python -m pytest -q
node --test tests/pwa-client.test.cjs
```

Os testes Python usam banco isolado e transporte push simulado. Os testes JavaScript verificam os fluxos de instalação/consentimento e o service worker sem navegador. A instalação e o recebimento real devem ser conferidos em aparelhos Android e iOS.
