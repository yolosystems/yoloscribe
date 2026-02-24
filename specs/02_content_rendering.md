### Content Rendering
+ AgentScribe is a wiki that runs in S3 as a single page website.
+ Each site in the S3 bucket has its own prefix. For example, if I want to create a new site called "engineering wiki" then this would be in the S3 bucket under a "engineering_wiki" prefix.
+ The content for each site is written in markdown in a file called "content.md"
+ The single page website should render the markdown.
+ Editing can occur two ways. The first is simply editing the markdown directly. The single page site should have an "edit" mode that brings up a rich markdown editor. The second way to edit the site is by a chat agent. In edit mode, the chat agent is displayed in a panel on the left, just like the Claude Code agent is in VSCode.
+ The chat agent accepts natural language instructions from the user and updates the content.md accordingly.
+ The single page website running in the S3 bucket connects to a FastAPI backend that will be running in an EKS cluster with a public ALB in front of it. The chat agent will call a /chat API in the backend.