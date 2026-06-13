# Подключение моб-приложения (ark_flutter_3) к бэкенду

Файл для **мобильного разработчика**. Бэкенд (`ark_yandex`) запущен на машине
бэкенд-дева. Значения ниже — актуальные и проверенные.

> ⚠️ IP может смениться (DHCP). Если перестало работать — бэкенд-дев заново
> смотрит свой IP: `ipconfig getifaddr en0` и обновляет адрес.

---

## 1. Подключить приложение

В приложении, на экране выбора сервера, вбей **РОВНО** этот адрес:

```
http://192.168.68.59:8000
```

Правила (из-за них чаще всего и «offline»):

- ❌ **без `/` на конце** — приложение клеит `<адрес>/healthcheck/`; если оставить
  слэш, получится `//healthcheck/` → **404 → backend offline**. Это была причина.
- ❌ без `/api`, без `/ru`, без `/healthcheck` — только `http://IP:порт`.
- ✅ `http://` — это нормально, **https не требуется**.
- ✅ оба Mac должны быть в **одной Wi-Fi** (телефон/Mac в подсети `192.168.68.x`).

После ввода правильного адреса «offline» уходит.

## 2. Проверка (если не подключается)

На своём Mac выполни:

```bash
curl http://192.168.68.59:8000/healthcheck/
```

- `{"status": "ok"}` → сеть и сервер в порядке; значит дело в адресе в
  приложении — убери слэш на конце (см. п.1).
- висит / `Connection refused` / `No route to host` → ты не в той сети или на
  роутере «изоляция клиентов»; подключись к той же Wi-Fi, что и бэкенд-дев.

## 3. Таймауты

Долгие запросы больше не рвутся на 30 сек (подняли до 120 на чтение). Чтобы это
подхватилось — **пересобери приложение** (`flutter run` заново), hot-reload не
применит изменение констант.

---

## (опционально) A2A-мост: твой Claude ↔ бэкенд-Claude

Это отдельная штука — НЕ для работы приложения, а чтобы **твой Claude Code мог
спрашивать у бэкенд-Claude** про API. Нужен только если бэкенд-дев запустил A2A-
сервер (порт **9999**, отдельно от приложения на 8000).

1. Один раз зарегистрируй мост (нужен Node/`npx`):
   ```bash
   claude mcp add a2a-bridge -e A2A_AGENT_URLS=http://192.168.68.59:9999 -- npx -y a2a-mcp-bridge
   ```
2. Скопируй секцию ниже в `ark_flutter_3/CLAUDE.md`.
3. Проверка в Claude: `call list_agents` → «ARK Yandex Backend Agent»; затем
   `send_message agent:<slug> message:"LIST"` → `No open tasks.`

### (копировать в ark_flutter_3/CLAUDE.md)

You are the **frontend** Claude for `ark_flutter_3`. The Django backend
(`ark_yandex`) runs on another machine with its own Claude Code, reachable via
the `a2a-bridge` MCP (`A2A_AGENT_URLS=http://192.168.68.59:9999`).

Tools: `list_agents` (once, to get the slug), `send_message` (agent=slug,
message=text). Call `send_message` whenever you need something the backend owns,
using one of these formats:

- `QUESTION: <specific question>` — grounded answer about the backend code.
  e.g. `QUESTION: What fields does GET /api/v1/auth/me/ return?`
- `BUG: <title> | <what happened> | <endpoint or file>`
- `FEATURE: <title> | <what is needed> | <expected response>`
- `LIST` — open tasks.

Rules: ask the backend agent **before** inventing an API shape; don't block on a
fix (mock + `// TODO(a2a #<id>)`); one issue per message; be concrete (method,
path, expected shape). `QUESTION` is sync; `BUG`/`FEATURE` return a task id.
