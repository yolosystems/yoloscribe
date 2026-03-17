# Tool: aws-network

Provides access to AWS networking tools covering VPC, Transit Gateway, Cloud WAN,
Network Firewall, and VPN. Helps inspect and troubleshoot network topology and routing.

## Use cases

Use this tool when the user wants to:
- Identify which resource owns an IP address or ENI
- Inspect VPC configuration, subnets, and route tables
- List and analyse Transit Gateway attachments and routes
- Inspect Cloud WAN core network topology
- Review Network Firewall rules
- Retrieve VPC Flow Logs for a traffic investigation
- Understand packet path methodology through the network

## Authentication

This tool uses AWS credentials. The deployment must have IAM permissions for
EC2 and Network Manager read operations (`ec2:Describe*`, `network-manager:Get*`,
`network-firewall:Describe*`).
