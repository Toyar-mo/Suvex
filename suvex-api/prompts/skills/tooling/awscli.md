---
name: awscli
description: AWS CLI v1 usage in the sandbox for IAM, S3, and metadata validation.
---

# awscli

Installed at `/opt/osint-venv/bin/aws` (linked as `aws`).

```bash
aws sts get-caller-identity
aws iam list-users --max-items 20
aws s3 ls
aws s3api get-bucket-acl --bucket <bucket>
aws iam simulate-principal-policy --policy-source-arn <arn> \
  --action-names s3:GetObject --resource-arns <arn>
```

Rules:
- `sts get-caller-identity` first: it proves which principal the
  credentials belong to before any conclusion about permissions.
- Prefer read-only calls; do not delete or modify resources.
- If no credentials are configured, report that — do not invent account IDs.
