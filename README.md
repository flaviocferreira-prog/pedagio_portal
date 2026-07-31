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
4. A conferência nasce em `READY`, sem horário inicial. Depois de renderizar todas as caixas, o navegador chama o início idempotente.
5. Uma caixa esperada fica verde e `CONFERIDA`; uma não esperada fica vermelha e `DIVERGENTE`; repetição fica `DUPLICADA` sem alterar os totais.
6. A finalização é permitida somente sem faltantes e sem divergentes.
7. O reinício encerra a tentativa atual, preserva o histórico e cria uma nova tentativa com o mesmo palete, arquivo e caixas esperadas.

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
| `POST` | `/api/conferences/{public_id}/start` | Início idempotente |
| `POST` | `/api/conferences/{public_id}/scan` | Registra uma bipagem |
| `POST` | `/api/conferences/{public_id}/finish` | Finaliza sem pendências |
| `POST` | `/api/conferences/{public_id}/restart` | Cria nova tentativa |
| `POST` | `/api/conferences/{public_id}/sync` | Registra tentativa de sincronização |
