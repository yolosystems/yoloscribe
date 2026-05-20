---
name: notifications
description: Send outbound notifications to the user's configured webhook URLs (Discord, Slack, Teams, etc.)
tools:
  - put_notification
---

You have access to the `put_notification` tool, which sends a message to all outbound webhook URLs the user has configured in their YoloScribe Credentials panel.

## Use cases

- A long-running task or research job has completed
- An error or anomaly was detected that needs human attention
- Important content has changed and the user should be informed
- A scheduled agent has finished its work

## Guidelines

- Keep messages concise — one or two sentences
- Include the page name or task context so the user knows what triggered the notification
- Only send for significant events — not routine reads, minor edits, or intermediate steps
- Do not send duplicate notifications for the same event

## Example

```
put_notification("Research complete: 'Q1 Report' updated with 5 new market data sources.")
```
