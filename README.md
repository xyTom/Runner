# Agent-Runner

> 🤖 AI-powered code modification runner using OpenHands SDK

## Overview

Agent-Runner is a GitHub Actions-based automation tool that:
1. Clones a forked repository
2. Runs an AI agent (powered by OpenHands SDK) to make code changes
3. Commits and pushes the changes
4. Creates a Pull Request to the upstream repository
5. **Notifies your backend via webhook when complete**

## Architecture

```
┌─────────────┐     1. Submit Job     ┌──────────────────┐
│             │ ───────────────────▶  │                  │
│  Your App   │                       │  Backend Service │
│             │ ◀───────────────────  │  (agent_runner)  │
└─────────────┘     5. Webhook        └──────────────────┘
                    Callback                  │
                                              │ 2. Fork Repo
                                              │ 3. Trigger Workflow
                                              ▼
                                    ┌──────────────────┐
                                    │  GitHub Actions  │
                                    │  (run.yml)       │
                                    │                  │
                                    │  • Clone fork    │
                                    │  • Run AI Agent  │
                                    │  • Commit/Push   │
                                    │  • Create PR     │
                                    │  • Send Callback │
                                    └──────────────────┘
```

## Quick Start

### 1. Repository Secrets (Required)

Add these secrets to your Agent-Runner repository:

| Secret | Description |
|--------|-------------|
| `BOT_TOKEN` | GitHub PAT with `repo` scope |
| `LLM_API_KEY` | API key for your LLM provider |
| `LLM_MODEL` | (Optional) Model name, defaults to `anthropic/claude-sonnet-4-5-20250929` |
| `WEBHOOK_SECRET` | Secret for signing/validating webhook callbacks (recommended; required unless `ALLOW_INSECURE_WEBHOOKS=1`) |

### 2. Trigger via GitHub API (Primary Method)

The primary way to use Agent-Runner is through GitHub's `workflow_dispatch` API:

```bash
curl -X POST \
  -H "Authorization: Bearer <BOT_TOKEN>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<your-org>/Agent-Runner/actions/workflows/run.yml/dispatches \
  -d '{
    "ref": "main",
    "inputs": {
      "fork_repo": "bot/repo",
      "upstream_repo": "owner/repo",
      "prompt": "Fix the typo in README.md",
      "job_id": "job-123",
      "callback_url": "https://your-backend.com/webhook"
    }
  }'
```

### 3. (Optional) Deploy HTTP Server

If you prefer an HTTP API interface:

```bash
# Install OpenHands SDK (requires Python 3.12+)
pip install -e '.[openhands]'

# Install with server extras
pip install -e '.[server]'

# Set environment variables
export BOT_TOKEN="ghp_xxx"
export RUNNER_REPO="your-org/Agent-Runner"
export BOT_USERNAME="your-bot-username"
export WEBHOOK_SECRET="your-secret-key"

# Run server
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

### 4. Submit a Job

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "upstream_repo": "owner/repo",
    "prompt": "Fix the typo in README.md",
    "callback_url": "https://your-app.com/webhook/agent-runner"
  }'
```

Response:
```json
{
  "job_id": "job-abc123def456",
  "status": "triggered",
  "upstream_repo": "owner/repo",
  "fork_repo": "your-bot/repo",
  "branch": "bot/job-abc123def456"
}
```

## Workflow Inputs

| Input | Description | Required |
|-------|-------------|----------|
| `fork_repo` | Fork repository path, e.g. `bot/repo` | ✅ |
| `upstream_repo` | Upstream repository path, e.g. `owner/repo` | ✅ |
| `prompt` | Instructions for the AI agent | ✅ |
| `job_id` | Unique identifier for tracking | ✅ |
| `callback_url` | URL to POST results when complete | ❌ |

## Webhook Callback

When the workflow completes, it sends a POST request to your `callback_url`:

### Success Payload
```json
{
  "job_id": "job-abc123def456",
  "status": "completed",
  "pr_url": "https://github.com/owner/repo/pull/123",
  "upstream_repo": "owner/repo",
  "fork_repo": "bot/repo",
  "branch": "bot/job-abc123def456"
}
```
`pr_url` may be `null` if no PR was created (e.g., no changes).

### Failure Payload
```json
{
  "job_id": "job-abc123def456",
  "status": "failed",
  "error": "Workflow failed. Check GitHub Actions logs for details.",
  "upstream_repo": "owner/repo",
  "fork_repo": "bot/repo"
}
```

### Signature Verification

If `WEBHOOK_SECRET` is configured, the callback includes an `X-Signature-256` header (custom header used by Agent-Runner callbacks):

