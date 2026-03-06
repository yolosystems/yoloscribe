# Skill: google-workspace

You have access to Google Workspace Toosl, provided by the workspace MCP server. These tools provide access to the following Google for Work services:

- Gmail
- Drive
- Docs
- Sheets
- Slides
- Calendar
- Chat

As well as your Google profile and some utility tools.

## When to use this skill

Use these tools when the user defines an agent that might need to:
- Search for content in emails, or componse a new email and send it.
- Search for calendar events, schedule calendar events, or respond to a calendar event.
- Retrieve slide presentations and content from specific slides.
- Find folders in Google drive, or create new folders.
- Search for docs, retrieve the text from docs, and update the text of a doc.

## Authentication

This skill uses OAuth. Users must authenticate via the Credentials panel before
agents using this skill can run.

## Usage guidelines

- Favor searching by text rather than internal IDs used by these tools. This means that most operations will require first listing or searching by text, and then calling the tool. Don't prompt the user for ID's as they won't know them.
- Look for attachments on calendar invites and emails - these often contain information that the user might be asking about.
- The most common operations are likely going to be searching and retrieving content such as emails, calendar events, docs, spreadsheets and slides.
