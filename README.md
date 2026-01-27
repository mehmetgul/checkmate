# Checkmate (Backend)

AI-powered QA testing agent that executes tests using natural language commands.

## Features

- **LangGraph AI Agent:** Classifies, plans, and executes tests intelligently
- **Natural Language Testing:** Ask "Is login working?" and the agent will test it
- **Test Case Generation:** Generate comprehensive test suites from prompts
- **Project Management:** Manage multiple projects with separate test suites
- **Real Browser Automation:** Uses Playwright via playwright-http service

## Quick Start

### Prerequisites

- Python 3.11+
- uv (Python package manager)

### 1. Backend

```bash
cp .env.example .env
# Add your OPENAI_API_KEY and ENCRYPTION_KEY to .env

uv sync
uv run uvicorn api.main:app --port 8000 --reload
```

### 2. playwright-http (Browser Automation)

```bash
# In a separate terminal
cd /path/to/playwright-http
uv sync
uv run playwright install chromium chrome
uv run uvicorn executor.main:app --port 8932
```

### 3. Frontend (Separate Repo)

The frontend is in a separate repository: [checkmate-ui](https://github.com/ksankaran/checkmate-ui)

```bash
cd /path/to/checkmate-ui
npm install
npm run dev
```

Open http://localhost:3000 in your browser.

## Architecture

```
Frontend (3000) → Backend (8000) → playwright-http (8932) → Browser
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/POST | `/api/projects/` | List/Create projects |
| GET/PUT/DELETE | `/api/projects/{id}` | Project CRUD |
| GET/POST | `/api/test-cases/project/{id}/` | Test cases for project |
| POST | `/api/test-cases/{id}/runs/stream` | Execute test case (SSE) |
| POST | `/api/agent/projects/{id}/chat` | Chat with LangGraph agent |
| POST | `/api/agent/projects/{id}/build` | Build test case with AI |

## License

MIT