```
X-Signature-256: sha256=<HMAC-SHA256 of payload>
```

Verify in your backend:
```python
import hmac
import hashlib

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

## Python Backend Usage Examples

### Basic Usage

```python
import asyncio
from agent_runner import AgentRunner

async def main():
    # Initialize the runner
    runner = AgentRunner(
        bot_token="ghp_xxxxxxxxxxxx",           # GitHub PAT with repo scope
        runner_repo="your-org/Agent-Runner",    # This repository
        bot_username="your-bot-username",       # GitHub bot account username
        webhook_secret="your-secret-key",       # Required to verify callback signatures (recommended)
    )

    # Submit a job
    job = await runner.submit_job(
        upstream_repo="vercel/next.js",
        prompt="Fix the typo in README.md where 'teh' should be 'the'",
        callback_url="https://your-backend.com/webhook/agent-runner",
    )

    print(f"Job ID: {job.job_id}")
    print(f"Status: {job.status.value}")
    print(f"Fork: {job.fork_repo}")
    print(f"Branch: {job.branch}")

asyncio.run(main())
```

### Integration with FastAPI

```python
import os
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from agent_runner import AgentRunner, JobStatus

app = FastAPI()

# Initialize runner (do this once at startup)
runner = AgentRunner(
    bot_token=os.environ["BOT_TOKEN"],
    runner_repo=os.environ["RUNNER_REPO"],
    bot_username=os.environ["BOT_USERNAME"],
    webhook_secret=os.environ.get("WEBHOOK_SECRET"),
    allow_insecure_webhooks=os.environ.get("ALLOW_INSECURE_WEBHOOKS") == "1",
)

@app.on_event("shutdown")
async def shutdown():
    await runner.close()

class SubmitRequest(BaseModel):
    upstream_repo: str
    prompt: str
    callback_url: str | None = None

