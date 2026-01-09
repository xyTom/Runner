#!/usr/bin/env python3
"""
CLI entry point for Agent Runner.

This module provides a command-line interface that can be called from:
- GitHub Actions workflow_dispatch
- Local development/testing
- Any CI/CD system

Commands:
    submit  - Submit a new job (fork, trigger workflow)
    run     - Run the agent directly (used by workflow)
    pr      - Create a pull request
    callback - Send callback notification
"""

import argparse
import asyncio
import logging
import os
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_env_or_fail(name: str) -> str:
    """Get environment variable or exit with error."""
    value = os.environ.get(name)
    if not value:
        logger.error(f"Required environment variable {name} not set")
        sys.exit(1)
    return value


async def cmd_submit(args: argparse.Namespace) -> int:
    """Submit a new agent runner job."""
    from agent_runner.core import AgentRunner
    
    runner = AgentRunner(
        bot_token=get_env_or_fail("BOT_TOKEN"),
        runner_repo=get_env_or_fail("RUNNER_REPO"),
        bot_username=get_env_or_fail("BOT_USERNAME"),
        webhook_secret=os.environ.get("WEBHOOK_SECRET"),
    )
    
    try:
        job = await runner.submit_job(
            upstream_repo=args.upstream_repo,
            prompt=args.prompt,
            callback_url=args.callback_url,
        )
        
        print(f"Job submitted successfully!")
        print(f"  Job ID: {job.job_id}")
        print(f"  Fork: {job.fork_repo}")
        print(f"  Branch: {job.branch}")
        print(f"  Status: {job.status.value}")
        
        return 0
    except Exception as e:
        logger.error(f"Failed to submit job: {e}")
        return 1
    finally:
        await runner.close()


async def cmd_run_agent(args: argparse.Namespace) -> int:
    """Run the OpenHands agent on a repository."""
    try:
        from openhands.sdk import LLM, Agent, Conversation, Tool
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.task_tracker import TaskTrackerTool
        from openhands.tools.terminal import TerminalTool
    except ImportError:
        logger.error(
            "OpenHands SDK not installed (requires Python 3.12+). "
            "Run: pip install -e '.[openhands]'"
        )
        return 1
    
    prompt = args.prompt or os.environ.get("AGENT_PROMPT")
    if not prompt:
        logger.error("No prompt provided (use --prompt or AGENT_PROMPT env var)")
        return 1
    
    workspace = args.workspace or os.getcwd()
    
    logger.info(f"Running agent in workspace: {workspace}")
    logger.info(f"Prompt: {prompt[:100]}...")
    
    # Initialize LLM
    llm = LLM(
        model=os.environ.get("LLM_MODEL") or "anthropic/claude-sonnet-4-5-20250929",
        api_key=os.environ.get("LLM_API_KEY"),
        base_url=os.environ.get("LLM_BASE_URL"),
    )
    
    # Create agent with tools
    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
    )
    
    # Create conversation and run
    conversation = Conversation(agent=agent, workspace=workspace)
    conversation.send_message(prompt)
    conversation.run()
    
    logger.info("Agent completed successfully!")
    return 0


async def cmd_create_pr(args: argparse.Namespace) -> int:
    """Create a pull request."""
    from agent_runner.github.client import GitHubClient
    from agent_runner.github.pr import PRManager
    
    bot_token = get_env_or_fail("BOT_TOKEN")
    
    client = GitHubClient(bot_token)
    pr_manager = PRManager(client)
    
    try:
        title = args.title or f"Bot: {args.job_id}"
        body = args.body or f"Automated changes via Agent Runner.\n\nJob: {args.job_id}"
        
        pr_url = await pr_manager.create_pr(
            upstream_repo=args.upstream_repo,
            fork_repo=args.fork_repo,
            branch=args.branch,
            title=title,
            body=body,
        )
        
        print(f"PR_URL={pr_url}")
        
        # Set GitHub output if running in Actions
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"pr_url={pr_url}\n")
        
        return 0
    except Exception as e:
        logger.error(f"Failed to create PR: {e}")
        return 1
    finally:
        await client.close()


