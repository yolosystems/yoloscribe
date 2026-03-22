## MCP Server Deployment

This document describes the strategy for deploying remote-capable versions of open-source MCP servers that ship as servers designed to be run locally in agentic coding tools like Claude Code.

YoloScribe is a distfibuted web-based application running in the AWS cloud, and as such, creating new skills for agents to use that rely on local MCP servers is not convenient (or secure). As a result, we've had to convert several MCP servers to remote-capable servers.

### Google Workspace

This remote MCP server was converted from the open-source Google Gemini CLI skill that Google maintains. It is implemented as a typescript web application and deployed into an EKS cluster as a K8S service fronted by an ALB ingress.

### AWS MCP

These are approximately 50 or so MCP servers spanning a wide variety of AWS services. These are open souce and maintained by AWS. Most of these ship as FastMCP servers. They have been forked from the orignal repos in Github and refactored to support running as remote MCP servers through the mcp-remote-wrapper package combined with some minor changes to the server.py files in each MCP server package. 

These too will be deployed as K8S services running in EKS fronted by an ALB ingress.

### Modular EKS deployment via Helm

The goal is to support modular deployment of an arbitrary combination of these services through a single Helm chart. All deployed services should share the same ALB ingress. We want to keep the git repos for the Google Workspace MCP servers and the AWS MCP servers separate, but use the same set of Helm charts to deploy both.

We want to support a process something like the following:

```
cd ~Projects
mkdir mcp_repos
git clone [url to google workspace mcp]
git clone [url to cloned AWS mcp with remote server mode]
git clone [url to helm charts to deploy the servers]
cd helm-charts
#now edit values.yaml to enable/disable the services to deploy
```
The values.yaml should look something like the following, where every available MCP server has its own section and can be enabled or disabled within that section.

```
google:
    workspace:
        enabled: true
        image: #image url
        tag: #image tag
aws:
    iac:
        enabled: true
        image: #image url
        tag: #image tag
    eks:
        enabled: true
        ...
    frontend:
        enabled: false
    ...
ingress:
    #standard ALB ingress block
```

For each enabled service, the helm chart will intall the MCP as a K8S service. All enabled services will be exposed via the ALB ingress. The URL path structure should be something like the following:

ALB host: mcp.runyolo.dev
paths:
    path: /mcp/google-workspace
    path: /mcp/aws-eks
    path: /mcp/aws-iac
    ...



❯ Okay great. Now I need a way to deploy these, as well as the google-workspace MCP, which is located at: /Users/nslater/Projects/yolo/google-workspace-mcp. Review the spec at
/Users/nslater/Projects/yolo/agentscribe/specs/11_mcp_server_deployment.md and come up with a plan for being able to deploy these MCP servers from the same helm chart. Make
suggestions and ask questions where appropriate. Also, the google-workspace MCP is already deployed and you can use the Helm chart that was used to deploy that as a basis for how to
 create this unified MCP server deployment via Helm.


