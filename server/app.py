"""
FastAPI application for Agent Runner (Optional HTTP Server).

This server provides HTTP endpoints as an alternative trigger mechanism.
The primary path is GitHub workflow_dispatch via CLI.

Usage:
    # With uvicorn
    uvicorn server.app:app --host 0.0.0.0 --port 8000
    
    # With the CLI
    agent-runner-server
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)


def create_app():
    """
    Create FastAPI application with Agent Runner endpoints.
    
    Environment variables required:
        BOT_TOKEN: GitHub PAT with repo scope
        RUNNER_REPO: Repository containing the workflow
        BOT_USERNAME: GitHub username of the bot account
        
    Environment variables optional:
        WEBHOOK_SECRET: Secret for webhook signature verification
        ALLOW_INSECURE_WEBHOOKS: Set to "1" to allow unsigned webhooks
    """
    try:
        from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
        from pydantic import BaseModel
    except ImportError:
        logger.error("FastAPI not installed. Run: pip install 'agent-runner[server]'")
        return None
    
    from agent_runner.core import AgentRunner
    
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
        logger.info("Agent Runner shutting down...")
        await runner.close()
    
    app = FastAPI(
        title="Agent Runner API",
        description="AI-powered code modification runner using OpenHands SDK. "
                    "NOTE: This HTTP server is optional. The primary interface is "
                    "GitHub workflow_dispatch via the CLI.",
        version="1.0.0",
        lifespan=lifespan,
    )
    
    # Request/Response models
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
    async def submit_job(request: SubmitJobRequest, background_tasks: BackgroundTasks):
        """
        Submit a new agent runner job.
        
        This will:
        1. Create a fork of the upstream repo (or reuse existing)
        2. Wait for the fork to be ready
        3. Trigger the GitHub Actions workflow
        """
        try:
            job = await runner.submit_job(
                upstream_repo=request.upstream_repo,
                prompt=request.prompt,
                callback_url=request.callback_url,
            )
            logger.info(f"Job submitted: job_id={job.job_id}, upstream={job.upstream_repo}")
            
            # Start background polling as a fallback
            background_tasks.add_task(runner.poll_workflow_status, job.job_id)
            
            return job.to_dict()
        except HTTPException:
            raise
        except ValueError as e:
            logger.warning(f"Invalid job request: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except Exception:
            logger.exception("Unexpected error while submitting job")
            raise HTTPException(status_code=500, detail="Internal server error")
    
    @app.get("/api/jobs")
    async def list_jobs(limit: int = 100):
        """List recent jobs."""
        jobs = runner.storage.list_jobs(limit=limit)
        return [job.to_dict() for job in jobs]

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        """Get job status."""
        job = runner.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job.to_dict()
    
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
            logger.warning("Webhook signature verification failed")
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


# Create app instance for uvicorn
app = create_app()


def run_server():
    """Run the server using uvicorn."""
    try:
        import uvicorn
    except ImportError:
        logger.error("uvicorn not installed. Run: pip install 'agent-runner[server]'")
        sys.exit(1)
    
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    
    uvicorn.run(
        "server.app:app",
        host=host,
        port=port,
        reload=os.environ.get("RELOAD", "").lower() in ("1", "true"),
    )


if __name__ == "__main__":
    run_server()
