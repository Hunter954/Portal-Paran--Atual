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

