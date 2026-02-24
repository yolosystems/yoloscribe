The tool signature is different — instead of a single repo_url, mcp-server-github takes owner, repo, and path as separate parameters:

  # Skill: git

  You have access to GitHub repository tools provided by mcp-server-github.

  ## When to use this skill

  Use these tools when the user wants to:
  - Import or reference content from a GitHub repository
  - Read source files, READMEs, or documentation from a repo
  - Base a wiki page on the contents of a codebase

  ## Tool: get_file_contents

  Retrieves the contents of a file or directory from a GitHub repository.

  Parameters:
  - `owner` (required): The GitHub username or organisation name
  - `repo` (required): The repository name
  - `path` (required): Path to the file or directory within the repo
  - `branch` (optional): Branch name. Defaults to the repo's default branch.

  Returns file contents as a string, or a directory listing if `path` is a
  directory.

  ## Usage guidelines

  - Parse GitHub URLs into parts: `https://github.com/{owner}/{repo}` →
    `owner` and `repo`. The path comes from whatever the user specifies.
  - To explore an unknown repo, call `get_file_contents` with `path` set to
    `/` or `""` to get a directory listing, then drill into files of interest.
  - Prefer reading `README.md` first for an overview before reading source files.
  - Do not read unnecessarily large files (e.g. binaries, generated files,
    lock files like `package-lock.json`).