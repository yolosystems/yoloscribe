# Tool: cloudwatch

Provides access to Amazon CloudWatch tools covering Logs, Metrics, and Alarms.
Allows agents to query log data, retrieve metrics, and inspect alarm states.

## Use cases

Use this tool when the user wants to:
- Run CloudWatch Logs Insights queries against a log group
- List available log groups or find logs for a specific service
- Retrieve CloudWatch metrics for a resource over a time period
- List active or recently triggered alarms
- Correlate logs and metrics during an incident investigation

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions for
`logs:*`, `cloudwatch:GetMetricData`, `cloudwatch:DescribeAlarms`, and related
CloudWatch read operations.
