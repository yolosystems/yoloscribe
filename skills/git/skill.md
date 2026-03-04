# Skill: github

You have access to GitHub tools provided by the GitHub Copilot MCP server.

## When to use this skill

Use these tools when the user wants to:
- View, create, or update GitHub issues and pull requests
- Read source files, READMEs, or documentation from a repository
- Search code or repositories
- Manage comments, labels, or milestones
- Base wiki content on the contents of a codebase

## Authentication

This skill uses OAuth. Users must authenticate via the Credentials panel before
agents using this skill can run.

## Usage guidelines

- Parse GitHub URLs into owner and repo: `https://github.com/{owner}/{repo}`
- To explore an unknown repo, list the root directory first, then drill into
  files of interest.
- Prefer reading `README.md` first for an overview before reading source files.
- Do not read unnecessarily large files (binaries, generated files, lock files).
- Search before creating — avoid duplicating existing issues or PRs.
