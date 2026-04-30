# ASAP

## Setup

Install dependencies:

```bash
uv sync
```

Create `.env` file with required variables:

```
JIRA_URL=https://your-jira-instance.com
JIRA_USER=your-email@example.com
JIRA_TOKEN=your-jira-api-token
ANTHROPIC_API_KEY=your-anthropic-api-key
JIRA_PATTERN=YOUR-TICKET-\d+  # optional: regex pattern for JIRA tickets
LLM_PROMPT=Your custom prompt  # optional: default provided
```

Authenticate with GitHub:

```bash
gh auth login
```

## Usage

Collect and summarize merged PRs:

```bash
uv run main.py collect --repository owner/repo --start-date "2026-04-01T00:00:00Z" --end-date "2026-04-30T23:59:59Z" --author "@me" --ai-summary-lang Polish
```

**Output**: Generates `report_*.md` file with PR summaries, JIRA details, and AI-generated insights.
