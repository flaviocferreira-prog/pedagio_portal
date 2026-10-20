# Conferência de Volumetria

Aplicação web local em Python puro para conferência física de caixas de um palete.

## Instalação para o colaborador

Para uso operacional, baixe o ZIP portátil na página de Releases, extraia-o em uma pasta como `Documentos\PEDAGIO_PORTAL` e execute `INSTALAR_PEDAGIO.bat`. O instalador não baixa código, não move o projeto e não instala Python ou bibliotecas: ele valida o runtime já empacotado, preserva dados operacionais e cria o atalho **Alerta Logística** na Área de Trabalho do usuário.

Requisitos do pacote portátil: Windows 10/11 e permissão de escrita na pasta do projeto. Python e as bibliotecas já acompanham o pacote; Python 3.11 ou superior é necessário somente para reproduzir o build de desenvolvimento. O instalador não altera o Python global, proxy, certificados ou políticas de segurança.

Para reparar o runtime, baixe novamente o ZIP de Release e extraia-o sobre os arquivos da aplicação, sem substituir `data/`, `logs/`, `backups/` ou `.env`. Depois da instalação, abra **Alerta Logística**. O atalho identifica a instância local antes de abrir o navegador e não encerra processos desconhecidos. Os diagnósticos do instalador ficam em `logs\instalacao_pedagio.log`; os do servidor ficam em `logs\servidor_saida.log` e `logs\servidor_erro.log`.

O atalho abre somente a ferramenta local. Caso seja necessário, o acesso ao Senior deve ser aberto manualmente em `https://platform.senior.com.br/login/?redirectTo=https://platform.senior.com.br/senior-x/&tenant=gruposbf.com.br`.

## Fluxo oficial

1. Identifique o colaborador pela matrícula na tela inicial. O cadastro rápido fica ao lado de `Acessar`.
2. Na tela seguinte, selecione somente o arquivo `.xlsx` ou `.csv`. A matrícula vem da sessão e não faz parte do upload.
3. O arquivo deve ter a coluna `CAIXA_ESTOQUE` ou uma variação aceita do cabeçalho. O limite padrão é 10 MB.
4. Cada upload cria uma conferência independente em `EM_ABERTO`. Depois que a lista é renderizada, a tela chama `/start` de forma idempotente para iniciar o cronômetro; enquanto ela estiver aberta, o backend e a tela bloqueiam nova importação para o mesmo colaborador.
5. Uma caixa esperada fica verde e `CONFERIDA`; uma não esperada fica vermelha como `SOBRA`, com código e horário; repetição fica `DUPLICADA` sem alterar os totais.
6. Atingir 100% não finaliza automaticamente. A finalização explícita exige zero faltantes; se houver sobras, o operador precisa confirmar que fez o tratamento físico. A confirmação fica registrada no SQLite com colaborador, horário e quantidade. Leituras duplicadas permanecem apenas como auditoria e não bloqueiam a finalização.
7. `FINALIZADA` e `CANCELADA` preservam caixas e eventos no histórico. Não há reinício ou reaproveitamento do mesmo palete; uma nova importação sempre gera outro identificador.
8. A finalização é exclusivamente local: grava a conferência no SQLite e libera uma nova importação.

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
| `POST` | `/api/conferences` | Importa `.xlsx` ou `.csv` em Base64/JSON |
| `GET` | `/api/conferences/{public_id}` | Consulta a conferência |
| `GET` | `/api/conferences/{public_id}/boxes` | Consulta as caixas esperadas |
| `POST` | `/api/conferences/{public_id}/start` | Compatibilidade idempotente; não reinicia o cronômetro |
| `POST` | `/api/conferences/{public_id}/scan` | Registra uma bipagem |
| `POST` | `/api/conferences/{public_id}/finish` | Finaliza sem faltantes e exige confirmação booleana das sobras, quando houver |
| `POST` | `/api/conferences/{public_id}/cancel` | Cancela e preserva o histórico |

## Sincronização manual com Google Sheets

O SQLite continua sendo a fonte principal. A sincronização é manual pelo ícone de atualização e envia todas as conferências `FINALIZADA` pendentes em lote, sem agendador ou serviço em segundo plano.

1. Na planilha de destino, mantenha as abas `CONFERENCIAS`, `CAIXAS_CONFERENCIA`, `BIPAGENS`, `DIVERGENCIAS`, `TENTATIVAS`, `AUDITORIA` e `COLABORADORES`.
2. Publique [google_apps_script/Code.gs](google_apps_script/Code.gs) como aplicativo da Web e crie a Script Property `GOOGLE_SYNC_TOKEN`.
3. Configure no `.env` local os valores `GOOGLE_SYNC_URL` e `GOOGLE_SYNC_TOKEN`. Os demais parâmetros em `.env.example` são opcionais.

O Apps Script mantém uma chave exclusiva por aba e insere apenas IDs ausentes. O backend marca uma conferência como `SINCRONIZADO` somente após o Script confirmar todos os dados do lote. Falhas preservam o estado `PENDENTE` no SQLite.
