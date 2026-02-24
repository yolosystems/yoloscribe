### Skills and Agents
+ Skills will be defined at the top level S3 bucket and they will apply to all sites underneath the bucket.
+ Each skill is defined in a markdown file called "skill.md"
+ The skill file structure should be based on the Claude Code skill definition file.
+ The skill file should be paired with a mcp.json file that defines a local MCP server that will expose the tools used by the skill.
+ The path structure used to store skills at the top level S3 bucket will be:
    + bucket_name/skills/[skill_name]/skill.md
    + bucket_name/skills/[skill_name]/mcp.json

+ Skills are used by agents.
+ Users create agents through the frontend chat interface. If a user says they want to create an agent, the chatbot should ask the user for a brief description of what the agent should do and what skills the agent should have. The only available skills are those from the top level skills directory in the S3 bucket. The chatbot should also ask for the name of the agent - this should be a brief single word name and conform to the rules governing S3 object and prefix names.
+ In the site directory, there will be a sub-directory called "agents". AFter the chatbot has captured all the information required to create an agent, it will call the FastAPI backend to create the agent.
+ The FastAPI backend creates the agent by writing a markdown file to the site "agents" directory. The path structure is as follows:
    + /site_name/agents/[agent_name]/agents.md where [agent_name] is the name of the agent.
+ The agents.md file contains the prompt for the agent given by the user and the list of skills the agent has.
+ After an agent file has been created, these should be editable in the frontend markdown editor simply by browsing to an agents.md file, in the same way that the frontend can also edit content.md files.

