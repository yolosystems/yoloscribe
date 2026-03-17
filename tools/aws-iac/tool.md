# Tool: aws-iac

Provides access to AWS Infrastructure as Code tools covering CloudFormation and the
AWS CDK. Helps validate, troubleshoot, and understand IaC templates and constructs.

## Use cases

Use this tool when the user wants to:
- Validate a CloudFormation template for syntax or compliance issues
- Troubleshoot a failed or stuck CloudFormation deployment
- Search CloudFormation or CDK documentation
- Find CDK construct samples and best practices
- Read IaC documentation pages

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions for
CloudFormation read operations (`cloudformation:Describe*`, `cloudformation:Get*`,
`cloudformation:List*`, `cloudformation:ValidateTemplate`).
