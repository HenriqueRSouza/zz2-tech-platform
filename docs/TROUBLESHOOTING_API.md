# Troubleshooting sessions API

A **tree** is a kit's diagnostic procedure (edited in the Kit Editor). A
**session** is one technician walking that tree for one customer: the branch
taken, the steps carried out, and whether it fixed the problem.

The tool records sessions automatically: *Apply to ticket* creates one
(`pending`); a second Apply marks the previous one `abandoned`; creating/saving
the ticket links it and closes it from the ticket status (Resolved → `resolved`,
RMA/Distributor → `escalated`, No solution/Destroy → `unresolved`).

## Storage

`ZZ2_TROUBLESHOOTING_STORE` = `auto` (default) | `supabase` | `local`.

* **Supabase** — table `"techSupport".troubleshooting_sessions`. Run
  [`sql/troubleshooting_sessions.sql`](../sql/troubleshooting_sessions.sql) once in
  the SQL editor. Written only by the backend with the service key; RLS on, no
  browser access.
* **Local** — `DATA_DIR/store/troubleshooting_sessions.json`, capped at 20,000
  sessions (oldest dropped). For development and installations without Supabase.

## Endpoints

All require `Authorization: Bearer <token>`.

| Method | Path | Permission |
|---|---|---|
| `POST` | `/api/troubleshooting/sessions` | `create_tickets` |
| `GET` | `/api/troubleshooting/sessions` | `tickets` |
| `GET` | `/api/troubleshooting/sessions/{id}` | `tickets` |
| `PATCH` | `/api/troubleshooting/sessions/{id}` | `create_tickets`, and the recording technician or `kit_editor` |
| `GET` | `/api/troubleshooting/stats` | `analytics` |

### Record

```http
POST /api/troubleshooting/sessions
{
  "kit_key": "ITZ-NTG3",
  "path": ["No audio", "Check DIP switches", "DIP 3 ON"],
  "checked_steps": ["DIP 3 ON"],
  "outcome": "pending",            // pending|resolved|unresolved|escalated|abandoned
  "ticket_number": "7001",         // optional; stored as TKT-7001
  "notes": "",                     // optional, ≤ 4000 chars
  "started_at": "2026-11-16T10:00:00Z"   // optional; gives duration_seconds
}
→ 201 { "ok": true, "session": { "id": "…", "technician": "ana@…", … } }
```

Rules: the kit must exist (404 otherwise); `path` needs 1–60 steps of ≤ 300
characters; the technician is always the caller — sending `technician` is a 422.

### List

`GET /api/troubleshooting/sessions?kit=&technician=&ticket=&outcome=&since=&limit=50&offset=0`

Newest first. `has_more` says whether another page exists. `ticket` accepts
`7001` or `TKT-7001`; `since` is an ISO timestamp.

### Amend

```http
PATCH /api/troubleshooting/sessions/{id}
{ "outcome": "resolved", "ticket_number": "7001", "notes": "…" }
```

A final outcome is final: changing it again is `409`.

### Statistics

`GET /api/troubleshooting/stats?kit=ITZ-NTG3` (omit `kit` for every kit):

```json
{ "ok": true, "kits": [{
  "kit_key": "ITZ-NTG3", "total": 5,
  "outcomes": {"pending": 1, "resolved": 3, "unresolved": 1, "escalated": 0, "abandoned": 0},
  "resolution_rate": 0.75,
  "top_resolving_paths": [{"path": "No audio > Check DIP switches > DIP 3 ON", "count": 3}],
  "top_failing_paths":   [{"path": "No audio > Check wiring > Replace harness", "count": 1}]
}]}
```

`resolution_rate` = resolved ÷ (resolved + unresolved + escalated); `pending` and
`abandoned` are excluded. `null` when there are none yet.
