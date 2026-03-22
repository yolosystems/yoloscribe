## Wiki Architecture

YoloScribe is a fully agentic wiki that lives entirely in S3. All the content for the wiki, including the definitions of agents, the skills they can use, and the page content itself is stored as markdown files in S3. The frontend is a single page S3 website that renders the markdown files in the S3 bucket. The S3 structure is as follows:

server_name [This is the DNS name that maps to the root of the S3 bucket. It often takse this form: yoloscribe-dev.s3-website-us-west-2.amazonaws.com but it can be aliased to different DNS names.]
    /site_name [The page is a prefix at the top level S3 bucket and represents a site.]
        index.html [This is the single page website that serves up the content.md file for the site]
        /assets [This is where the assets for the single page website live]
        content.md [This is the content for the site. This gets rendered by the frontend single page web app]
        /page_name [Pages are hierarchical can support any number of child pages and nested child pages. The single page website at the site level must be able to serve up and edit markdown files in child pages]
            content.md
        /.agents [This defines the agents that update the page]
            /.agents/agent_name/agent.md [This defines what the agent does and what skills it has access to]
    /.skills [These are where the skills available to page-level agents live. These are only defined at the site level]
        /skill_name/skill.md [this is the skill definition file]
        /skill_name/mcp.json [these are the MCP servers that define the tools the skill has access to]

The frontend single page web makes calls to a FastAPI backend running in an EKS cluster for the functionality required by the ChatBot in the frontend.

Also, the frontend should be able to edit both content.md and agent.md files directly, using a browser-based markdown editor.

## Agent Design

This document describes how all agents in this project should be implemented. Use the strands-mcp server to retrieve documentation about the strands-agents framework.

### Base Architecture

1. All base agent classes should inherit from the strands-agent class.
2. All base agent classes should have a SYSTEM_PROMPT class variable that is a multi-line string.
3. The system prompt needs to support string templates so that information can be injected into the prompt at run time.
4. Agents can call other agents. To do this, the other agents are exposed as tools using the strands-agents tools concept. For example, if agent A needs to call agent B, agent B should be exposed to agent A as a tool.
5. The default model(s) to be used by the agents are Anthropic's Claude models. Always default to the latest model, but the model should be something that can be set at runtime.

### Specific Agent Implementations

These are the agents that are implemented in the yoloscribe project.

#### ChatAgent

This agent is the main interface into the yoloscribe application. It handles all inbound user queries and determines where to route the request. These requests will be handled by other agents. The ChatAgent must support the following user requests:

1. Update the wiki content - route to the ContentWriterAgent
2. Define a new agent.md file - route to the CreatorAgent
3. Create a new child page from the current page - route to the PageCreatorAgent
4. Invoke an agent defined in an agent.md file - route to the RunnerAgent

#### ContentWriterAgent

This agent is responsible for updating the content.md file for a given page, based on the query the user has entered into the ChatAgent. This agent must be able to do the following:

1. Retrieve the contents of the content.md file from the page location in the S3 bucket
2. Update the content.md file
3. Save the content.md file back to the page location in the S3 bucket

### CreatorAgent

This agent needs to take a query from the ChatAgent that indicates the user wants to create a new agent for updating a page and create the agent. It does this by doing the following:

1. Determining what the agent should do. It should ask follow-up questions where appropriate.
2. Determine what skills the agent should use to perform its task. It should determine which skills the agent should use based on the description of what the agent should do, and it should also ask the user what skills the agent should invoke in cases where it's not clear what skills should be used. It should also list available skills upon request, as well as provide details on how they can be used.

### PageCreatorAgent

This agent creates a new page or child page. It needs to know the name of the page to create, and it validates that the name conforms to the rules for S3 prefix/folder naming.

It does this by doing the following:

1. It creates the prefix at the right location in the S3 bucket. If this is a top level page, the prefix is created at the top level of the S3 bucket. If it is a child page, the prefix is created underneath the current page.
2. It creates a basic content.md file.
3. It creates the .agents subdirectory

#### RunnerAgent

The runner agent is one of the most important agents. This is the agent that will queue up an agent defined in a page's agend.md file to run asynchronously in its own sandboxed environment. This agent must do the following:

1. It will queue up an SQS task that includes the following in the message payload:
    + The S3 bucket name
    + The path to the page's content.md file
    + The path to the page's agent.md file that defines the agent that is being queued up for invocation.
    + The prompt that the user supplied to the ChatAgent indicating that they want to run an agent.
