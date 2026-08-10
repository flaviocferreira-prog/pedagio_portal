# Conferência de Volumetria

Aplicação web local em Python puro para conferência física de caixas de um palete.

## Instalação para o colaborador

O colaborador executa somente `instalar.bat`. Ele baixa a versão atual do GitHub, instala em `Documentos\Conferencia Volumetria`, prepara Python 3.11 ou superior quando necessário, cria o ambiente virtual, banco SQLite, `.env`, logs, backups e o atalho **CONFERENCIA VOLUMETRIA** na Área de Trabalho.

O atalho inicia o servidor local, espera a resposta e abre o navegador uma única vez. Novos cliques enquanto o servidor estiver ativo não criam outras abas.

## Fluxo oficial

1. Identifique o colaborador pela matrícula na tela inicial. O cadastro rápido fica ao lado de `Acessar`.
2. Na tela seguinte, selecione somente o arquivo `.xlsx` ou `.csv`. A matrícula vem da sessão e não faz parte do upload.
3. O arquivo deve ter a coluna `CAIXA_ESTOQUE` ou uma variação aceita do cabeçalho. O limite padrão é 10 MB.
4. Cada upload cria uma conferência independente em `EM_ABERTO`. Depois que a lista é renderizada, a tela chama `/start` de forma idempotente para iniciar o cronômetro; enquanto ela estiver aberta, o backend e a tela bloqueiam nova importação para o mesmo colaborador.
5. Uma caixa esperada fica verde e `CONFERIDA`; uma não esperada fica vermelha como `SOBRA`, com código e horário; repetição fica `DUPLICADA` sem alterar os totais.
6. Atingir 100% não finaliza automaticamente. A finalização explícita exige zero faltantes; se houver sobras, o operador precisa confirmar que fez o tratamento físico. A confirmação fica registrada no SQLite com colaborador, horário e quantidade. Leituras duplicadas permanecem apenas como auditoria e não bloqueiam a finalização.
7. `FINALIZADA` e `CANCELADA` preservam caixas e eventos no histórico. Não há reinício ou reaproveitamento do mesmo palete; uma nova importação sempre gera outro identificador.
8. A finalização é exclusivamente local: grava a conferência no SQLite, marca a sincronização como `PENDENTE`, libera uma nova importação e não depende de internet ou serviço externo.

## Sincronização com Google Sheets

A integração segue um único fluxo de backend:

```text
SQLite -> serviço Python -> HTTP POST JSON -> Google Apps Script -> Google Sheets
```

Não existe login Google no navegador, popup, OAuth, `postMessage` ou `BroadcastChannel`. O segredo compartilhado permanece somente no `.env` local e nas Script Properties do Apps Script.

- **Sincronizar pendentes** envia todas as conferências `FINALIZADA + PENDENTE`, divididas em lotes.
- O agendador do backend usa o mesmo serviço a cada 30 minutos.
- Timeout, falha de rede ou resposta inválida mantêm o registro como `PENDENTE` para uma tentativa posterior.
- O estado `SINCRONIZANDO` é reservado atomicamente no SQLite e reservas interrompidas são recuperadas automaticamente.
- O Apps Script é idempotente por `ID_CONFERENCIA`; uma resposta `ALREADY_EXISTS` também confirma a sincronização local.
- Itens esperados, bipagens, tentativas, confirmações, divergências, sobras e auditoria são enviados como relações completas, sem agrupar códigos repetidos e sem limitar as bipagens.

### Publicar o Apps Script

1. Crie a planilha de destino e copie seu ID, o trecho entre `/d/` e `/edit` na URL.
2. Crie um projeto no Google Apps Script e substitua o editor pelo conteúdo de `google_apps_script/Code.gs`. Use também o manifesto `google_apps_script/appsscript.json`.
3. Em **Configurações do projeto > Propriedades do script**, crie:
   - `SPREADSHEET_ID`: ID da planilha.
   - `SYNC_SHARED_SECRET`: segredo longo, aleatório e exclusivo.
4. Em **Implantar > Nova implantação > Aplicativo da Web**, execute como o proprietário e permita acesso ao Web App sem login Google. A autenticação da integração é feita pelo segredo no corpo JSON.
5. Copie a URL terminada em `/exec` para `APPS_SCRIPT_URL` no `.env` do Portal e copie o mesmo segredo para `SYNC_SHARED_SECRET`.
6. Reinicie o Portal. O indicador no cabeçalho deve mudar para **Tudo sincronizado** ou mostrar a quantidade pendente.

O script cria e mantém as abas `CONFERENCIAS`, `ITENS`, `BIPAGENS`, `TENTATIVAS`, `CONFIRMACOES`, `DIVERGENCIAS`, `SOBRAS_RESUMO`, `SOBRAS`, `TRATAMENTOS_SOBRA`, `ITENS_TRATAMENTO` e `AUDITORIA`. A linha de `CONFERENCIAS` é gravada por último, funcionando como marcador de conclusão para tentativas idempotentes.

Ao alterar `Code.gs`, publique uma nova versão da implantação. Para trocar o segredo, atualize a Script Property e o `.env` juntos e reinicie o Portal. Nunca versione o `.env` nem grave o segredo em HTML, JavaScript ou URLs.

### Configuração local

```dotenv
APPS_SCRIPT_URL=https://script.google.com/macros/s/SEU_DEPLOYMENT_ID/exec
SYNC_SHARED_SECRET=SEU_SEGREDO_LONGO_E_ALEATORIO
SYNC_INTERVAL_MINUTES=30
SYNC_BATCH_SIZE=10
SYNC_MAX_RETRIES=3
SYNC_RETRY_DELAY=2
SYNC_CONNECT_TIMEOUT=5
SYNC_READ_TIMEOUT=30
SYNC_RESERVATION_TIMEOUT_MINUTES=15
```

Em atualizações, o instalador preserva o `.env` e acrescenta somente as duas chaves obrigatórias vazias caso ainda não existam. Preencha-as manualmente. Os nomes antigos `GOOGLE_APPS_SCRIPT_URL` e `GOOGLE_SYNC_SECRET` não são usados.

## Testes

```powershell
py -m unittest discover -s tests -v
```

## Rotas

| Método | Rota | Finalidade |
| --- | --- | --- |
| `GET` | `/` | Identificação e cadastro rápido |
| `GET` | `/conference` | Upload e conferência |
| `POST` | `/api/access` | Cria a sessão operacional |
| `POST` | `/api/colaboradores/cadastro-rapido` | Cadastra colaborador |
| `POST` | `/api/logout` | Encerra a identificação atual |
| `GET` | `/api/sync/status` | Consulta fila e saúde conhecida da sincronização |
| `POST` | `/api/sync/pending` | Sincroniza todos os registros pendentes pelo backend |
| `POST` | `/api/conferences` | Importa `.xlsx` ou `.csv` em Base64/JSON |
| `GET` | `/api/conferences/{public_id}` | Consulta a conferência |
| `GET` | `/api/conferences/{public_id}/boxes` | Consulta as caixas esperadas |
| `POST` | `/api/conferences/{public_id}/start` | Compatibilidade idempotente; não reinicia o cronômetro |
| `POST` | `/api/conferences/{public_id}/scan` | Registra uma bipagem |
| `POST` | `/api/conferences/{public_id}/finish` | Finaliza sem faltantes e exige confirmação booleana das sobras, quando houver |
| `POST` | `/api/conferences/{public_id}/cancel` | Cancela e preserva o histórico |
