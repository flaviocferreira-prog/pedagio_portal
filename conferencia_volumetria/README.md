# Conferência de Volumetria

Aplicação web local em Python puro para conferência física de caixas de um palete.

## Requisitos

- Python 3.10 ou superior;
- `openpyxl`, exclusivamente para leitura de arquivos `.xlsx`.

## Execução

```powershell
cd conferencia_volumetria
py app.py
```

Abra `http://127.0.0.1:8080`.

## Fluxo oficial

1. Identifique o colaborador pela matrícula na tela inicial. O cadastro rápido fica ao lado de `Acessar`.
2. Na tela seguinte, selecione somente o arquivo `.xlsx` ou `.csv`. A matrícula vem da sessão e não faz parte do upload.
3. O arquivo deve ter a coluna `CAIXA_ESTOQUE` ou uma variação aceita do cabeçalho. O limite padrão é 10 MB.
4. Cada upload cria uma conferência independente em `EM_ABERTO`. Depois que a lista é renderizada, a tela chama `/start` de forma idempotente para iniciar o cronômetro; enquanto ela estiver aberta, o backend e a tela bloqueiam nova importação para o mesmo colaborador.
5. Uma caixa esperada fica verde e `CONFERIDA`; uma não esperada fica vermelha e `DIVERGENTE`; repetição fica `DUPLICADA` sem alterar os totais.
6. Atingir 100% não finaliza automaticamente. A finalização explícita só é permitida sem faltantes, divergências, sobras ou duplicidades.
7. `FINALIZADA` e `CANCELADA` preservam caixas e eventos no histórico. Não há reinício ou reaproveitamento do mesmo palete; uma nova importação sempre gera outro identificador.

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
| `POST` | `/api/conferences/{public_id}/finish` | Finaliza sem pendências |
| `POST` | `/api/conferences/{public_id}/cancel` | Cancela e preserva o histórico |
| `POST` | `/api/conferences/{public_id}/sync` | Registra tentativa de sincronização |
