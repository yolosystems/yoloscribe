# Tool: dynamodb

Provides access to Amazon DynamoDB data modelling and development tools. Helps design
table schemas, validate data models, generate CDK resources, and estimate performance
and costs.

## Use cases

Use this tool when the user wants to:
- Design or review a DynamoDB data model and access patterns
- Validate an existing DynamoDB schema against best practices
- Generate CDK constructs or CloudFormation resources for a DynamoDB table
- Generate a data access layer for a DynamoDB table
- Estimate read/write capacity and costs for a given workload
- Migrate a relational or other schema to DynamoDB

## Authentication

This tool uses AWS credentials for cost and capacity estimation. Data modelling
and CDK generation are performed locally without AWS API calls.
