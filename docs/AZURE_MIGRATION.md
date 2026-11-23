# Migração Supabase → Azure

Status: código migrado e testado; deploy em produção pendente (2026-11-21).

## Destino

| Antes (Supabase)            | Depois (Azure)                                                  |
|-----------------------------|-----------------------------------------------------------------|
| Postgres via PostgREST      | Azure Database for PostgreSQL Flexible Server `zz2-tech-pg` (Spain Central, B1ms, PG 16), SQL direto (`zz2/gateways/database.py`, psycopg 3 + pool) |
| Supabase Auth (senha, JWT)  | Tabela `techSupport.app_users` (`zz2/gateways/user_directory.py`) + sessões HS256 emitidas pelo servidor (`zz2/services/session_tokens.py`) |
| —                           | Login com conta Microsoft (Outlook / Microsoft 365) via OpenID Connect (Entra ID) |
| Supabase Storage            | Azure Blob Storage, container privado `zz2-manuals`             |
| Realtime (presence)         | Endpoint de presença no próprio backend, por polling            |
| Front-end                   | **Continua** no App Service `zz2-tech-platform` (o mesmo processo FastAPI serve páginas e API) |

Schema: `sql/azure_schema.sql`. Search path das conexões: `"techSupport", zoho, public` —
as queries usam nomes de tabela sem schema.

## Configuração (variáveis novas)

```
ZZ2_DATABASE_URL=postgresql://zz2admin:<senha>@zz2-tech-pg.postgres.database.azure.com:5432/zz2?sslmode=require
ZZ2_DB_SCHEMA=techSupport
ZZ2_SESSION_SECRET=<48+ caracteres aleatórios>
ZZ2_AZURE_STORAGE_CONNECTION_STRING=<connection string>
ZZ2_AZURE_STORAGE_CONTAINER=zz2-manuals
ZZ2_ENABLE_BLOB_STORAGE=true
ZZ2_MICROSOFT_ENABLED=true
ZZ2_MICROSOFT_TENANT=organizations
ZZ2_MICROSOFT_CLIENT_ID=<app registration>
ZZ2_MICROSOFT_CLIENT_SECRET=<secret>
ZZ2_ENABLE_DB_MIRROR=true            # antes ZZ2_ENABLE_SUPABASE_MIRROR
ZZ2_TROUBLESHOOTING_STORE=auto       # valores: auto | database | local
```

Todas as variáveis `ZZ2_SUPABASE_*` deixam de existir.

## Contrato de API para o front-end

Todas as rotas exigem `Authorization: Bearer <token>` salvo indicação.

### Autenticação

- `GET /api/config` (público) → `{build, environment, saml_enabled, microsoft_enabled, password_login_enabled}`
- `POST /api/auth/login` (público) body `{email, password}` → `200 {ok, token, expires_in, user:{email, display, role}}`; `401` credenciais inválidas.
- `GET /api/auth/microsoft/login?next=/index.html` (público) → 302 para a Microsoft → volta em
  `/login.html#sso=<code>` (sucesso) ou `/login.html#sso_error=<motivo>`.
- `POST /api/auth/sso/exchange` (público) body `{code}` → `{ok, token, next}` (o antigo
  `/api/auth/saml/exchange` continua funcionando).
- `POST /api/session/verify` → principal atual. `POST /api/session/logout` → revoga o token.

### Tickets (substitui as chamadas diretas do supabase-js / REST)

Linhas no formato cru da tabela `tickets`
(`id, ticket_number, ticket_date, tech, client, product, vehicle, issue, resolution, status, serial, notes, created_at`).

- `GET /api/ticket-records?limit=1000&offset=0` → `{ok, rows}` ordenado por `ticket_number` desc. Permissão `tickets`.
- `POST /api/ticket-records` body com as colunas (sem `id`) → `{ok, row}`. Permissão `create_tickets`.
- `PATCH /api/ticket-records/{id}` body `{<coluna>: valor}` (colunas permitidas: todas menos `id`, `created_at`) → `{ok, row}`. Permissão `tickets`.
- `GET /api/ticket-records/history?field=client|product&pattern=<ILIKE>&limit=1000&offset=0` →
  `{ok, rows}` ordenado por `ticket_date desc nulls last, ticket_number desc`. Permissão `tickets`.
- `GET /api/ticket-records/count?since=YYYY-MM-DD&before=YYYY-MM-DD` (ambos opcionais) → `{ok, count}`. Permissão `dashboard` ou `tickets`.

### Presença em ticket (substitui Supabase Realtime)

Em memória no processo (o deploy roda 1 worker). Polling sugerido: 10 s.

- `POST /api/presence/tickets/{ticket_number}` body `{client_id, name}` → `{ok, viewers:[{client_id, name, since}], events:[...]}` (heartbeat; expira em 30 s sem heartbeat).
- `DELETE /api/presence/tickets/{ticket_number}?client_id=...` → `{ok}`.
- `POST /api/presence/tickets/{ticket_number}/events` body `{client_id, type, payload}` → `{ok}`; eventos ficam 60 s e são devolvidos no heartbeat de outros clientes (não para quem enviou).
