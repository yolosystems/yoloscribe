# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3>=1.35.0", "kubernetes>=30.0.0"]
# ///
"""Migrate per-user IRSA ServiceAccounts from one EKS cluster to another.

Every YoloScribe user gets a Kubernetes ServiceAccount named `user-{user_id}`
in the shared `K8S_NAMESPACE` namespace (see backend/aws/infra.py), annotated
with `eks.amazonaws.com/role-arn` pointing at a per-user IAM role
(`yoloscribe-user-{user_id}`). That role's trust policy only allows
`AssumeRoleWithWebIdentity` from the *source* cluster's OIDC provider, so
recreating the ServiceAccount alone is not enough — pods in the new cluster
would fail to assume the role. This script therefore does two things per
user, against the same AWS account:

  1. Recreates the ServiceAccount (same name/namespace/annotations) in the
     target cluster.
  2. Adds a second trust-policy statement to the user's IAM role that trusts
     the target cluster's OIDC provider (leaving the existing statement for
     the source cluster untouched, so both clusters can assume the role
     during the migration window).

The target cluster's IAM OIDC identity provider (created by whatever tool
stood up the cluster — eksctl/terraform/etc.) must already exist; this
script does not create it, only checks for it.

Usage:
    uv run --env-file .env scripts/migrate_service_accounts.py \\
        --source-kubeconfig ~/.kube/old-cluster.yaml \\
        --target-kubeconfig ~/.kube/new-cluster.yaml \\
        --target-cluster-name yoloscribe-prod-v2 \\
        --profile runyolo_admin \\
        [--dry-run]

Options:
    --source-kubeconfig PATH   Kubeconfig file for the existing cluster (required).
    --target-kubeconfig PATH   Kubeconfig file for the new cluster (required).
    --namespace NAME           Namespace to read/write ServiceAccounts in on both
                                clusters (default: $K8S_NAMESPACE or "yoloscribe").
    --target-cluster-name NAME EKS cluster name to auto-derive the target OIDC
                                provider from (via eks:DescribeCluster).
    --target-oidc-provider STR Explicit target OIDC provider, e.g.
                                "oidc.eks.us-west-2.amazonaws.com/id/XXXX".
                                Overrides --target-cluster-name if both given.
    --skip-oidc-check          Don't verify the target OIDC provider is
                                registered in IAM before proceeding.
    --overwrite                Replace the ServiceAccount on the target cluster
                                if one with the same name already exists.
    --profile NAME              AWS profile (default: $AWS_PROFILE).
    --region NAME                AWS region (default: $AWS_REGION or "us-west-2").
    --dry-run                   List what would change without changing anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROLE_ARN_ANNOTATION = "eks.amazonaws.com/role-arn"
AWS_REGION_DEFAULT = os.environ.get("AWS_REGION", "us-west-2")
NAMESPACE_DEFAULT = os.environ.get("K8S_NAMESPACE", "yoloscribe")


def _k8s_client(kubeconfig_path: str):
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config

    configuration = k8s_client.Configuration()
    k8s_config.load_kube_config(config_file=kubeconfig_path, client_configuration=configuration)
    api_client = k8s_client.ApiClient(configuration)
    return k8s_client.CoreV1Api(api_client)


def _list_user_service_accounts(v1, namespace: str) -> list:
    accounts = v1.list_namespaced_service_account(namespace=namespace)
    return [
        sa
        for sa in accounts.items
        if sa.metadata.name.startswith("user-")
        and (sa.metadata.annotations or {}).get(ROLE_ARN_ANNOTATION)
    ]


def _resolve_target_oidc_provider(
    session, region: str, cluster_name: str | None, explicit: str | None
) -> str:
    if explicit:
        return explicit.removeprefix("https://")
    if not cluster_name:
        raise ValueError("Must pass --target-oidc-provider or --target-cluster-name")
    eks = session.client("eks", region_name=region)
    cluster = eks.describe_cluster(name=cluster_name)["cluster"]
    issuer = cluster["identity"]["oidc"]["issuer"]
    return issuer.removeprefix("https://")


def _check_oidc_provider_registered(session, region: str, provider: str) -> bool:
    iam = session.client("iam")
    providers = iam.list_open_id_connect_providers()["OpenIDConnectProviderList"]
    for p in providers:
        if p["Arn"].endswith(f"oidc-provider/{provider}"):
            return True
    return False


def _ensure_service_account(target_v1, namespace: str, sa, overwrite: bool, dry_run: bool) -> str:
    from kubernetes import client as k8s_client

    name = sa.metadata.name
    annotations = dict(sa.metadata.annotations or {})
    labels = dict(sa.metadata.labels or {}) if sa.metadata.labels else None

    try:
        target_v1.read_namespaced_service_account(name=name, namespace=namespace)
        exists = True
    except Exception as exc:
        if "404" not in str(exc) and "Not Found" not in str(exc):
            return f"error checking existing SA: {exc}"
        exists = False

    if exists and not overwrite:
        return "exists-skipped"

    if dry_run:
        return "would-overwrite" if exists else "would-create"

    body = k8s_client.V1ServiceAccount(
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace, annotations=annotations, labels=labels)
    )
    try:
        if exists:
            target_v1.replace_namespaced_service_account(name=name, namespace=namespace, body=body)
            return "overwritten"
        target_v1.create_namespaced_service_account(namespace=namespace, body=body)
        return "created"
    except Exception as exc:
        return f"error: {exc}"


def _add_trust_for_target(
    session, role_arn: str, target_provider: str, namespace: str, sa_name: str, dry_run: bool
) -> str:
    # role_arn looks like arn:aws:iam::{account_id}:role/yoloscribe/yoloscribe-user-{id}
    try:
        account_id = role_arn.split(":")[4]
        role_name = role_arn.rsplit("/", 1)[-1]
    except IndexError:
        return f"error: unparseable role ARN {role_arn!r}"

    iam = session.client("iam")
    target_federated_arn = f"arn:aws:iam::{account_id}:oidc-provider/{target_provider}"

    try:
        current = iam.get_role(RoleName=role_name)["Role"]["AssumeRolePolicyDocument"]
    except Exception as exc:
        return f"error fetching role: {exc}"

    for statement in current.get("Statement", []):
        if statement.get("Principal", {}).get("Federated") == target_federated_arn:
            return "already-trusted"

    if dry_run:
        return "would-update"

    new_statement = {
        "Effect": "Allow",
        "Principal": {"Federated": target_federated_arn},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
            "StringEquals": {
                f"{target_provider}:sub": f"system:serviceaccount:{namespace}:{sa_name}",
                f"{target_provider}:aud": "sts.amazonaws.com",
            }
        },
    }
    current["Statement"].append(new_statement)
    try:
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(current))
        return "updated"
    except Exception as exc:
        return f"error updating trust policy: {exc}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-kubeconfig", required=True)
    parser.add_argument("--target-kubeconfig", required=True)
    parser.add_argument("--namespace", default=NAMESPACE_DEFAULT)
    parser.add_argument("--target-cluster-name", default=None)
    parser.add_argument("--target-oidc-provider", default=None)
    parser.add_argument("--skip-oidc-check", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--region", default=AWS_REGION_DEFAULT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import boto3

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()

    try:
        target_provider = _resolve_target_oidc_provider(
            session, args.region, args.target_cluster_name, args.target_oidc_provider
        )
    except Exception as exc:
        log.error("Could not resolve target OIDC provider: %s", exc)
        sys.exit(1)
    log.info("Target OIDC provider: %s", target_provider)

    if not args.skip_oidc_check:
        if not _check_oidc_provider_registered(session, args.region, target_provider):
            log.error(
                "No IAM OIDC identity provider found for %r. Create it (eksctl utils "
                "associate-iam-oidc-provider or equivalent) before migrating, or pass "
                "--skip-oidc-check to proceed anyway.",
                target_provider,
            )
            sys.exit(1)
        log.info("Confirmed target OIDC provider is registered in IAM.")

    source_v1 = _k8s_client(args.source_kubeconfig)
    target_v1 = _k8s_client(args.target_kubeconfig)

    service_accounts = _list_user_service_accounts(source_v1, args.namespace)
    log.info("Found %d user ServiceAccount(s) in namespace %r on source cluster", len(service_accounts), args.namespace)
    if not service_accounts:
        return

    if args.dry_run:
        log.info("Dry run — no changes will be made")

    counts: dict[str, int] = {}
    errors: list[str] = []

    for sa in service_accounts:
        name = sa.metadata.name
        role_arn = sa.metadata.annotations[ROLE_ARN_ANNOTATION]

        sa_status = _ensure_service_account(target_v1, args.namespace, sa, args.overwrite, args.dry_run)
        trust_status = _add_trust_for_target(session, role_arn, target_provider, args.namespace, name, args.dry_run)

        log.info("%s: serviceaccount=%s trust=%s", name, sa_status, trust_status)
        counts[f"sa:{sa_status.split(':')[0]}"] = counts.get(f"sa:{sa_status.split(':')[0]}", 0) + 1
        counts[f"trust:{trust_status.split(':')[0]}"] = counts.get(f"trust:{trust_status.split(':')[0]}", 0) + 1
        if sa_status.startswith("error"):
            errors.append(f"{name}: serviceaccount {sa_status}")
        if trust_status.startswith("error"):
            errors.append(f"{name}: trust policy {trust_status}")

    log.info("Summary: %s", counts)
    if errors:
        log.error("Completed with %d error(s):", len(errors))
        for e in errors:
            log.error("  %s", e)
        sys.exit(1)
    log.info("Done.")


if __name__ == "__main__":
    main()
