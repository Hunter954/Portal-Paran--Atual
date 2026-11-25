# Radar Paraná Atual

Abra **Radar de Notícias** no administrativo. Em **Fontes e busca → Adicionar fonte**, informe o endereço de um RSS, Atom, sitemap de notícias ou página pública que divulgue um feed ou apresente links em elementos de artigo. A opção **Testar** verifica a leitura e a elegibilidade. **Coletar agora** grava os itens elegíveis. A busca percorre a base coletada e pode ser limitada por fonte, município, região e categoria. Resultados e falhas aparecem no histórico.

O recorte geográfico é aplicado a cada item antes de ele entrar na fila. A seção da fonte serve para organização. A evidência extraída da manchete ou do começo do resumo aparece em **Fontes e evidências**. Notícias com localização indefinida, menções incidentais ou localização externa identificada no título ficam fora da fila. A lista de municípios vem da [API de Localidades do IBGE](https://servicodados.ibge.gov.br/api/v1/localidades/estados/41/municipios). O filtro é conservador e pode excluir matérias cujo contexto local esteja apenas no corpo completo.

## Operação

- Utilize o mesmo PostgreSQL do CMS com `DATABASE_URL`. Faça backup e rode `flask --app app.radar.worker:create_worker_app radar migrate`. A migração só cria tabelas do Radar.
- Rode `flask --app app.radar.worker:create_worker_app radar seed` para instalar os feeds iniciais. Execute `flask --app app.radar.worker:create_worker_app radar worker` em um único serviço com reinício controlado. PostgreSQL impede que dois workers ativos compartilhem a fila.
- Para a IA, defina `RADAR_OLLAMA_URL` no portal e no worker, com acesso ao Ollama pela rede interna no worker. O padrão de `RADAR_OLLAMA_MODEL` é `qwen3:4b-instruct-2507-q4_K_M`. As gerações são sequenciais e exigem revisão editorial.
- A publicação no CMS exige revisão da versão atual, confirmação e `RADAR_ENABLE_PUBLISHING=1` na aplicação e no worker. Opcionalmente use `RADAR_PUBLISHER_IDS` para delimitar os IDs de administradores autorizados, separados por espaços. O padrão mantém a publicação desativada.
- O adaptador de WhatsApp está em `ops/whatsapp` e utiliza o gateway Baileys existente. Aplique `python ops/whatsapp/install_adapter.py /copia/do/gateway` antes de construir sua imagem. Configure `RADAR_WHATSAPP_TOKEN` no portal, worker e gateway e `RADAR_WHATSAPP_URL` no portal e no worker. A configuração do gateway usa `RADAR_WHATSAPP_WEBHOOK` para o endpoint interno do portal `/internal/radar/whatsapp`. Os administradores habilitam alertas no próprio perfil do Radar. Para conhecer os comandos suportados envie `RADAR AJUDA` em conversa privada.

## Rollback

Encerre o worker, restaure a imagem anterior do portal e preserve o backup. Manter as tabelas do Radar permite retomar a implantação sem perder o histórico. O comando `radar downgrade` remove dados do Radar e deve ser usado somente se essa exclusão tiver sido aprovada.


## Registro da implantação em 24/11/2026

O portal e o gateway foram atualizados como serviços Docker Swarm do Dokploy. As imagens de referência para rollback são `parana-atual-radar-base:6ea2ed4` para o portal e `127.0.0.1:5000/paranapop-trivox-whatsapp:foztotal-prints-20261123` para o gateway. O backup PostgreSQL validado está no host em `/root/parana-atual-radar-20261124/before-radar.pg.dump`.

O worker `radar-parana-worker` e o Ollama `radar-parana-ollama` têm reinício automático e limites de CPU e memória. A geração tem tempo limite de 38 segundos no Ollama. O serviço usa rede interna para inferência. O Dokploy está configurado para a branch `radar-parana-atual` no portal e para a imagem `127.0.0.1:5000/paranapop-trivox-whatsapp:parana-atual-radar-chat-20261124` no gateway, com as variáveis de integração registradas nos dois serviços.

Para retirar o Radar, pare `radar-parana-worker` e restaure as imagens anteriores dos serviços. Remova as configurações `RADAR_*` dos serviços ao restaurar o estado anterior. Use o backup apenas se houver necessidade de restaurar dados. O portal anterior funciona com as tabelas do Radar preservadas.


## WhatsApp em grupo e ferramentas do portal

Abra **WhatsApp** no menu principal do administrativo, escolha o grupo da redação e ative o bot. **Enviar novas matérias publicadas** compartilha artigos do CMS e do Radar. **Enviar alertas de pautas do Radar** aplica a pontuação escolhida a pautas novas do Paraná. As duas opções são independentes. A seleção persiste no volume de autenticação já usado pelo gateway. Grupos configurados para outros portais aparecem bloqueados.

No grupo selecionado, um administrador ativo com WhatsApp cadastrado pode usar `menu` e os comandos `RADAR`. O navegador também oferece **Estúdio de mídia** para artes e vídeos, **Calendário da home** para escolher uma edição e **Print automático** para consultar os comprovantes já registrados e baixar um Word.

A captura usa `PARANAATUAL_AD_REPORTS_URL` e `PARANAATUAL_AD_REPORTS_TOKEN`. Execute a migração do Radar antes de ligar o worker atualizado. A nova tabela `radar_group_deliveries` registra partilhas de matérias e alertas, com controle de duplicatas. Envios começam a partir da ativação do grupo ou da automação. A revisão e a confirmação da versão são obrigatórias para publicar uma pauta do Radar pelo painel.



## Radar na conversa do WhatsApp

Envie MENU e escolha 9. O submenu oferece 1 Pautas do Paraná, 2 Urgentes do Paraná, 3 Buscar notícia por assunto — Brasil e mundo, 4 Minhas buscas e 0 Menu principal. Escolha uma pauta pelo número, prepare a prévia e use as opções numeradas para publicar, refazer, conferir fontes ou editar texto e foto. As listas têm paginação. Os comandos antigos continuam disponíveis para quem já os utiliza.

Responda POSTAR na mensagem da pauta para receber a prévia. O worker lê a reportagem completa e procura a foto creditada no artigo. A foto anexada à prévia é preservada na publicação, junto com o crédito cadastrado. O campo de licença permanece como metadado; quando estiver vazio, a foto não é descartada. Sem uma foto válida, a publicação aguarda a imagem e informa o editor. Na prévia responda PUBLICAR para confirmar ou REFAZER para gerar outro texto. TÍTULO, RESUMO e TEXTO continuam disponíveis para edições pontuais. A prévia e a capa são associadas à versão atual da pauta.

A migra??o cria radar_chat_sessions, radar_chat_cards, radar_chat_draft_replies e radar_draft_media. A configura??o do grupo e os alertas permanecem no gateway existente. A publica??o conserva as permiss?es definidas em RADAR_PUBLISHER_IDS e as verifica??es de duplicidade do Radar.

Use RADAR FONTES para listar o cat?logo, RADAR FONTE ADICIONAR nome | URL | tipo para cadastrar e RADAR COLETAR ID para solicitar uma coleta. Para agendar, responda RADAR AGENDAR DD/MM/AAAA HH:MM na pr?via revisada, no hor?rio de Bras?lia. RADAR CANCELAR ID devolve uma pauta agendada ? revis?o.

## Administração do portal no WhatsApp

Use MENU, 2 e 1 para criar uma matéria normal. Envie primeiro o título e o texto na mesma mensagem, separados por uma linha em branco, ou em duas mensagens. A prévia aceita PUBLICAR, AGENDAR, edição por campo, BUSCAR FOTO e o envio de uma imagem como resposta. Categorias, usuários, publicidade, SEO, métricas e biblioteca ficam no menu principal.

Nas prévias do Radar e nas matérias com fonte cadastrada, BUSCAR FOTO consulta exclusivamente a reportagem vinculada. A foto original e seu crédito são anexados à versão atual da prévia e enviados ao WhatsApp. Não há busca por assunto, Google ou Wikimedia como alternativa para essas capas. Se a imagem não puder ser obtida, o bot avisa e mantém a prévia sem foto. Sugestões antigas sem vínculo com a reportagem não podem ser anexadas; imagens enviadas pela redação continuam disponíveis.

O worker prioriza solicitações da redação. A geração antecipada de pautas fica desativada por padrão no modo de matéria completa; RADAR_PREFETCH=1 permite ativá-la quando houver capacidade disponível. A reportagem é lida uma vez; o download da foto original acontece durante a geração do texto. A primeira prévia pronta inclui texto, foto e crédito na mesma mensagem. Se a geração ficar incompleta, a conversa recebe as fontes e a opção 5 para editar o texto e concluir a prévia com revisão. A publicação continua vinculada à confirmação da versão.


## Fotos originais — correção de 24/11/2026

Imagem do portal e do worker: `parana-atual-radar:original-article-photo-20261124`. A prévia #98 foi usada para conferir a extração da foto creditada a Bruno Fernandes na reportagem original do g1. A foto foi verificada por SHA-256, sem alterações nos pixels. Os 49 testes do portal e do Radar passaram, incluindo falha de extração, isolamento de versões, bloqueio de cartões antigos e envio da foto no fluxo de prévia. A busca genérica continua restrita à consulta explícita de imagens na biblioteca, sem vincular seus resultados às matérias.


## Publicação preserva a foto — 24/11/2026

Portal e worker usam `parana-atual-radar:publish-original-photo-20261124`. O Radar publica a mesma imagem da versão aprovada, com crédito e descrição, sem substituir o arquivo por uma logo quando o campo de licença está vazio. O CMS no WhatsApp também aguarda uma foto quando a matéria não tem imagem válida. Os 52 testes aprovados incluem o fluxo completo de extração, prévia, PUBLICAR, página pública, crédito, Open Graph e acesso anônimo aos mesmos bytes da foto.


## Busca por assunto no WhatsApp — 24/11/2026

O caminho MENU → 9 → 3 solicita apenas o assunto e oferece períodos de 7 dias, 30 dias ou sem limite de data. O worker consulta o RSS público do Bing Notícias, decodifica os links originais e mostra título, fonte e data de cada resultado. A consulta não vira uma alegação factual e as miniaturas do buscador são ignoradas. A foto continua vindo da reportagem selecionada pelo fluxo de fotos originais, com a mesma imagem na prévia e na publicação.

Escolher um resultado inicia a preparação da matéria. A publicação continua exigindo a escolha de PUBLICAR na prévia. O resultado importado usa a região interna Busca livre e uma fonte inativa e arquivada, sem ampliar o monitoramento, a fila automática, os alertas ou a preparação antecipada de pautas do Paraná. O histórico é limitado ao administrador e à conversa. Navegar para outro menu ou iniciar outra busca impede a entrega de resultados antigos naquela navegação.

A migração aditiva cria radar_manual_searches. Portal e worker usam a imagem parana-atual-radar:search-menu-20261124. O gateway e os grupos existentes são preservados. As escolhas são numeradas em mensagens de texto, sem exigir que o editor memorize comandos. Somente o assunto pesquisado e os campos em edição são texto livre.


## Prévia com foto e preparação mais rápida — 24/11/2026

Selecionar uma reportagem da busca já inicia a preparação. O worker lê o artigo uma vez e baixa a foto original em paralelo à redação. A foto é gravada na mesma versão do rascunho antes da entrega. A prévia padrão chega como uma imagem cuja legenda contém título, texto, fonte, crédito e opções de revisão. Textos longos são divididos em mensagens do mesmo envio. A versão e a origem da foto continuam verificadas antes de anexar ou publicar.

A geração usa o mesmo modelo qwen3.5:2b-q4_K_M com o mesmo formato editorial de manchete, subtítulo e dois parágrafos. A inferência usa quatro threads em vez de três e o limite de CPU do Ollama passa de três para quatro núcleos, preservando os demais limites. O modelo fica carregado por 24 horas para reduzir a espera por inicialização. As validações de fonte acessível, números, idioma, atribuições e semelhança continuam ativas. O andamento informa a leitura, a redação e o tempo transcorrido, com entrega automática da prévia. Se a foto não puder ser obtida, a prévia informa a falha e oferece tentar novamente ou enviar uma imagem; a publicação continua aguardando uma foto válida.

Portal e worker: parana-atual-radar:photo-speed-20261124. Não há alteração de esquema nem de grupo do WhatsApp.


## Matérias completas — 25/11/2026

O prompt radar-pt-br-v6-full substitui o formato antigo de nota com dois parágrafos. A leitura mantém até 16 mil caracteres da reportagem principal e 8 mil da fonte complementar. A geração recebe até 12 mil e 6 mil caracteres, incluindo informações do final dos textos. Chamadas de outras notícias são descartadas sem interromper a leitura dos parágrafos seguintes.

A extensão acompanha a informação disponível: fontes com 400 a 799 palavras geram uma solicitação de 350 a 550 palavras, solicitando cerca de seis parágrafos; materiais mais ricos podem gerar de 450 a 650 palavras. Fontes menores têm metas proporcionais para não criar fatos ou encher o texto. Cabeçalhos repetidos pelo modelo no início do corpo são removidos sem cortar o texto. A validação exige ao menos quatro parágrafos desenvolvidos para fontes extensas e rejeita notas curtas, parágrafos incompletos ou repetidos, corte pelo limite de tokens e números que não constam no material fornecido. As verificações de redação própria, idioma e atribuição de fontes permanecem ativas. Se houver trechos extensos próximos da fonte, há uma única revisão automática dos parágrafos envolvidos, dentro do mesmo orçamento de tempo. A revisão preserva o conteúdo e passa novamente pelas verificações de extensão e originalidade. Frases factuais isoladas em uma matéria longa são diferenciadas de cópia extensa; trechos longos e repetição de porções substanciais continuam bloqueados.

O modelo das matérias completas é qwen3:4b-instruct-2507-q4_K_M, com contexto de 4096 ou 8192 tokens conforme o material e orçamento de saída de 1800 tokens. O limite de memória do Ollama é 5500 MB. O orçamento de uma redação completa, incluindo uma eventual revisão, é 420 segundos. A prévia chega automaticamente ao terminar; a produção de uma matéria completa exige mais processamento que a nota anterior. O download da foto continua em paralelo e usa exclusivamente a reportagem vinculada.

Intertítulos são armazenados como linhas iniciadas por ## e renderizados como h2 no site, com escape de HTML. No WhatsApp aparecem em negrito. Textos longos são enviados integralmente em partes identificadas; a primeira parte contém a foto, o título, o início do texto e o crédito. Todas as partes têm cartões vinculados à versão atual e a última oferece as opções de revisão e publicação. O conteúdo completo é preservado no site.

Notas automáticas antigas com menos de 180 palavras são reconhecidas ao solicitar uma nova preparação. Revisões extensas feitas pela redação são preservadas. A alteração não reescreve automaticamente o acervo publicado. A matéria 141, pauta 98, foi revisada separadamente a pedido da redação: passou de 77 para 471 palavras, com três intertítulos, preservando a URL, a foto original e o crédito de Bruno Fernandes.

Portal e worker usam parana-atual-radar:full-articles-20261124. Não há alteração de esquema nem de grupo do WhatsApp. Os 86 testes incluem texto completo, dados ao final da fonte, bloqueio de notas curtas, corte de geração, intertítulos seguros e prévias longas no WhatsApp com foto e texto preservados.
