# Tool: cloudtrail

Provides access to AWS CloudTrail tools for querying API activity and audit logs.
Supports both standard event lookup and CloudTrail Lake SQL queries.

## Use cases

Use this tool when the user wants to:
- Look up API calls made by a specific user, role, or service
- Investigate what actions were taken on a specific resource
- Trace the history of changes to an AWS resource
- Run SQL queries against CloudTrail Lake for advanced analysis
- Audit access patterns or investigate a security incident

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions for
`cloudtrail:LookupEvents`, `cloudtrail:GetQueryResults`, and related CloudTrail
read operations.
