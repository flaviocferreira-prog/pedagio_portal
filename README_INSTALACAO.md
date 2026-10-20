# PEDAGIO PORTAL portátil

1. Baixe `PEDAGIO_PORTAL_PORTATIL_v1.0.0.zip` na página de Releases.
2. Extraia a pasta `PEDAGIO_PORTAL` em `Documentos` ou em outro diretório com permissão de escrita.
3. Execute `INSTALAR_PEDAGIO.bat` uma vez. Ele cria os diretórios operacionais e o atalho **Alerta Logística**; não instala Python nem baixa bibliotecas.
4. Abra o atalho ou execute `INICIAR_PEDAGIO.bat`.

O pacote já inclui Python, bibliotecas e recursos internos em `_internal`. Dados locais ficam ao lado do executável em `data/`, `logs/` e `backups/`; atualizações não devem sobrescrever esses diretórios. Caso use sincronização com Google Sheets, preencha `GOOGLE_SYNC_URL` e `GOOGLE_SYNC_TOKEN` somente no `.env` local.

Antes de usar um ZIP baixado, confira o arquivo `.sha256` publicado junto dele. Em caso de falha, consulte `logs\instalacao_pedagio.log` e `logs\servidor_erro.log`.
