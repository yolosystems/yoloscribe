# Tool: eks

Provides access to Amazon EKS and Kubernetes tools for deploying, managing, and
troubleshooting containerised workloads on EKS clusters.

## Use cases

Use this tool when the user wants to:
- Deploy or manage EKS cluster stacks
- Generate Kubernetes manifests for an application
- Apply YAML manifests to a cluster
- List Kubernetes resources (pods, services, deployments, etc.)
- Retrieve pod logs or Kubernetes events for troubleshooting
- Pull CloudWatch metrics for EKS workloads
- Search the EKS troubleshooting guide

## Authentication

This tool uses AWS credentials and requires `kubectl` access to the target cluster.
The deployment must have IAM permissions for `eks:*` and CloudWatch read operations.
