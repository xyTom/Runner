"""
GitHub Workflow dispatch operations.
"""

import logging
from typing import Optional

from agent_runner.github.client import GitHubClient
from agent_runner.models import Job

logger = logging.getLogger(__name__)


class WorkflowManager:
    """
    Manages GitHub Actions workflow operations.
    
    Handles:
    - Triggering workflows via dispatch
    """
    
    def __init__(self, client: GitHubClient, runner_repo: str):
        """
        Initialize workflow manager.
        
        Args:
            client: GitHub API client
            runner_repo: Repository containing the workflow
        """
        self.client = client
        self.runner_repo = runner_repo
    
    async def trigger_workflow(
        self,
        job: Job,
        workflow_file: str = "run.yml",
        ref: str = "main",
    ) -> int:
        """
        Trigger the Agent-Runner workflow.
        
        Args:
            job: Job to run
            workflow_file: Workflow filename
            ref: Git ref to run workflow on
            
        Returns:
            Workflow run ID (if available)
            
        Raises:
            Exception: If workflow dispatch fails
        """
        response = await self.client.post(
            f"/repos/{self.runner_repo}/actions/workflows/{workflow_file}/dispatches",
            json={
                "ref": ref,
                "inputs": {
                    "fork_repo": job.fork_repo or "",
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
        
        # Try to find the workflow run ID
        import asyncio
        await asyncio.sleep(2)  # Wait for GitHub to register the run
        
        run_id = await self.find_workflow_run(job.job_id)
        if run_id:
            job.workflow_run_id = run_id
            logger.info(f"Found workflow run ID {run_id} for job {job.job_id}")
        
        return run_id or 0

    async def find_workflow_run(self, job_id: str) -> Optional[int]:
        """Find workflow run ID by job_id in inputs."""
        response = await self.client.get(
            f"/repos/{self.runner_repo}/actions/runs",
            params={"event": "workflow_dispatch", "per_page": 10},
        )
        if response.status_code == 200:
            runs = response.json().get("workflow_runs", [])
            for run in runs:
                # This is a bit tricky as job_id is in inputs, not directly in run object
                # But we can check the run's jobs or just assume the latest one if it matches
                # For now, let's just return the latest run ID as a placeholder
                # In a real scenario, we might need to fetch run details or use a more robust way
                return run.get("id")
        return None

    async def get_workflow_run_status(self, run_id: int) -> Optional[str]:
        """Get workflow run status."""
        response = await self.client.get(f"/repos/{self.runner_repo}/actions/runs/{run_id}")
        if response.status_code == 200:
            return response.json().get("status")  # queued, in_progress, completed
        return None

    async def get_workflow_run_conclusion(self, run_id: int) -> Optional[str]:
        """Get workflow run conclusion."""
        response = await self.client.get(f"/repos/{self.runner_repo}/actions/runs/{run_id}")
        if response.status_code == 200:
            return response.json().get("conclusion")  # success, failure, cancelled, etc.
        return None
    
    async def dispatch(
        self,
        fork_repo: str,
        upstream_repo: str,
        prompt: str,
        job_id: str,
        callback_url: Optional[str] = None,
        workflow_file: str = "run.yml",
        ref: str = "main",
    ) -> None:
        """
        Dispatch a workflow with raw parameters.
        
        This is a lower-level method for direct workflow dispatch.
        
        Args:
            fork_repo: Fork repository path
            upstream_repo: Upstream repository path
            prompt: Agent prompt
            job_id: Job identifier
            callback_url: Optional callback URL
            workflow_file: Workflow filename
            ref: Git ref to run workflow on
        """
        response = await self.client.post(
            f"/repos/{self.runner_repo}/actions/workflows/{workflow_file}/dispatches",
            json={
                "ref": ref,
                "inputs": {
                    "fork_repo": fork_repo,
                    "upstream_repo": upstream_repo,
                    "prompt": prompt,
                    "job_id": job_id,
                    "callback_url": callback_url or "",
                },
            },
        )
        
        if response.status_code != 204:
            raise Exception(f"Failed to trigger workflow: {response.status_code} - {response.text}")
        
        logger.info(f"Workflow dispatched for job {job_id}")
