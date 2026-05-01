from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import httpx
import typer
from dotenv import load_dotenv
from jira import JIRA
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)


class Config:
    __slots__ = [
        "jira_url",
        "jira_user",
        "jira_token",
        "anthropic_api_key",
        "jira_pattern",
        "llm_prompt",
    ]

    def __init__(self):
        self.jira_url = os.getenv("JIRA_URL")
        self.jira_user = os.getenv("JIRA_USER")
        self.jira_token = os.getenv("JIRA_TOKEN")
        self.jira_pattern = os.getenv("JIRA_PATTERN")
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.llm_prompt = (
            os.getenv("LLM_PROMPT")
            or "You are a helpful assistant that summarizes PRs based on their content and associated JIRA tickets"
        )

        if not all([self.jira_url, self.jira_user, self.jira_token]):
            raise ValueError(
                "Please set JIRA_URL, JIRA_USER, and JIRA_TOKEN environment variables"
            )

    @classmethod
    def resolve(cls, file_path=".env") -> Config:
        load_dotenv(file_path, override=True)

        return cls()


config: Config | None = None
JIRA_TICKET_PATTERN: re.Pattern | None = None


def ask_llm(pr_info: PRInfo, lang="Polish") -> str:
    apikey = config.anthropic_api_key

    if not apikey:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    client = httpx.Client(base_url="https://api.anthropic.com/v1")
    prinfo_json = json.dumps(pr_info.__dict__, indent=2, default=str)

    response = client.post(
        "/messages",
        headers={
            "x-api-key": apikey,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "system": config.llm_prompt % lang,
            "messages": [
                {
                    "role": "user",
                    "content": f"PR and JIRA info:\n```json\n{prinfo_json}\n```",
                }
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    return data["content"][0]["text"].strip()


def connect_jira():
    return JIRA(
        server=config.jira_url,
        basic_auth=(config.jira_user, config.jira_token),
    )


@dataclass
class PRInfo:
    pr_number: int
    pr_title: str
    pr_date: str
    pr_body: str
    pr_url: str | None
    jira_ticket_id: str | None
    jira_url: str | None
    jira_description: str | None
    jira_summary: str | None
    llm_summary: str | None = None

    def set_llm_summary(self, summary: str):
        self.llm_summary = summary


def read_prs_from_file(file_path="prs.json") -> list:
    with open(file_path, "r") as f:
        return json.load(f)


def _extract_ticket_id(text: str) -> str | None:
    match = JIRA_TICKET_PATTERN.search(text)
    return match.group(0) if match else None


def _fetch_issue_details(
    jira: JIRA, ticket_id: str
) -> tuple[str, str, str] | tuple[None, None, None]:
    try:
        issue = jira.issue(ticket_id)
        url = f"{config.jira_url}/browse/{ticket_id}"
        description = issue.fields.description
        summary = issue.fields.summary
        return url, description, summary
    except Exception as e:
        print(f"Failed to fetch {ticket_id}: {e}")
        return None, None, None


def build_pr_info(jira: JIRA, pr: dict) -> PRInfo:
    ticket_id = _extract_ticket_id(pr.get("title", ""))
    jira_url, jira_description, jira_summary = None, None, None

    if ticket_id:
        jira_url, jira_description, jira_summary = _fetch_issue_details(jira, ticket_id)
    else:
        print(f"No ticket found in PR #{pr['number']}: {pr['title']}")

    return PRInfo(
        pr_number=pr["number"],
        pr_title=pr["title"],
        pr_date=pr["mergedAt"],
        pr_body=pr.get("body", ""),
        pr_url=pr.get("url"),
        jira_ticket_id=ticket_id,
        jira_url=jira_url,
        jira_description=jira_description,
        jira_summary=jira_summary,
    )


def collect_prs(
    start_gte="2026-04-01T00:00:00Z",
    end_lte="2026-04-30T23:59:59Z",
    author: str = "@me",
    repo: str = "myorg/myrepo",
):
    """Collect PRs merged in a specific date range and save to prs.json.

    Note: This function relies on the GitHub CLI (`gh`) being installed and authenticated.
    Args:
        start_gte: Start date (inclusive) in ISO format (e.g., "2026-04-01T00:00:00Z").
        end_lte: End 2026-04-29T23:59:59Z").
        author: GitHub username or "@me" for the authenticated user.
        repo: Repository in "owner/repo" format or SSH URL
    """
    repo_prefix = "git@github.com:"
    repo_ssh = f"{repo_prefix}{repo}.git"
    command = [
        "gh",
        "pr",
        "list",
        "--state",
        "merged",
        "--author",
        author,
        "--limit",
        "1000",
        "--repo",
        repo_ssh,
        "--json",
        "mergedAt,title,number,url,body",
        "--jq",
        f'[.[] | select(.mergedAt >= "{start_gte}" and .mergedAt <= "{end_lte}") | {{number, title, url, body, mergedAt}}]',
    ]

    print(f"Running command: {' '.join(command)}")

    process = subprocess.Popen(
        command,  # pass list directly, no shell=True needed
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    stdout, stderr = process.communicate()
    print("STDERR:", stderr)

    if process.returncode != 0:
        raise RuntimeError(f"Command failed: {stderr}")

    res = json.loads(stdout)
    if len(res) == 0:
        print("No PRs found in the specified date range.")
    else:
        print(f"Collected {len(res)} PRs")
        filepath = f"prs{repo.replace('/', '_')}_{start_gte[:10]}_{end_lte[:10]}.json"
        with open(filepath, "w") as f:
            json.dump(res, f, indent=2)
        return filepath


def enrich_pr_info(pr_info: PRInfo, lang: str) -> PRInfo:
    summary = ask_llm(pr_info, lang=lang)
    pr_info.set_llm_summary(summary)
    return pr_info


def build_md_report(pr_infos: list[PRInfo], output_file="report.md"):
    with open(output_file, "w") as f:
        for pr_info in pr_infos:
            f.write(f"# PR #{pr_info.pr_number}: {pr_info.pr_title}\n\n")
            f.write(f"- **PR Date**: {pr_info.pr_date}\n")
            if pr_info.jira_ticket_id:
                f.write(f"- **JIRA Ticket ID**:\n{pr_info.jira_ticket_id}\n")
                f.write(f"- **JIRA URL**:\n{pr_info.jira_url}\n")
                f.write(f"- **JIRA Summary**:\n{pr_info.jira_summary}\n")

            if pr_info.llm_summary:
                summary = pr_info.llm_summary.replace("\n", "  \n  ")
                f.write(f"- **LLM Summary**:\n{summary}\n\n")
            f.write(f"- **PR URL**:\n{pr_info.pr_url}\n")
            f.write("---\n\n")


cli = typer.Typer()


@cli.command("collect")
def main(
    repository: str = "myorg/myrepo",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    author: str = "@me",
    ai_summary_lang: str = "Polish",
    ai_summary: bool = True,
    env_file: str = ".env",
):
    if not os.path.exists(env_file):
        raise FileNotFoundError(
            f"Environment file '{env_file}' not found. Please create it with the necessary variables."
        )
    global config, JIRA_TICKET_PATTERN
    config = Config.resolve(file_path=env_file)
    JIRA_TICKET_PATTERN = re.compile(config.jira_pattern or r"PROJECT-\d+")

    if not start_date and not end_date:
        start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(month=start.month + 1, day=1) - timedelta(seconds=1)
        start_date = start.isoformat() + "Z"
        end_date = end.isoformat() + "Z"
        print(f"Using default date range: {start_date} to {end_date}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.fields[status]}"),
    ) as progress:
        output_file = f"report_{repository.replace('/', '_')}_{start_date}_{end_date}"
        filepath = collect_prs(
            author=author, repo=repository, start_gte=start_date, end_lte=end_date
        )
        if not filepath:
            print("No PRs collected, exiting.")
            return
        jira = connect_jira()
        prs_data = read_prs_from_file(filepath)

        overall = progress.add_task(
            "Overall Progress", total=len(prs_data), status="Preparing pr infos..."
        )
        infos = []
        for pr in prs_data:
            pr_task = progress.add_task(
                f"Processing PR #{pr['number']}", status="In progress..."
            )
            progress.advance(pr_task, advance=0)  # Start task
            progress.update(pr_task, status="Building PR info...")
            info = build_pr_info(jira, pr)
            infos.append(info)
            progress.advance(pr_task, advance=40)  # Update status before LLM call
            if ai_summary:
                progress.update(pr_task, status="Enriching with LLM...")
                enrich_pr_info(info, lang=ai_summary_lang)
            else:
                progress.update(pr_task, status="Skipping LLM enrichment...")
            progress.advance(pr_task, advance=100)
            progress.update(pr_task, status="Done")
            progress.advance(overall)

        progress.update(overall, status="Building markdown report...")
        build_md_report(infos, output_file=output_file + ".md")
        progress.update(overall, status="All done!")


if __name__ == "__main__":
    cli()
