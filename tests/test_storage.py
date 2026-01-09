import os
import pytest
from agent_runner.models import Job, JobStatus
from agent_runner.storage import JobStorage

def test_save_and_get_job(tmp_path):
    db_path = str(tmp_path / "test_jobs.db")
    storage = JobStorage(db_path)
    
    job = Job(
        job_id="test-job-1",
        upstream_repo="owner/repo",
        prompt="test prompt",
    )
    
    storage.save_job(job)
    
    retrieved_job = storage.get_job("test-job-1")
    assert retrieved_job is not None
    assert retrieved_job.job_id == "test-job-1"
    assert retrieved_job.upstream_repo == "owner/repo"
    assert retrieved_job.status == JobStatus.PENDING

def test_update_job_status(tmp_path):
    db_path = str(tmp_path / "test_jobs.db")
    storage = JobStorage(db_path)
    
    job = Job(
        job_id="test-job-2",
        upstream_repo="owner/repo",
        prompt="test prompt",
    )
    storage.save_job(job)
    
    job.update_status(JobStatus.RUNNING)
    storage.save_job(job)
    
    retrieved_job = storage.get_job("test-job-2")
    assert retrieved_job.status == JobStatus.RUNNING

def test_list_jobs(tmp_path):
    db_path = str(tmp_path / "test_jobs.db")
    storage = JobStorage(db_path)
    
    for i in range(5):
        job = Job(
            job_id=f"job-{i}",
            upstream_repo="owner/repo",
            prompt=f"prompt {i}",
        )
        storage.save_job(job)
    
    jobs = storage.list_jobs()
    assert len(jobs) == 5