async def cmd_callback(args: argparse.Namespace) -> int:
    """Send callback notification."""
    from agent_runner.callback import CallbackHandler
    
    if not args.callback_url:
        logger.info("No callback URL provided, skipping")
        return 0
    
    handler = CallbackHandler(
        webhook_secret=os.environ.get("WEBHOOK_SECRET"),
    )
    
    try:
        if args.status == "completed":
            success = await handler.send_success_callback(
                callback_url=args.callback_url,
                job_id=args.job_id,
                pr_url=args.pr_url,
                upstream_repo=args.upstream_repo,
                fork_repo=args.fork_repo,
                branch=args.branch or "",
            )
        else:
            success = await handler.send_failure_callback(
                callback_url=args.callback_url,
                job_id=args.job_id,
                error=args.error or "Workflow failed",
                upstream_repo=args.upstream_repo,
                fork_repo=args.fork_repo,
            )
        
        if success:
            logger.info("Callback sent successfully!")
            return 0
        else:
            logger.warning("Callback may have failed")
            return 1
            
    except Exception as e:
        logger.error(f"Failed to send callback: {e}")
        return 1


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser."""
    parser = argparse.ArgumentParser(
        prog="agent-runner",
        description="AI-powered agent runner using OpenHands SDK",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Submit command
    submit_parser = subparsers.add_parser(
        "submit",
        help="Submit a new agent runner job",
    )
    submit_parser.add_argument(
        "--upstream-repo",
        required=True,
        help="Upstream repository (owner/repo)",
    )
    submit_parser.add_argument(
        "--prompt",
        required=True,
        help="Instructions for the AI agent",
    )
    submit_parser.add_argument(
        "--callback-url",
        help="URL to POST results when job completes",
    )
    
    # Run agent command
    run_parser = subparsers.add_parser(
        "run",
        help="Run the OpenHands agent",
    )
    run_parser.add_argument(
        "--prompt",
        help="Instructions for the agent (or use AGENT_PROMPT env var)",
    )
    run_parser.add_argument(
        "--workspace",
        help="Workspace directory (default: current directory)",
    )
    
    # Create PR command
    pr_parser = subparsers.add_parser(
        "pr",
        help="Create a pull request",
    )
    pr_parser.add_argument(
        "--upstream-repo",
        required=True,
        help="Target repository (owner/repo)",
    )
    pr_parser.add_argument(
        "--fork-repo",
        required=True,
        help="Source fork repository",
    )
    pr_parser.add_argument(
        "--branch",
        required=True,
        help="Branch name",
    )
    pr_parser.add_argument(
        "--job-id",
        required=True,
        help="Job identifier",
    )
    pr_parser.add_argument(
        "--title",
        help="PR title",
    )
    pr_parser.add_argument(
        "--body",
        help="PR body/description",
    )
    
    # Callback command
    callback_parser = subparsers.add_parser(
        "callback",
        help="Send callback notification",
    )
    callback_parser.add_argument(
        "--callback-url",
        help="URL to POST notification to",
    )
    callback_parser.add_argument(
        "--job-id",
        required=True,
        help="Job identifier",
    )
    callback_parser.add_argument(
        "--status",
        required=True,
        choices=["completed", "failed"],
        help="Job status",
    )
    callback_parser.add_argument(
        "--upstream-repo",
        required=True,
        help="Upstream repository",
    )
    callback_parser.add_argument(
        "--fork-repo",
        help="Fork repository",
    )
    callback_parser.add_argument(
        "--branch",
        help="Branch name",
    )
    callback_parser.add_argument(
        "--pr-url",
        help="Pull request URL (for success)",
    )
    callback_parser.add_argument(
        "--error",
        help="Error message (for failure)",
    )
    
    return parser


async def async_main() -> int:
    """Async main entry point."""
    parser = create_parser()
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    commands = {
        "submit": cmd_submit,
        "run": cmd_run_agent,
        "pr": cmd_create_pr,
        "callback": cmd_callback,
    }
    
    handler = commands.get(args.command)
    if handler:
        return await handler(args)
    else:
        parser.print_help()
        return 1


def main() -> None:
    """Main entry point."""
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
