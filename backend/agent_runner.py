"""
Agent Runner Backend Service

This module provides the core functionality for:
1. Automatically forking a repository
2. Waiting for the fork to be ready
3. Triggering the Agent-Runner workflow
4. Tracking job status

Usage:
    from agent_runner import AgentRunner
    
    runner = AgentRunner(
        bot_token="ghp_xxx",
        runner_repo="your-org/Agent-Runner",
        bot_username="agent-bot"
    )
    
    job = await runner.submit_job(
        upstream_repo="vercel/next.js",
        prompt="Fix the typo in README.md",
        callback_url="https://your-backend.com/webhook/agent-runner"
    )
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit

import httpx

import sqlite3


# Configure logging
logger = logging.getLogger(__name__)


class JobStatus(Enum):
    PENDING = "pending"           # Job created, not yet started
    FORKING = "forking"           # Creating fork
    FORK_READY = "fork_ready"     # Fork created and ready
    TRIGGERED = "triggered"       # Workflow triggered
    RUNNING = "running"           # Workflow running
    COMPLETED = "completed"       # Workflow completed successfully
    FAILED = "failed"             # Workflow failed
    CANCELLED = "cancelled"       # Job cancelled


@dataclass
class Job:
    """Represents a single agent runner job."""
    job_id: str
    upstream_repo: str
    prompt: str
    callback_url: Optional[str] = None
    status: JobStatus = JobStatus.PENDING
    fork_repo: Optional[str] = None
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    workflow_run_id: Optional[int] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "upstream_repo": self.upstream_repo,
            "prompt": self.prompt,
            "callback_url": self.callback_url,
            "status": self.status.value,
            "fork_repo": self.fork_repo,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "workflow_run_id": self.workflow_run_id,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Job':
        return cls(
            job_id=data["job_id"],
            upstream_repo=data["upstream_repo"],
            prompt=data["prompt"],
            callback_url=data.get("callback_url"),
            status=JobStatus(data["status"]),
            fork_repo=data.get("fork_repo"),
            branch=data.get("branch"),
            pr_url=data.get("pr_url"),
            workflow_run_id=data.get("workflow_run_id"),
            error=data.get("error"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )



class JobStorage:
    """SQLite storage for jobs."""
    def __init__(self, db_path: str = "jobs.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def save_job(self, job: Job):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, data, updated_at) VALUES (?, ?, ?)",
                (job.job_id, json.dumps(job.to_dict()), job.updated_at.isoformat())
            )

    def get_job(self, job_id: str) -> Optional[Job]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT data FROM jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if row:
                return Job.from_dict(json.loads(row[0]))
        return None

    def list_jobs(self, limit: int = 100) -> list[Job]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT data FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,))
            return [Job.from_dict(json.loads(row[0])) for row in cursor.fetchall()]

class AgentRunner:
    """
    Core service for managing Agent Runner jobs.
    
    Handles:
    - Repository forking
    - Workflow dispatch
    - Job status tracking
    """
    
    GITHUB_API = "https://api.github.com"
    
    def __init__(
        self,
        bot_token: str,
        runner_repo: str,
        bot_username: str,
        webhook_secret: Optional[str] = None,
        allow_insecure_webhooks: bool = False,
        fork_timeout: int = 120,
        fork_poll_interval: int = 5,
    ):
        """
        Initialize the Agent Runner service.
        
        Args:
            bot_token: GitHub PAT with repo scope
            runner_repo: Repository containing the workflow (e.g., "your-org/Agent-Runner")
            bot_username: GitHub username of the bot account
            webhook_secret: Secret for signing webhook payloads (optional but recommended)
            allow_insecure_webhooks: If True, accept unsigned webhook callbacks when webhook_secret is not configured
            fork_timeout: Maximum seconds to wait for fork to be ready
            fork_poll_interval: Seconds between fork status checks
        """
        self.bot_token = bot_token
        self.runner_repo = runner_repo
        self.bot_username = bot_username
        self.webhook_secret = webhook_secret
        self.allow_insecure_webhooks = allow_insecure_webhooks
        self.fork_timeout = fork_timeout
        self.fork_poll_interval = fork_poll_interval
        
        # Persistent job storage
        self._storage = JobStorage()
        
        # Reusable HTTP client
        self._client: Optional[httpx.AsyncClient] = None
        
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    
    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create reusable HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Perform HTTP request with retry logic."""
        client = await self._get_client()
        max_retries = 3
        base_delay = 1

        for attempt in range(max_retries):
            try:
                response = await client.request(method, url, **kwargs)
                
                # Handle rate limiting
                if response.status_code == 403 and "rate limit" in response.text.lower():
                    reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                    sleep_duration = max(reset_time - time.time(), 0) + 1
                    logger.warning(f"Rate limit hit, sleeping for {sleep_duration}s")
                    await asyncio.sleep(min(sleep_duration, 60)) # Cap sleep at 60s
                    continue

                # Retry on server errors
                if response.status_code >= 500:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Server error {response.status_code}, retrying in {delay}s...")
                    await asyncio.sleep(delay)
                    continue

                return response
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                delay = base_delay * (2 ** attempt)
                if attempt == max_retries - 1:
                    raise
                logger.warning(f"Request error {e}, retrying in {delay}s...")
                await asyncio.sleep(delay)
        
        return await client.request(method, url, **kwargs) # Final attempt
    
    async def close(self) -> None:
        """Close HTTP client. Call this when shutting down."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
    
    @staticmethod
    def _validate_repo_path(repo: str) -> bool:
        """Validate repository path format (owner/repo)."""
        pattern = r'^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$'
        return bool(re.match(pattern, repo))

    @staticmethod
    def _validate_callback_url(callback_url: str) -> bool:
        """Validate callback URL format (http/https)."""
        if not callback_url or any(ch.isspace() for ch in callback_url):
            return False
        try:
            parsed = urlsplit(callback_url)
        except Exception:
            return False
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    
    async def submit_job(
        self,
        upstream_repo: str,
        prompt: str,
        callback_url: Optional[str] = None,
    ) -> Job:
        """
        Submit a new agent runner job.
        
        This will:
        1. Create a fork of the upstream repo (or reuse existing)
        2. Wait for the fork to be ready
        3. Trigger the workflow
        
        Args:
            upstream_repo: Repository to fork (e.g., "vercel/next.js")
            prompt: Instructions for the AI agent
            callback_url: URL to POST results when job completes
            
        Returns:
            Job object with tracking information
        """
        # Validate input
        if not self._validate_repo_path(upstream_repo):
            raise ValueError(f"Invalid repository path: {upstream_repo}. Expected format: owner/repo")
        
        if not prompt or not prompt.strip():
            raise ValueError("Prompt cannot be empty")

        callback_url = callback_url.strip() if callback_url is not None else None
        if callback_url == "":
            callback_url = None
        if callback_url and not self._validate_callback_url(callback_url):
            raise ValueError(
                "Invalid callback_url. Expected an http(s) URL, e.g. https://your-app.com/webhook/agent-runner"
            )
        
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = Job(
            job_id=job_id,
            upstream_repo=upstream_repo,
            prompt=prompt,
            callback_url=callback_url,
        )
        self._storage.save_job(job)
        
        try:
            # Step 1: Create or get fork
            job.status = JobStatus.FORKING
            job.updated_at = datetime.now(timezone.utc)
            self._storage.save_job(job)
            
            fork_repo = await self._create_or_get_fork(upstream_repo)
            job.fork_repo = fork_repo
            job.branch = f"bot/{job_id}"
            job.status = JobStatus.FORK_READY
            job.updated_at = datetime.now(timezone.utc)
            self._storage.save_job(job)
            
            # Step 2: Trigger workflow
            await self._trigger_workflow(job)
            job.status = JobStatus.TRIGGERED
            job.updated_at = datetime.now(timezone.utc)
            self._storage.save_job(job)
            
            return job
            
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.updated_at = datetime.now(timezone.utc)
            self._storage.save_job(job)
            raise
    
    async def _create_or_get_fork(self, upstream_repo: str) -> str:
        """
        Create a fork or return existing fork.
        
        Args:
            upstream_repo: Repository to fork (e.g., "owner/repo")
            
        Returns:
            Fork repository path (e.g., "bot-username/repo")
        """
        client = await self._get_client()
        
        parts = upstream_repo.split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid upstream_repo format: {upstream_repo}")
        repo_name = parts[1]
        fork_repo = f"{self.bot_username}/{repo_name}"
        
        # Check if fork already exists
        response = await self._request_with_retry(
            "GET",
            f"{self.GITHUB_API}/repos/{fork_repo}",
            headers=self._headers,
        )
        
        if response.status_code == 200:
            fork_data = response.json()
            # Verify it's actually a fork of the upstream
            if fork_data.get("fork") and fork_data.get("parent", {}).get("full_name") == upstream_repo:
                logger.info(f"Using existing fork: {fork_repo}")
                # Sync fork with upstream
                await self._sync_fork(fork_repo, upstream_repo)
                return fork_repo
            else:
                # Repo exists but is not a fork of upstream - this is a naming conflict
                raise ValueError(
                    f"Repository {fork_repo} exists but is not a fork of {upstream_repo}. "
                    "Please rename or delete the conflicting repository."
                )
        
        # Create new fork
        logger.info(f"Creating fork of {upstream_repo}...")
        response = await self._request_with_retry(
            "POST",
            f"{self.GITHUB_API}/repos/{upstream_repo}/forks",
            headers=self._headers,
            json={"default_branch_only": True},
        )
            
        if response.status_code not in (202, 200):
            raise Exception(f"Failed to create fork: {response.status_code} - {response.text}")
        
        # Wait for fork to be ready
        await self._wait_for_fork(fork_repo)
        
        return fork_repo
    
    async def _wait_for_fork(self, fork_repo: str) -> None:
        """Wait for a fork to be ready (cloneable)."""
        start_time = time.time()
        
        while time.time() - start_time < self.fork_timeout:
            response = await self._request_with_retry(
                "GET",
                f"{self.GITHUB_API}/repos/{fork_repo}",
                headers=self._headers,
            )
            
            if response.status_code == 200:
                # Try to verify the repo is actually ready by checking branches
                branches_response = await self._request_with_retry(
                    "GET",
                    f"{self.GITHUB_API}/repos/{fork_repo}/branches",
                    headers=self._headers,
                )
                if branches_response.status_code == 200 and branches_response.json():
                    logger.info(f"Fork {fork_repo} is ready!")
                    return
            
            logger.debug(f"Waiting for fork {fork_repo} to be ready...")
            await asyncio.sleep(self.fork_poll_interval)
        
        raise TimeoutError(f"Fork {fork_repo} did not become ready within {self.fork_timeout} seconds")
    
    async def _sync_fork(self, fork_repo: str, upstream_repo: str) -> None:
        """Sync fork with upstream (fetch latest changes)."""
        # Get upstream default branch
        response = await self._request_with_retry(
            "GET",
            f"{self.GITHUB_API}/repos/{upstream_repo}",
            headers=self._headers,
        )
        if response.status_code != 200:
            logger.warning("Could not get upstream info for sync")
            return
            
        default_branch = response.json().get("default_branch", "main")
        
        # Sync fork using GitHub's merge-upstream API
        response = await self._request_with_retry(
            "POST",
            f"{self.GITHUB_API}/repos/{fork_repo}/merge-upstream",
            headers=self._headers,
            json={"branch": default_branch},
        )
        
        if response.status_code == 200:
            logger.info(f"Fork {fork_repo} synced with upstream")
        elif response.status_code == 409:
            logger.debug(f"Fork {fork_repo} already up to date")
        else:
            logger.warning(f"Could not sync fork: {response.status_code}")
    
    async def _trigger_workflow(self, job: Job) -> None:
        """Trigger the Agent-Runner workflow."""
        response = await self._request_with_retry(
            "POST",
            f"{self.GITHUB_API}/repos/{self.runner_repo}/actions/workflows/run.yml/dispatches",
            headers=self._headers,
            json={
                "ref": "main",
                "inputs": {
                    "fork_repo": job.fork_repo,
                    "upstream_repo": job.upstream_repo,
                    "prompt": job.prompt,
                    "job_id": job.job_id,
                    "callback_url": job.callback_url or "",
                },
            },
        )
        
        if response.status_code != 204:
            raise Exception(f"Failed to trigger workflow: {response.status_code} - {response.text}")
        
        logger.info(f"Workflow triggered for job {job.job_id}")
    
    def get_job(self, job_id: str) -> Optional[Job]:
        """Get job by ID."""
        return self._storage.get_job(job_id)
    

    def list_jobs(self, limit: int = 100) -> list[Job]:
        """List recent jobs."""
        return self._storage.list_jobs(limit)

    def update_job_from_callback(self, job_id: str, status: str, pr_url: Optional[str] = None, error: Optional[str] = None) -> Optional[Job]:
        """
        Update job status from workflow callback.
        
        Args:
            job_id: Job identifier
            status: New status (completed, failed)
            pr_url: Pull request URL if created
            error: Error message if failed
            
        Returns:
            Updated job or None if not found
        """
        job = self._storage.get_job(job_id)
        if not job:
            return None
        
        if status == "completed":
            job.status = JobStatus.COMPLETED
            job.pr_url = pr_url
        elif status == "failed":
            job.status = JobStatus.FAILED
            job.error = error
        
        job.updated_at = datetime.now(timezone.utc)
        self._storage.save_job(job)
        return job
    
    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verify webhook signature using HMAC-SHA256.
        
        Args:
            payload: Raw request body
            signature: X-Signature-256 header value
            
        Returns:
            True if signature is valid
        """
        if not self.webhook_secret:
            if self.allow_insecure_webhooks:
                logger.warning("No webhook_secret configured - signature verification skipped")
                return True
            logger.error("No webhook_secret configured - rejecting webhook because signature cannot be verified")
            return False
        
        expected = "sha256=" + hmac.new(
            self.webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        
        return hmac.compare_digest(expected, signature)


# Example usage with FastAPI
def create_fastapi_app():
    """Create a FastAPI application with Agent Runner endpoints."""
    try:
        from contextlib import asynccontextmanager
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel
    except ImportError:
        print("FastAPI not installed. Run: pip install fastapi uvicorn")
        return None
    
    import os
    
    # Validate required environment variables at startup
    required_vars = ["BOT_TOKEN", "RUNNER_REPO", "BOT_USERNAME"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    
    allow_insecure_webhooks = os.environ.get("ALLOW_INSECURE_WEBHOOKS", "").lower() in ("1", "true", "yes")
    webhook_secret = os.environ.get("WEBHOOK_SECRET")
    
    # Warn about insecure configuration
    if not webhook_secret and not allow_insecure_webhooks:
        logger.warning(
            "WEBHOOK_SECRET not set. Callbacks will be rejected unless ALLOW_INSECURE_WEBHOOKS=1. "
            "Set WEBHOOK_SECRET for production use."
        )
    elif not webhook_secret and allow_insecure_webhooks:
        logger.warning("Running in INSECURE mode: webhook signatures are not being verified!")
    
    runner = AgentRunner(
        bot_token=os.environ["BOT_TOKEN"],
        runner_repo=os.environ["RUNNER_REPO"],
        bot_username=os.environ["BOT_USERNAME"],
        webhook_secret=webhook_secret,
        allow_insecure_webhooks=allow_insecure_webhooks,
    )
    
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Manage application lifecycle."""
        logger.info(f"Agent Runner starting up (runner_repo={runner.runner_repo})")
        yield
        # Cleanup on shutdown
        logger.info("Agent Runner shutting down...")
        await runner.close()
    
    app = FastAPI(
        title="Agent Runner API",
        description="AI-powered code modification runner using OpenHands SDK",
        version="1.0.0",
        lifespan=lifespan,
    )
    
    class SubmitJobRequest(BaseModel):
        upstream_repo: str
        prompt: str
        callback_url: Optional[str] = None
    
    class CallbackPayload(BaseModel):
        job_id: str
        status: str  # completed, failed
        pr_url: Optional[str] = None
        error: Optional[str] = None
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint for load balancers and monitoring."""
        return {
            "status": "healthy",
            "service": "agent-runner",
            "runner_repo": runner.runner_repo,
        }
    
    @app.post("/api/jobs")
    async def submit_job(request: SubmitJobRequest):
        """Submit a new agent runner job."""
        try:
            job = await runner.submit_job(
                upstream_repo=request.upstream_repo,
                prompt=request.prompt,
                callback_url=request.callback_url,
            )
            logger.info(f"Job submitted: job_id={job.job_id}, upstream={job.upstream_repo}")
            return job.to_dict()
        except HTTPException:
            raise
        except ValueError as e:
            logger.warning(f"Invalid job request: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except Exception:
            logger.exception("Unexpected error while submitting job")
            raise HTTPException(status_code=500, detail="Internal server error")
    
    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        """Get job status."""
        job = runner.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()

    @app.get("/api/jobs")
    async def list_jobs(limit: int = 100):
        """List recent jobs."""
        jobs = runner.list_jobs(limit)
        return [job.to_dict() for job in jobs]
    
    @app.post("/webhook/agent-runner")
    async def workflow_callback(request: Request):
        """
        Callback endpoint for workflow completion notifications.
        
        The workflow calls this endpoint when it completes (success or failure).
        """
        # Verify signature if configured
        signature = request.headers.get("X-Signature-256", "")
        body = await request.body()
        
        if not runner.verify_webhook_signature(body, signature):
            logger.warning(f"Webhook signature verification failed")
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        try:
            payload = CallbackPayload(**json.loads(body))
        except Exception as e:
            logger.warning(f"Invalid callback payload: {e}")
            raise HTTPException(status_code=400, detail="Invalid payload format")
        
        job = runner.update_job_from_callback(
            job_id=payload.job_id,
            status=payload.status,
            pr_url=payload.pr_url,
            error=payload.error,
        )
        
        if not job:
            logger.warning(f"Callback received for unknown job: {payload.job_id}")
            raise HTTPException(status_code=404, detail="Job not found")
        
        logger.info(f"Job updated via callback: job_id={job.job_id}, status={job.status.value}")
        return {"status": "ok", "job": job.to_dict()}
    
    return app


if __name__ == "__main__":
    # Quick demo
    import os
    
    async def main():
        runner = AgentRunner(
            bot_token=os.environ["BOT_TOKEN"],
            runner_repo=os.environ["RUNNER_REPO"],
            bot_username=os.environ["BOT_USERNAME"],
        )
        
        job = await runner.submit_job(
            upstream_repo="example/repo",
            prompt="Fix the typo in README.md",
            callback_url="https://your-backend.com/webhook/agent-runner",
        )
        
        print(f"Job submitted: {job.to_dict()}")
    
    asyncio.run(main())
