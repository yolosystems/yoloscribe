# Tool: aws-support

Provides access to AWS Support tools for creating and managing support cases
programmatically via the AWS Support API.

## Use cases

Use this tool when the user wants to:
- Create a new AWS support case
- Add communications or attachments to an existing case
- Check the status of open support cases
- Resolve a support case
- Look up available support services and severity levels

## Authentication

This tool uses AWS credentials. Requires an AWS account with a Business, Enterprise On-Ramp,
or Enterprise support plan. The deployment must have IAM permissions for `support:*`.
