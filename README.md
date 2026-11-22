# FLOWBREAK

**Autonomous software reliability testing — browser evidence, not invented metrics.**

FLOWBREAK is a mobile-first reliability dashboard backed by Python FastAPI and real Playwright Chromium runs. It checks desktop/mobile page loads, HTTP failures, broken links and JavaScript errors; captures screenshots; and optionally asks OpenAI to explain observed failures.

> **MVP scope:** single-process background jobs and SQLite are suitable for a small deployment. For high concurrency, move execution to a durable queue (Redis/Celery), use managed PostgreSQL, and store screenshots in object storage. Do not expose this service publicly without authentication, quotas and abuse controls.

## Architecture

- **Frontend:** React + TypeScript + Vite. Deploy to Vercel.
- **API/runner:** FastAPI + Playwright Chromium. Deploy the Dockerfile to Render.
- **Persistence:** SQLite on Render persistent disk; JSON test evidence and PNG screenshots.
- **AI:** OpenAI API is optional. Browser observations remain available if the key is absent or AI fails.
- **Demo:** `POST /api/demo` inserts clearly marked sample data, never a real browser execution.

## Features

- Desktop (1440×900) and mobile (390×844) Chromium emulation.
- Main document navigation status and response status monitoring.
- JavaScript uncaught exceptions and console errors.
- Anchor link HTTP validation, bounded by `MAX_LINKS`.
- Failure classification, screenshots, persisted reports and AI recommendations.
- Live dashboard polling; metrics are calculated from persisted test runs.
- SSRF baseline guard rejects non-public target hosts and non-HTTP schemes.

## Cloud deployment from a phone

### Render backend

1. Create a Web Service from this repository in Render.
2. Select Docker; set Dockerfile path to `backend/Dockerfile` and Docker context to `backend` (or use the included Blueprint).
3. Add a persistent disk mounted at `/var/data`.
4. Set `CORS_ORIGINS` to your exact Vercel origin.
5. Add `OPENAI_API_KEY` as a Render secret if AI analysis is wanted. Never put it in frontend variables.
6. Deploy and confirm `https://YOUR-RENDER-SERVICE.onrender.com/health` returns status ok.

### Vercel frontend

1. Import this repository in Vercel.
2. Set **Root Directory** to `frontend`.
3. Set `VITE_API_URL` to the Render service origin (no trailing slash).
4. Deploy. The included `frontend/vercel.json` supports SPA routing.

## Run locally

Backend (Python 3.11+): `cd backend && pip install -r requirements.txt && playwright install chromium && uvicorn app.main:app --reload`

Frontend: `cd frontend && npm install && npm run dev`

Set `VITE_API_URL=http://localhost:8000` for local frontend development. Configure backend variables from `.env.example`. Never commit secrets.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Service health |
| POST | `/api/runs` | Start a browser test; returns 202 and run ID |
| GET | `/api/runs` | List recent runs |
| GET | `/api/runs/{id}` | Fetch a report |
| GET | `/api/runs/{id}/artifacts/{filename}` | Fetch a captured PNG |
| POST | `/api/demo` | Create explicitly labelled demo data |

Example request:

```json
{"url":"https://example.com","desktop":true,"mobile":true,"check_links":true,"capture_screenshot":true}
```

## Security and limitations

- Keep `OPENAI_API_KEY` on the backend only.
- Only test websites you own or have permission to assess.
- Browser navigation, redirects, subresources, downloads and link checks use a local validating HTTP proxy. The proxy resolves each requested hostname, rejects the connection if any DNS answer is non-public, and opens the upstream socket to a vetted IP literal (connection pinning), preventing Chromium from independently resolving the target after validation. Redirects create new proxy connections and are checked again. Browser requests are configured not to bypass the proxy for loopback destinations. This is application-level connection-pinned egress enforcement, not an OS/container firewall: other backend code and any process that bypasses the proxy are not restricted. For public multi-tenant production deployments, additionally enforce outbound firewall/network-policy rules, authentication, quotas and abuse controls.
- Link checking is bounded. Sites may block bots or require authentication; an HTTP failure is an observed response, not definitive proof of a user-visible defect.
- Screenshots may contain personal data. Protect access and define retention.
- No user authentication, team isolation, scheduled monitoring, durable queue, video recording or cross-browser matrix yet. One Render instance processes jobs in-process.

## Tests

Backend: `cd backend && python -m playwright install --with-deps chromium && pytest -q`

Frontend: `cd frontend && npm install && npm test && npm run build`

## License

MIT — see [LICENSE](LICENSE).
