# FLOWBREAK architecture

```text
Mobile browser
  └── React + TypeScript (Vercel)
        └── HTTPS REST API
              └── FastAPI (Render Docker)
                    ├── SQLite run history
                    ├── Playwright Chromium worker
                    │     ├── Desktop viewport
                    │     ├── Mobile viewport
                    │     ├── JS + HTTP observation
                    │     ├── Link checks
                    │     └── PNG screenshots
                    └── OpenAI API (optional, backend secret)
```

## Execution lifecycle

1. Validate target URL and create a queued run record.
2. FastAPI schedules an in-process background task.
3. Playwright visits each requested viewport and records browser observations.
4. Desktop page anchors are checked with bounded HTTP requests.
5. Screenshots and structured evidence are persisted.
6. OpenAI receives the observed failure summary and returns advisory suggestions.
7. Final report is stored in SQLite and the frontend polls for updates.

## Trust boundaries

Browser observations are primary evidence. AI output is advisory and stored separately. Demo records are marked `is_demo=true`. URL validation is baseline SSRF protection, not a substitute for infrastructure egress controls.
