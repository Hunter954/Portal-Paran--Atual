# Radar Paraná Atual

Abra **Radar de Notícias** no administrativo. Em **Fontes e busca → Adicionar fonte**, informe o endereço de um RSS, Atom, sitemap de notícias ou página pública que divulgue um feed ou apresente links em elementos de artigo. A opção **Testar** verifica a leitura e a elegibilidade. **Coletar agora** grava os itens elegíveis. A busca percorre a base coletada e pode ser limitada por fonte, município, região e categoria. Resultados e falhas aparecem no histórico.

O recorte geográfico é aplicado a cada item antes de ele entrar na fila. A seção da fonte serve para organização. A evidência extraída da manchete ou do começo do resumo aparece em **Fontes e evidências**. Notícias com localização indefinida, menções incidentais ou localização externa identificada no título ficam fora da fila. A lista de municípios vem da [API de Localidades do IBGE](https://servicodados.ibge.gov.br/api/v1/localidades/estados/41/municipios). O filtro é conservador e pode excluir matérias cujo contexto local esteja apenas no corpo completo.

## Operação

- Utilize o mesmo PostgreSQL do CMS com `DATABASE_URL`. Faça backup e rode `flask --app app.radar.worker:create_worker_app radar migrate`. A migração só cria tabelas do Radar.
- Rode `flask --app app.radar.worker:create_worker_app radar seed` para instalar os feeds iniciais. Execute `flask --app app.radar.worker:create_worker_app radar worker` em um único serviço com reinício controlado. PostgreSQL impede que dois workers ativos compartilhem a fila.
- Para a IA, defina `RADAR_OLLAMA_URL` no portal e no worker, com acesso ao Ollama pela rede interna no worker. O padrão de `RADAR_OLLAMA_MODEL` é `qwen3:4b`. As gerações são sequenciais e exigem revisão editorial.
- A publicação no CMS exige revisão da versão atual, confirmação e `RADAR_ENABLE_PUBLISHING=1` na aplicação e no worker. Opcionalmente use `RADAR_PUBLISHER_IDS` para delimitar os IDs de administradores autorizados, separados por espaços. O padrão mantém a publicação desativada.
- O adaptador de WhatsApp está em `ops/whatsapp` e utiliza o gateway Baileys existente. Aplique `python ops/whatsapp/install_adapter.py /copia/do/gateway` antes de construir sua imagem. Configure `RADAR_WHATSAPP_TOKEN` no portal, worker e gateway e `RADAR_WHATSAPP_URL` no portal e no worker. A configuração do gateway usa `RADAR_WHATSAPP_WEBHOOK` para o endpoint interno do portal `/internal/radar/whatsapp`. Os administradores habilitam alertas no próprio perfil do Radar. Para conhecer os comandos suportados envie `RADAR AJUDA` em conversa privada.

## Rollback

Encerre o worker, restaure a imagem anterior do portal e preserve o backup. Manter as tabelas do Radar permite retomar a implantação sem perder o histórico. O comando `radar downgrade` remove dados do Radar e deve ser usado somente se essa exclusão tiver sido aprovada.


## Registro da implantação em 24/11/2026

O portal e o gateway foram atualizados como serviços Docker Swarm do Dokploy. As imagens de referência para rollback são `parana-atual-radar-base:6ea2ed4` para o portal e `127.0.0.1:5000/paranapop-trivox-whatsapp:foztotal-prints-20261123` para o gateway. O backup PostgreSQL validado está no host em `/root/parana-atual-radar-20261124/before-radar.pg.dump`.

O worker `radar-parana-worker` e o Ollama `radar-parana-ollama` têm reinício automático e limites de CPU e memória. O Ollama usa `qwen3:1.7b` nesta VPS após o teste do modelo de 4B ultrapassar o tempo limite. O serviço usa rede interna para inferência. O Dokploy está configurado para a branch `radar-parana-atual` no portal e para a imagem `127.0.0.1:5000/paranapop-trivox-whatsapp:parana-atual-radar-chat-20261124` no gateway, com as variáveis de integração registradas nos dois serviços.

Para retirar o Radar, pare `radar-parana-worker` e restaure as imagens anteriores dos serviços. Remova as configurações `RADAR_*` dos serviços ao restaurar o estado anterior. Use o backup apenas se houver necessidade de restaurar dados. O portal anterior funciona com as tabelas do Radar preservadas.


## WhatsApp em grupo e ferramentas do portal

Abra **WhatsApp** no menu principal do administrativo, escolha o grupo da redação e ative o bot. **Enviar novas matérias publicadas** compartilha artigos do CMS e do Radar. **Enviar alertas de pautas do Radar** aplica a pontuação escolhida a pautas novas do Paraná. As duas opções são independentes. A seleção persiste no volume de autenticação já usado pelo gateway. Grupos configurados para outros portais aparecem bloqueados.

No grupo selecionado, um administrador ativo com WhatsApp cadastrado pode usar `menu` e os comandos `RADAR`. O navegador também oferece **Estúdio de mídia** para artes e vídeos, **Calendário da home** para escolher uma edição e **Print automático** para consultar os comprovantes já registrados e baixar um Word.

A captura usa `PARANAATUAL_AD_REPORTS_URL` e `PARANAATUAL_AD_REPORTS_TOKEN`. Execute a migração do Radar antes de ligar o worker atualizado. A nova tabela `radar_group_deliveries` registra partilhas de matérias e alertas, com controle de duplicatas. Envios começam a partir da ativação do grupo ou da automação. A revisão e a confirmação da versão são obrigatórias para publicar uma pauta do Radar pelo painel.



## Radar na conversa do WhatsApp

Digite MENU e escolha 9, ou use RADAR LISTAR 10. Cada pauta chega em uma mensagem pr?pria. Informe outra quantidade, use MAIS para continuar ou RADAR REENVIAR para repetir a consulta. RADAR URGENTES 10 filtra as pautas com pontua??o a partir de 75. Os alertas autom?ticos seguem o limite escolhido e a data de ativa??o, enquanto a listagem manual inclui o hist?rico aberto.

Responda POSTAR na mensagem da pauta para receber a prévia. O worker lê a reportagem completa e procura a foto creditada no artigo. Imagens de referência ficam na prévia autenticada. Quando houver licença compatível ou o editor registrar a permissão com FOTO AUTORIZADA seguida da origem, a imagem vira capa pública. Nos outros casos, o portal usa a arte do site. Na prévia responda PUBLICAR para confirmar ou REFAZER para gerar outro texto. TÍTULO, RESUMO e TEXTO continuam disponíveis para edições pontuais. A prévia e a capa são associadas à versão atual da pauta.

A migra??o cria radar_chat_sessions, radar_chat_cards, radar_chat_draft_replies e radar_draft_media. A configura??o do grupo e os alertas permanecem no gateway existente. A publica??o conserva as permiss?es definidas em RADAR_PUBLISHER_IDS e as verifica??es de duplicidade do Radar.

Use RADAR FONTES para listar o cat?logo, RADAR FONTE ADICIONAR nome | URL | tipo para cadastrar e RADAR COLETAR ID para solicitar uma coleta. Para agendar, responda RADAR AGENDAR DD/MM/AAAA HH:MM na pr?via revisada, no hor?rio de Bras?lia. RADAR CANCELAR ID devolve uma pauta agendada ? revis?o.

## Administração do portal no WhatsApp

Use MENU, 2 e 1 para criar uma matéria normal. Envie primeiro o título e o texto na mesma mensagem, separados por uma linha em branco, ou em duas mensagens. A prévia aceita PUBLICAR, AGENDAR, edição por campo, BUSCAR FOTO e o envio de uma imagem como resposta. Categorias, usuários, publicidade, SEO, métricas e biblioteca ficam no menu principal.

O Radar busca imagens pelo assunto depois de enviar uma nova prévia. Também é possível pedir BUSCAR FOTO com um assunto mais específico. Configure a variável `RADAR_GOOGLE_IMAGES_KEY` no worker para consultar o Google Imagens via SerpApi. Sem essa configuração, o worker consulta o Wikimedia Commons e preserva os créditos e a licença. Para anexar uma opção, responda USAR FOTO.
