# Tool: cfn

Provides access to CloudFormation resource management via the Cloud Control API.
Allows agents to read, create, update, and delete any CloudFormation-supported resource
type using a unified interface.

## Use cases

Use this tool when the user wants to:
- List or describe resources of any CloudFormation resource type
- Create, update, or delete a resource using its CloudFormation schema
- Retrieve the schema and supported properties for a resource type
- Check the status of an in-progress resource operation
- Generate a CloudFormation template from existing resources

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions appropriate
to the resource types being managed, plus `cloudformation:*` and `cloudcontrol:*`.
