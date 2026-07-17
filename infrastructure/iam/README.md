# metron-runner-executor-role setup

Companion to the metron-runner-dispatcher Lambda (nousergon-data). Two-step
provisioning, both required — missing step 2 was a real bug found live
2026-07-17 (metron's first dispatch: EC2 instances launched fine but never
registered with SSM at all, sat "waiting_ssm" until the bootstrap deadline
reaped them, 2/2 first attempts failed this way).

```sh
aws iam create-role --role-name metron-runner-executor-role \
  --assume-role-policy-document file://infrastructure/iam/metron-runner-executor-role-trust.json
aws iam put-role-policy --role-name metron-runner-executor-role \
  --policy-name metron-runner-executor-policy \
  --policy-document file://infrastructure/iam/metron-runner-executor-role-policy.json
aws iam create-instance-profile --instance-profile-name metron-runner-executor-profile
aws iam add-role-to-instance-profile --instance-profile-name metron-runner-executor-profile \
  --role-name metron-runner-executor-role

# REQUIRED — without this the SSM agent on the box can never call
# ssm:UpdateInstanceInformation/ssmmessages:*/ec2messages:* to register
# itself with Systems Manager at all. The inline policy above only covers
# reading the one PAT param; it does NOT include baseline agent-registration
# permissions. Confirmed present on the proven alpha-engine-config-runner-
# executor-role (verified via aws iam list-attached-role-policies) but
# missing from the JSON files this role's policy was mirrored from — that
# attachment is a separate step done outside the checked-in policy JSON,
# easy to miss when mirroring the pattern to a new repo.
aws iam attach-role-policy --role-name metron-runner-executor-role \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```
