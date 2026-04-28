# Tool: aws-pricing

Provides access to AWS pricing tools. Allows agents to retrieve and compare service
pricing across regions and analyse the estimated cost of CDK-defined infrastructure.

## Use cases

Use this tool when the user wants to:
- Look up the price of an AWS service or instance type
- Compare pricing across regions or instance families
- Estimate the cost of infrastructure defined in a CDK project
- Identify cheaper alternatives to a given configuration

## Authentication

This tool uses the public AWS Pricing API and requires no authentication for most
queries. CDK project analysis requires read access to the local CDK project files.
