# Tool: billing-cost-management

Provides access to AWS billing and cost management tools spanning Cost Explorer,
Compute Optimizer, Cost Anomaly Detection, Budgets, Savings Plans, Reserved Instances,
and more.

## Use cases

Use this tool when the user wants to:
- Analyse and visualise AWS spend across services, accounts, or time periods
- Detect cost anomalies or unexpected billing spikes
- Review budget status and alerts
- Evaluate Reserved Instance or Savings Plan performance
- Get rightsizing recommendations from Compute Optimizer
- Query Cost and Usage Reports via Athena
- Compare costs across billing periods or accounts

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions for
`ce:*`, `budgets:*`, `compute-optimizer:*`, `cur:*`, and related cost management services.
