"""
Core Agent Runner service.

This module provides the main AgentRunner class that orchestrates:
- Repository forking
- Workflow dispatch
- Job status tracking
"""

import logging
import re
import uuid
from typing import Optional
from urllib.parse import urlsplit

from agent_runner.callback import CallbackHandler
from agent_runner.github.client import GitHubClient
from agent_runner.github.pr import PRManager
from agent_runner.github.repo import RepoManager
from agent_runner.github.workflow import WorkflowManager
from agent_runner.models import Job, JobStatus
from agent_runner.storage import JobStorage

logger = logging.getLogger(__name__)


class AgentRunner:
    """
    Core service for managing Agent Runner jobs.
    
    Orchestrates:
    - Repository forking via RepoManager
    - Workflow dispatch via WorkflowManager
    - Callback handling via CallbackHandler
    - Job status tracking
    
    Example:
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
    
    def __init__(
        self,
        bot_token: str,
        runner_repo: str,
        bot_username: str,
        webhook_secret: Optional[str] = None,
        allow_insecure_webhooks: bool = False,
        fork_timeout: int = 120,
        fork_poll_interval: int = 5,
        db_path: str = "jobs.db",
    ):
        """
        Initialize the Agent Runner service.
        
        Args:
            bot_token: GitHub PAT with repo scope
            runner_repo: Repository containing the workflow (e.g., "your-org/Agent-Runner")
            bot_username: GitHub username of the bot account
            webhook_secret: Secret for signing webhook payloads (optional but recommended)
            allow_insecure_webhooks: If True, accept unsigned webhook callbacks
            fork_timeout: Maximum seconds to wait for fork to be ready
            fork_poll_interval: Seconds between fork status checks
            db_path: Path to SQLite database for job storage
        """
        self.runner_repo = runner_repo
        self.bot_username = bot_username
        
        # Initialize components
        self.client = GitHubClient(bot_token)
        self.repo_manager = RepoManager(
            client=self.client,
            bot_username=bot_username,
            fork_timeout=fork_timeout,
            fork_poll_interval=fork_poll_interval,
        )
        self.pr_manager = PRManager(client=self.client)
        self.workflow_manager = WorkflowManager(
            client=self.client,
            runner_repo=runner_repo,
        )
        self.callback_handler = CallbackHandler(
            webhook_secret=webhook_secret,
            allow_insecure=allow_insecure_webhooks,
        )
        
        # Persistent job storage
        self.storage = JobStorage(db_path)
    
    async def close(self) -> None:
        """Close HTTP client. Call this when shutting down."""
        await self.client.close()
    
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
            
        Raises:
            ValueError: If input validation fails
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
        self.storage.save_job(job)
        
        try:
            # Step 1: Create or get fork
            job.update_status(JobStatus.FORKING)
            self.storage.save_job(job)
            
            fork_repo = await self.repo_manager.create_or_get_fork(upstream_repo)
            job.fork_repo = fork_repo
            job.branch = f"bot/{job_id}"
            job.update_status(JobStatus.FORK_READY)
            self.storage.save_job(job)
            
            # Step 2: Trigger workflow
            await self.workflow_manager.trigger_workflow(job)
            job.update_status(JobStatus.TRIGGERED)
            self.storage.save_job(job)
            
            return job
            
        except Exception as e:
            job.mark_failed(str(e))
            self.storage.save_job(job)
            raise
    
    def get_job(self, job_id: str) -> Optional[Job]:
        """Get job by ID."""
        return self.storage.get_job(job_id)
    
    def update_job_from_callback(
        self,
        job_id: str,
        status: str,
        pr_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Optional[Job]:
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
        job = self.storage.get_job(job_id)
        if not job:
            return None
        
        if status == "completed":
            job.mark_completed(pr_url)
        elif status == "failed":
            job.mark_failed(error or "Unknown error")
        
        self.storage.save_job(job)
        return job
    
    async def poll_workflow_status(self, job_id: str, interval: int = 30, timeout: int = 3600):
        """
        Poll for workflow completion if callback is not received.
        
        This is a fallback mechanism.
        """
        job = self.storage.get_job(job_id)
        if not job or not job.workflow_run_id:
            return

        start_time = time.time()
        while time.time() - start_time < timeout:
            job = self.storage.get_job(job_id)
            if not job or job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                break
            
            status = await self.workflow_manager.get_workflow_run_status(job.workflow_run_id)
            if status == "completed":
                conclusion = await self.workflow_manager.get_workflow_run_conclusion(job.workflow_run_id)
                if conclusion == "success":
                    # We still need the PR URL, which is usually sent via callback.
                    # If we don't have it, we might need to find it.
                    pr_url = None
                    if not job.pr_url:
                        try:
                            fork_owner = job.fork_repo.split("/")[0]
                            head = f"{fork_owner}:{job.branch}"
                            pr_url = await self.pr_manager.find_existing_pr(job.upstream_repo, head)
                        except Exception:
                            pass
                    self.update_job_from_callback(job_id, "completed", pr_url=pr_url)
                else:
                    self.update_job_from_callback(job_id, "failed", error=f"Workflow finished with conclusion: {conclusion}")
                break
            
            await asyncio.sleep(interval)

    def verify_webhook_signature(self, payload: bytes, signature: str) -> bool:
        """
        Verify webhook signature using HMAC-SHA256.
        
        Args:
            payload: Raw request body
            signature: X-Signature-256 header value
            
        Returns:
            True if signature is valid
        """
        return self.callback_handler.verify_signature(payload, signature)