@app.post("/api/jobs")
async def submit_job(request: SubmitRequest):
    """Submit a new agent runner job."""
    try:
        job = await runner.submit_job(
            upstream_repo=request.upstream_repo,
            prompt=request.prompt,
            callback_url=request.callback_url,
        )
        return job.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Get job status by ID."""
    job = runner.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()

@app.post("/webhook/agent-runner")
async def handle_callback(request: Request):
    """Handle workflow completion callback."""
    # Verify signature
    signature = request.headers.get("X-Signature-256", "")
    body = await request.body()
    
    if not runner.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    data = await request.json()
    job = runner.update_job_from_callback(
        job_id=data["job_id"],
        status=data["status"],
        pr_url=data.get("pr_url"),
        error=data.get("error"),
    )
    
    # Do something with the completed job
    if job and job.status == JobStatus.COMPLETED:
        print(f"🎉 PR created: {job.pr_url}")
    
    return {"status": "ok"}
```

### Integration with Flask

```python
import os
from flask import Flask, request, jsonify
import asyncio
from agent_runner import AgentRunner

app = Flask(__name__)

runner = AgentRunner(
    bot_token=os.environ["BOT_TOKEN"],
    runner_repo=os.environ["RUNNER_REPO"],
    bot_username=os.environ["BOT_USERNAME"],
    webhook_secret=os.environ.get("WEBHOOK_SECRET"),
    allow_insecure_webhooks=os.environ.get("ALLOW_INSECURE_WEBHOOKS") == "1",
)

@app.route("/api/jobs", methods=["POST"])
def submit_job():
    data = request.json
    
    # Run async code in sync context
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        job = loop.run_until_complete(
            runner.submit_job(
                upstream_repo=data["upstream_repo"],
                prompt=data["prompt"],
                callback_url=data.get("callback_url"),
            )
        )
        return jsonify(job.to_dict()), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        loop.close()

@app.route("/webhook/agent-runner", methods=["POST"])
def handle_callback():
    signature = request.headers.get("X-Signature-256", "")
    body = request.get_data()
    if not runner.verify_webhook_signature(body, signature):
        return jsonify({"error": "Invalid signature"}), 401

    data = request.get_json() or {}
    job = runner.update_job_from_callback(
        job_id=data["job_id"],
        status=data["status"],
        pr_url=data.get("pr_url"),
        error=data.get("error"),
    )
    return jsonify({"status": "ok"})
```

### Custom Configuration

```python
runner = AgentRunner(
    bot_token="ghp_xxx",
    runner_repo="your-org/Agent-Runner",
    bot_username="your-bot",
    
    # Advanced options
    webhook_secret="your-hmac-secret",     # For callback signature verification
    allow_insecure_webhooks=False,         # Set True only for local dev without webhook signatures
    fork_timeout=180,                       # Max seconds to wait for fork (default: 120)
    fork_poll_interval=3,                   # Seconds between fork status checks (default: 5)
)
```

### Handling Job Status

```python
from agent_runner import JobStatus

job = runner.get_job("job-abc123")

if job.status == JobStatus.PENDING:
    print("Job is waiting to start")
elif job.status == JobStatus.FORKING:
    print("Creating fork...")
elif job.status == JobStatus.TRIGGERED:
    print("Workflow triggered, waiting for completion")
elif job.status == JobStatus.COMPLETED:
    print(f"Done! PR: {job.pr_url}")
elif job.status == JobStatus.FAILED:
    print(f"Failed: {job.error}")
```

## Backend API Reference

### POST /api/jobs

Submit a new agent runner job.

**Request Body:**
```json
{
  "upstream_repo": "owner/repo",
  "prompt": "Your instructions here",
  "callback_url": "https://your-webhook.com/callback"
}
```

**Response (201):**
```json
{
  "job_id": "job-xxx",
  "status": "triggered",
  ...
}
```

### GET /api/jobs/{job_id}

Get job status.

**Response (200):**
```json
{
  "job_id": "job-xxx",
  "status": "completed",
  "pr_url": "https://github.com/...",
  ...
}
```

### POST /webhook/agent-runner

Internal endpoint for workflow callbacks.

## Direct API Trigger

You can also trigger the workflow directly without the backend:

```bash
curl -X POST \
  -H "Authorization: Bearer <BOT_TOKEN>" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<your-org>/Agent-Runner/actions/workflows/run.yml/dispatches \
  -d '{
    "ref": "main",
    "inputs": {
      "fork_repo": "bot/repo",
      "upstream_repo": "owner/repo",
      "prompt": "Fix the typo",
      "job_id": "job-123",
      "callback_url": "https://your-backend.com/webhook"
    }
  }'
```

## LLM Configuration

### Option 1: Direct Provider (Anthropic/OpenAI)

```bash
export LLM_API_KEY="your-anthropic-or-openai-key"
export LLM_MODEL="anthropic/claude-sonnet-4-5-20250929"
```

### Option 2: OpenHands Cloud (Recommended)

Sign up at [OpenHands Cloud](https://app.all-hands.dev) for verified models.

```bash
export LLM_API_KEY="your-openhands-api-key"
export LLM_MODEL="openhands/claude-sonnet-4-5-20250929"
```

## File Structure

```
Agent-Runner/
├── .github/
│   └── workflows/
│       └── run.yml              # GitHub Actions workflow (thin, calls Python CLI)
├── src/
│   └── agent_runner/
│       ├── __init__.py          # Package exports
│       ├── cli.py               # CLI entry point (submit, run, pr, callback)
│       ├── core.py              # Core AgentRunner service
│       ├── models.py            # Data models (Job, JobStatus)
│       ├── callback.py          # Webhook callback handling
│       └── github/
│           ├── client.py        # GitHub API client
│           ├── repo.py          # Repository operations (fork, sync)
│           ├── pr.py            # Pull request operations
│           └── workflow.py      # Workflow dispatch
├── scripts/
│   ├── sync_fork.py             # Sync fork with upstream
│   └── commit_push.py           # Commit and push changes
├── server/
│   └── app.py                   # Optional FastAPI HTTP server
├── pyproject.toml               # Python package configuration
├── requirements.txt             # Core dependencies
├── LICENSE
└── README.md
```

## Security Best Practices

- 🔒 Never commit API keys - use GitHub Secrets
- 🔒 Use minimal PAT permissions (only `repo` scope needed)
- 🔒 Configure `WEBHOOK_SECRET` for callback signature verification
- 🔒 Validate and sanitize prompts before passing to the agent
- 🔒 Consider rate limiting on your backend

## Troubleshooting

### Fork Creation Timeout

If fork creation times out, increase `fork_timeout` in AgentRunner config:

```python
runner = AgentRunner(
    ...,
    fork_timeout=180,  # 3 minutes
)
```

### Workflow Not Triggering

1. Check that `BOT_TOKEN` has `repo` scope
2. Verify the workflow file exists at `.github/workflows/run.yml`
3. Check GitHub Actions is enabled for the repository

### Agent Errors

Check the GitHub Actions logs for detailed error messages. Common issues:
- Invalid `LLM_API_KEY`
- Rate limiting from LLM provider
- Repository too large for agent to process
