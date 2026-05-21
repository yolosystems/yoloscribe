"""AWS infrastructure provisioning and deprovisioning for YoloScribe users.

Handles IAM role + inline policy, K8s ServiceAccount, and Secrets Manager placeholder.
"""

import json
import logging
import os

from fastapi import HTTPException

from config import (
    AWS_ACCOUNT_ID,
    AWS_REGION,
    EKS_OIDC_PROVIDER,
    K8S_NAMESPACE,
    LOCAL_MODE,
    S3_BUCKET,
    SQS_INDEXING_QUEUE_URL,
    SQS_QUEUE_URL,
    boto_session,
)


async def provision_user_infrastructure(user_id: str, site_name: str) -> None:
    """Provision IAM role, K8s ServiceAccount, and SM placeholder for a new user."""
    if LOCAL_MODE:
        return
    role_name = f"yoloscribe-user-{user_id}"
    sa_name = f"user-{user_id}"
    sm_secret_name = f"yoloscribe/{user_id}/.initialized"

    iam = boto_session.client("iam")
    secrets_manager = boto_session.client("secretsmanager", region_name=AWS_REGION)

    # 1. Create IAM role with IRSA trust policy
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Federated": f"arn:aws:iam::{AWS_ACCOUNT_ID}:oidc-provider/{EKS_OIDC_PROVIDER}"
                },
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{EKS_OIDC_PROVIDER}:sub": f"system:serviceaccount:{K8S_NAMESPACE}:{sa_name}",
                        f"{EKS_OIDC_PROVIDER}:aud": "sts.amazonaws.com",
                    }
                },
            }
        ],
    }
    iam.create_role(
        RoleName=role_name,
        Path="/yoloscribe/",
        AssumeRolePolicyDocument=json.dumps(trust_policy),
        Description=f"IRSA role for YoloScribe user {user_id}",
    )

    # 2. Attach inline policy
    secret_arn_prefix = (
        f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT_ID}:secret:yoloscribe/{user_id}/"
    )
    s3_bucket_arn = f"arn:aws:s3:::{S3_BUCKET}"
    statements: list[dict] = [
        {
            "Sid": "SecretsManagerUserSecrets",
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue",
                "secretsmanager:PutSecretValue",
            ],
            "Resource": f"{secret_arn_prefix}*",
        },
        {
            "Sid": "S3ReadWriteUserPrefix",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": f"{s3_bucket_arn}/{site_name}/*",
        },
        {
            "Sid": "S3ReadToolsPrefix",
            "Effect": "Allow",
            "Action": "s3:GetObject",
            "Resource": f"{s3_bucket_arn}/.tools/*",
        },
        {
            "Sid": "S3ListUserPrefix",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": s3_bucket_arn,
            "Condition": {
                "StringLike": {"s3:prefix": [f"{site_name}/*", ".tools/*", ".skills/*"]}
            },
        },
    ]
    if SQS_QUEUE_URL:
        queue_name = SQS_QUEUE_URL.rstrip("/").split("/")[-1]
        agent_queue_arn = f"arn:aws:sqs:{AWS_REGION}:{AWS_ACCOUNT_ID}:{queue_name}"
        statements.append(
            {
                "Sid": "SQSSendAgentQueue",
                "Effect": "Allow",
                "Action": "sqs:SendMessage",
                "Resource": agent_queue_arn,
            }
        )
    if SQS_INDEXING_QUEUE_URL:
        queue_name = SQS_INDEXING_QUEUE_URL.rstrip("/").split("/")[-1]
        indexing_queue_arn = f"arn:aws:sqs:{AWS_REGION}:{AWS_ACCOUNT_ID}:{queue_name}"
        statements.append(
            {
                "Sid": "SQSSendIndexingQueue",
                "Effect": "Allow",
                "Action": "sqs:SendMessage",
                "Resource": indexing_queue_arn,
            }
        )
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="yoloscribe-user-access",
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    role_arn = f"arn:aws:iam::{AWS_ACCOUNT_ID}:role/yoloscribe/{role_name}"

    # 3. Create K8s ServiceAccount annotated with role ARN
    try:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]

        kubeconfig = os.environ.get("KUBECONFIG")
        if kubeconfig:
            k8s_config.load_kube_config(config_file=kubeconfig)
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

        v1 = k8s_client.CoreV1Api()
        sa = k8s_client.V1ServiceAccount(
            metadata=k8s_client.V1ObjectMeta(
                name=sa_name,
                namespace=K8S_NAMESPACE,
                annotations={"eks.amazonaws.com/role-arn": role_arn},
            )
        )
        v1.create_namespaced_service_account(namespace=K8S_NAMESPACE, body=sa)
    except Exception as k8s_exc:
        raise HTTPException(
            status_code=502, detail=f"K8s ServiceAccount creation failed: {k8s_exc}"
        ) from k8s_exc

    # 4. Create Secrets Manager placeholder
    secrets_manager.create_secret(
        Name=sm_secret_name,
        SecretString=json.dumps({"initialized": "true"}),
        Description=f"Placeholder secret for YoloScribe user {user_id}",
    )


async def deprovision_user_infrastructure(user_id: str, site_name: str | None) -> list[str]:
    """Delete IAM role/policy, SM secrets, and K8s ServiceAccount for a user.

    Returns a list of warning strings. Never raises.
    """
    if LOCAL_MODE:
        return []
    warnings: list[str] = []
    role_name = f"yoloscribe-user-{user_id}"
    sa_name = f"user-{user_id}"

    iam = boto_session.client("iam")
    secrets_manager = boto_session.client("secretsmanager", region_name=AWS_REGION)

    # 1. Delete IAM inline policy
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName="yoloscribe-user-access")
    except iam.exceptions.NoSuchEntityException:
        pass
    except Exception as exc:
        warnings.append(f"IAM policy delete warning: {exc}")

    # 2. Delete IAM role
    try:
        iam.delete_role(RoleName=role_name)
    except iam.exceptions.NoSuchEntityException:
        pass
    except Exception as exc:
        warnings.append(f"IAM role delete warning: {exc}")

    # 3. Delete SM secrets under yoloscribe/{user_id}/
    prefix = f"yoloscribe/{user_id}/"
    try:
        paginator = secrets_manager.get_paginator("list_secrets")
        for page in paginator.paginate():
            for secret in page.get("SecretList", []):
                if secret["Name"].startswith(prefix):
                    try:
                        secrets_manager.delete_secret(
                            SecretId=secret["ARN"],
                            ForceDeleteWithoutRecovery=True,
                        )
                    except Exception as exc:
                        warnings.append(f"SM secret delete warning ({secret['Name']}): {exc}")
    except Exception as exc:
        warnings.append(f"SM list secrets warning: {exc}")

    # 4. Delete K8s ServiceAccount
    try:
        from kubernetes import client as k8s_client  # type: ignore[import-untyped]
        from kubernetes import config as k8s_config  # type: ignore[import-untyped]

        kubeconfig = os.environ.get("KUBECONFIG")
        if kubeconfig:
            k8s_config.load_kube_config(config_file=kubeconfig)
        else:
            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

        v1 = k8s_client.CoreV1Api()
        try:
            v1.delete_namespaced_service_account(name=sa_name, namespace=K8S_NAMESPACE)
        except Exception as exc:
            if "404" not in str(exc) and "Not Found" not in str(exc):
                warnings.append(f"K8s ServiceAccount delete warning: {exc}")
    except Exception as k8s_exc:
        warnings.append(f"K8s config warning: {k8s_exc}")

    for w in warnings:
        logging.warning(w)
    return warnings
