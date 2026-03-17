# Tool: iam

Provides access to AWS IAM tools for managing users, groups, roles, and policies.
Allows agents to inspect and modify identity and access configurations.

## Use cases

Use this tool when the user wants to:
- Create, list, or manage IAM users, groups, or roles
- Attach or detach managed policies
- Create or review inline policies
- Manage access keys for IAM users
- Audit permissions attached to a user, group, or role

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions for
`iam:*` operations appropriate to the tasks being performed. Use with care —
IAM changes affect access control across the entire AWS account.
