"""Default content strings for newly created YoloScribe pages."""

DEFAULT_WELCOME_MD = """\
# Welcome to your YoloScribe site!

This is the home page of your personal wiki. Edit this content using the editor,
or ask the AI assistant in the Chat panel to help you write and organise your notes.

## Getting Started

- Click **Edit** to enter edit mode
- Use the **Chat** panel to ask the AI to help you write content
- Navigate to sub-pages by clicking links
"""


def default_child_page_md(title: str) -> str:
    return (
        f"# {title}\n\n"
        f"This is a new wiki page. Edit this content using the editor,\n"
        f"or ask the AI assistant in the Chat panel to help you write and organise your notes.\n\n"
        f"## Getting Started\n\n"
        f"- Click **Edit** to enter edit mode\n"
        f"- Use the **Chat** panel to ask the AI to help you write content\n"
        f"- Navigate to sub-pages by clicking links\n"
    )
