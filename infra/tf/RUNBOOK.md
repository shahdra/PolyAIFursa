# Runbook — running the cluster from your laptop

Copy-paste, in order. Every command runs on your Mac.

**Total time from nothing to a working cluster: ~15 minutes**, most of it waiting.

| Step | What it does | Time |
|---|---|---|
| [1](#step-1--set-your-shell-up) | Set up your shell | instant |
| [2](#step-2--create-the-infrastructure) | Create the AWS infrastructure | ~4 min |
| [3](#step-3--wait-for-the-control-plane) | Wait for `kubeadm init` | ~3 min |
| [4](#step-4--wait-for-the-worker-to-join) | Wait for the worker to join | ~6 min |
| [5](#step-5--bootstrap-the-cluster) | Install Calico, ArgoCD, the apps | ~5 min |
| [6](#step-6--check-it-worked) | Verify | ~1 min |
| [7](#step-7--open-the-app) | Open the app and the ArgoCD UI | — |
| [8](#step-8--destroy-everything) | **Destroy everything** | ~4 min |

> **Step 4 is easy to skip and shouldn't be.** If you bootstrap before the worker
> has joined, ArgoCD's pods have nowhere to run (the control plane carries a
> `NoSchedule` taint) and the install fails half-way. This is the single most common
> way this goes wrong.

---

## Step 1 — Set your shell up

```bash
cd /Users/saed/shahd/PolyAIFursa/infra/tf
export KEY=/Users/saed/shahd-key.pem
```

Everything below uses `$KEY`. Keep this shell open for the whole session.

<details>
<summary>First time on a new machine? Check your tools</summary>

```bash
terraform -version           # need >= 1.7
aws --version                # need v2
aws sts get-caller-identity  # must show account 228281126655, user shahdra
ls -l $KEY                   # must exist; chmod 600 if it complains later
ls -l ../../services/agent/.env   # the app secrets, used in step 5
terraform init               # only after a fresh git clone
```
</details>

---

## Step 2 — Create the infrastructure

Builds the VPC, both subnets, security groups, IAM roles, the control plane, and the
worker ASG. **38 resources.**

```bash
terraform workspace select us-east-1
terraform apply -var-file=tfvars/us-east-1.tfvars
```

Type `yes`. Then capture the control plane's address:

```bash
export CP=$(terraform output -raw control_plane_public_ip)
echo "control plane: $CP"
```

> This IP is **different every rebuild**. Never write it down anywhere — always read
> it from `terraform output`.

<details>
<summary>Deploying to a different region</summary>

The workspace name must equal the region. Create a tfvars file first:

```bash
sed 's/us-east-1/us-east-2/' tfvars/us-east-1.tfvars > tfvars/us-east-2.tfvars
terraform workspace select -or-create us-east-2
terraform apply -var-file=tfvars/us-east-2.tfvars
```

No code changes needed. A `precondition` in `main.tf` fails the plan if the
workspace and region disagree.

⚠️ **EC2 key pairs are per-region.** `shahd-key` exists in `us-east-1` only, so
create one in the new region first (or edit `ssh_key_name` in the new tfvars):

```bash
aws ec2 create-key-pair --region us-east-2 --key-name shahd-key \
  --query KeyMaterial --output text > ~/shahd-key-us-east-2.pem
chmod 600 ~/shahd-key-us-east-2.pem
```
</details>

---

## Step 3 — Wait for the control plane

`terraform apply` finishes when AWS has *created* the instance. The instance then
spends a few minutes installing `cri-o` and running `kubeadm init`.

```bash
ssh -o StrictHostKeyChecking=no -i $KEY ubuntu@$CP \
  'until test -f /var/lib/cloud/control-plane-ready; do echo "still initializing..."; sleep 15; done; echo "CONTROL PLANE READY"'
```

Wait for `CONTROL PLANE READY`. That file is written on the last line of the
user-data script, so its existence means `kubeadm init` completed cleanly.

> **Why not just SSH in and check?** sshd accepts connections long before
> `kubeadm init` finishes. Polling port 22 tells you nothing useful.

**Stuck for more than ~6 minutes?** Read the boot log — it contains the whole story:

```bash
ssh -i $KEY ubuntu@$CP 'sudo tail -50 /var/log/user-data.log'
```

---

## Step 4 — Wait for the worker to join

The worker installs the same packages, then polls SSM for the join command. It
typically joins **3-5 minutes after** the control plane is ready.

```bash
ssh -i $KEY ubuntu@$CP \
  'until [ $(kubectl get nodes --no-headers 2>/dev/null | wc -l) -ge 2 ]; do echo "waiting for worker..."; sleep 20; done; kubectl get nodes'
```

You want **two** nodes listed. Both will show `NotReady` — correct, there's no
network plugin yet. Step 5 installs it.

> **Don't skip ahead.** The control plane has a `NoSchedule` taint, so with no
> worker there is nowhere to place ArgoCD's 7 pods. They'd sit `Pending` and the
> bootstrap would time out at phase 6b.

**Worker never appears?** See [Troubleshooting](#worker-never-joins).

---

## Step 5 — Bootstrap the cluster

Two commands. The first copies the app secrets to the node; the second installs
everything.

```bash
cd /Users/saed/shahd/PolyAIFursa

scp -i $KEY services/agent/.env ubuntu@$CP:/tmp/polyai.env

ssh -i $KEY ubuntu@$CP "POLYAI_ENV_FILE=/tmp/polyai.env bash -s" < infra/k8s/bootstrap.sh
```

The script runs 8 phases and prints progress:

| Phase | What it installs | Why it matters |
|---|---|---|
| 0 | Preflight + clone the repo on the node | needs the manifests locally |
| 1 | **Calico CNI** | pinned to always-VXLAN, or cross-node pod traffic breaks on AWS |
| 2 | Wait for nodes `Ready` | Calico is what flips them; nothing schedules until this passes |
| 3 | EBS CSI driver + `ebs-sc` StorageClass | Prometheus/Grafana volumes |
| 4 | Namespaces `dev`, `prod`, `argocd` | one cluster, two environments |
| 5 | `polyai-secrets` in dev + prod | 6 keys; without it every pod fails to start |
| 6 | ArgoCD (+ 6b: wait for 3 rollouts) | the GitOps engine |
| 7 | Two app-of-apps parents | these create the 12 child Applications |
| 8 | Summary — admin password + app URLs | |

Then wipe the credentials off the node:

```bash
ssh -i $KEY ubuntu@$CP 'shred -u /tmp/polyai.env'
```

> The script is **idempotent** — safe to re-run any number of times. It's the exact
> same script `cluster.yaml` runs in CI.

---

## Step 6 — Check it worked

```bash
cd /Users/saed/shahd/PolyAIFursa/infra/tf

ssh -i $KEY ubuntu@$CP 'kubectl get nodes'
```
→ 2 nodes, both **`Ready`**.

```bash
ssh -i $KEY ubuntu@$CP 'kubectl -n dev get pods'
```
→ 6 pods, all **`1/1 Running`**. Grafana and Prometheus are slowest (EBS attach).

```bash
ssh -i $KEY ubuntu@$CP 'kubectl -n argocd get applications'
```
→ 14 Applications. The 7 dev ones `Synced`/`Healthy`.

> The 6 **prod** apps stay **`OutOfSync`/`Missing`**. That is the manual-sync
> promotion gate working as designed — not a failure. Deploy prod deliberately with
> `argocd app sync <service>-prod`.

---

## Step 7 — Open the app

Since task007 everything is published on **HTTPS** through an ALB, so there are no
node ports to look up and no IP that changes when the ASG replaces a worker:

```bash
terraform output urls
```

| | URL |
|---|---|
| prod frontend | `https://shahdra.fursa.click` *(after `argocd app sync`)* |
| prod agent | `https://shahdra.fursa.click/chat` |
| dev frontend | `https://dev.shahdra.fursa.click` |
| dev agent | `https://dev.shahdra.fursa.click/chat` |
| ArgoCD | `https://argocd.shahdra.fursa.click` |
| Grafana | `https://grafana.shahdra.fursa.click` |
| Prometheus | `https://prometheus.shahdra.fursa.click` *(basic auth)* |
| Alertmanager | `https://alertmanager.shahdra.fursa.click` *(basic auth)* |

Frontend and agent share a hostname on purpose: `lib/api.ts` falls back to the page's
own origin when there is no explicit port, so `/chat` on the same host means no CORS
and no rebuild when anything moves.

**Passwords.** All three are printed by `bootstrap.sh` on the run that CREATES them,
and never again. ArgoCD's is recoverable; the other two are hashed or generated:

```bash
# ArgoCD — user `admin`
ssh -i $KEY ubuntu@$CP \
  'kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo'

# Grafana — user `admin`
ssh -i $KEY ubuntu@$CP \
  'kubectl -n monitoring get secret grafana-admin -o jsonpath="{.data.admin-password}" | base64 -d; echo'
```

Prometheus/Alertmanager basic auth is stored as an apr1 hash and **cannot be read
back**. To reset it to something you know:

```bash
ssh -i $KEY ubuntu@$CP 'PW=newpassword; kubectl -n monitoring create secret generic \
  monitoring-basic-auth --from-literal=auth="polyai:$(openssl passwd -apr1 "$PW")" \
  --dry-run=client -o yaml | kubectl apply -f -'
```

### If a hostname returns 503

That is the ALB reporting no healthy targets — the load balancer is fine, nothing is
answering behind it. In order:

```bash
# 1. Are the ASG instances passing the target-group health check?
aws elbv2 describe-target-health \
  --target-group-arn $(terraform output -raw ingress_target_group_arn)

# 2. Is the ingress controller up, and on the port the target group expects (30080)?
ssh -i $KEY ubuntu@$CP 'kubectl -n ingress-nginx get pods,svc'

# 3. Bypass the ALB entirely - this isolates "broken ALB" from "broken cluster"
curl -H 'Host: dev.shahdra.fursa.click' http://$WORKER:30080/
```

### One manual step: confirm the alert email

Terraform creates the SNS topic and the email subscription, but **AWS requires the
recipient to click a confirmation link** before it will deliver anything. Until then
alerts fire, reach Alertmanager, and publish to SNS successfully — and no mail
arrives, with nothing in any log to explain it.

```bash
aws sns list-subscriptions-by-topic --topic-arn $(terraform output -raw alerts_sns_topic_arn)
```

`PendingConfirmation` means go and click the link in your inbox. The confirmation
survives `terraform destroy`, because the topic ARN is derived from the cluster name
and comes back identical.

<details>
<summary>Run kubectl from your laptop instead of over SSH</summary>

```bash
cd /Users/saed/shahd/PolyAIFursa/infra/tf
eval "$(terraform output -raw fetch_kubeconfig_command)"
export KUBECONFIG=$PWD/kubeconfig-us-east-1.yaml
kubectl get nodes
```

Re-fetch after every rebuild — the file embeds the control plane's public IP.
</details>

---

## Step 8 — Destroy everything

**Order matters.** Do 8a before 8b or you leave paid-for volumes behind.

### 8a. Release the EBS volumes first

Prometheus and Grafana volumes were created by the **CSI driver**, not Terraform, so
`terraform destroy` cannot see them. Deleting the PVCs makes Kubernetes delete the
underlying volumes (their reclaim policy is `Delete`).

```bash
ssh -i $KEY ubuntu@$CP 'kubectl -n dev delete pvc grafana-pvc prometheus-pvc'
ssh -i $KEY ubuntu@$CP 'kubectl -n prod delete pvc grafana-pvc prometheus-pvc --ignore-not-found'
sleep 20
```

> ArgoCD's selfHeal may recreate the PVCs within seconds. That's fine — the CSI
> driver has already issued the EBS deletes, and you're tearing the cluster down
> anyway.

### 8b. Destroy the infrastructure

```bash
cd /Users/saed/shahd/PolyAIFursa/infra/tf
terraform destroy -var-file=tfvars/us-east-1.tfvars
```

Type `yes`. Expect **37 to destroy**, ~4 minutes.

### 8c. Tidy the SSM parameter (optional)

Also not Terraform-managed — the control plane creates it at boot. Harmless if left
(a dead token for a dead IP), and the next control plane deletes it anyway.

```bash
aws ssm delete-parameter --name "/polyai/shahdra-polyai-us-east-1/join-command" 2>/dev/null || true
```

### 8d. Confirm nothing is left

```bash
terraform state list | wc -l     # want 0

aws ec2 describe-instances \
  --filters "Name=tag:Owner,Values=shahdra" "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output text
```

Check for orphaned volumes — **by your own IDs only.** This account is shared and
has 60+ available volumes belonging to other students:

```bash
aws ec2 describe-volumes --filters "Name=status,Values=available" \
  --query 'Volumes[?CreateTime>=`2026-07-30`].[VolumeId,Size,CreateTime]' --output table

# then delete individually, never in bulk:
# aws ec2 delete-volume --volume-id vol-xxxxx
```

---

### Just parking it for the night?

Cheaper than a destroy, and the cluster survives:

```bash
terraform apply -var-file=tfvars/us-east-1.tfvars -var worker_desired_capacity=0
ssh -i $KEY ubuntu@$CP 'kubectl get nodes'          # find the NotReady one
ssh -i $KEY ubuntu@$CP 'kubectl delete node <name>' # node cleanup is manual by design
```

Scale back up by re-applying with `worker_desired_capacity=1`.

⚠️ **But note:** the account's budget keeper stops all instances at 16:00 and 00:00,
and a stopped control plane can't be recovered (see below). Destroying and rebuilding
is often the more honest option.

---

## Troubleshooting

### ⚠️ Everything stopped on its own

**This will happen.** A course Lambda (`aws-learning-budget-keeper-function`) stops
**every** EC2 instance in the account at **16:00 and 00:00** daily, plus ad-hoc.

A stopped control plane is **not restartable**: `kubeadm init` baked the public IP
into the API server's TLS certificate, and a restart assigns a new IP, so `kubectl`
fails verification. Don't try to start it.

**Recovery — rebuild:**
```bash
cd /Users/saed/shahd/PolyAIFursa/infra/tf
terraform apply -var-file=tfvars/us-east-1.tfvars
export CP=$(terraform output -raw control_plane_public_ip)
# then repeat steps 3, 4, 5
```

If Terraform treats the stopped instance as still valid, force replacement:
```bash
terraform taint 'module.k8s_cluster.aws_instance.control_plane'
terraform apply -var-file=tfvars/us-east-1.tfvars
```

### Worker never joins

```bash
WORKER_IP=$(aws ec2 describe-instances \
  --filters "Name=tag:Cluster,Values=$(terraform output -raw cluster_name)" \
            "Name=tag:Role,Values=worker" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].PublicIpAddress' --output text)

ssh -i $KEY ubuntu@$WORKER_IP 'sudo tail -30 /var/log/user-data.log'
```

Check the join command names the **current** control plane:
```bash
aws ssm get-parameter --name "/polyai/shahdra-polyai-us-east-1/join-command" \
  --with-decryption --query Parameter.Value --output text | grep -oE '^kubeadm join [0-9.]+'
terraform output -raw control_plane_private_ip
```

If they disagree the parameter is stale — the worker's liveness probe keeps it
polling rather than joining a dead endpoint. Force a fresh worker:
```bash
aws autoscaling start-instance-refresh \
  --auto-scaling-group-name "$(terraform output -raw worker_asg_name)"
```

### ArgoCD pods `Pending`, or `argocd-server` in `CrashLoopBackOff`

Two different causes:

**`Pending`** → no worker joined. The control plane's `NoSchedule` taint means
nothing can be placed. Check `kubectl get nodes`; fix per above, then re-run step 5.

**`CrashLoopBackOff` with `lookup argocd-redis: i/o timeout`** → cross-node pod
networking is broken. Confirm Calico is tunnelling:
```bash
ssh -i $KEY ubuntu@$CP 'kubectl get ippools default-ipv4-ippool -o jsonpath="{.spec.vxlanMode}"'
```
Must print **`Always`**. If it says `CrossSubnet`, the bootstrap script's inline
`Installation` didn't apply — re-run step 5, then restart ArgoCD:
```bash
ssh -i $KEY ubuntu@$CP 'kubectl -n argocd rollout restart deploy/argocd-server deploy/argocd-repo-server'
```

### An app is stuck `OutOfSync` and won't self-heal

ArgoCD gives up after 5 failed syncs and stays in backoff even once the blocker is
gone. Force one:
```bash
ssh -i $KEY ubuntu@$CP \
  'kubectl -n argocd patch app grafana-dev --type=merge \
     -p "{\"operation\":{\"initiatedBy\":{\"username\":\"manual\"},\"sync\":{\"revision\":\"dev\"}}}"'
```

### `Permission denied (publickey)`
```bash
chmod 600 $KEY
```

### `Host key verification failed`
Normal after a rebuild — the IP is recycled with a new host key.
```bash
ssh-keygen -R $CP
```

### A pod is stuck `ContainerCreating` on a volume
```bash
ssh -i $KEY ubuntu@$CP 'kubectl -n dev get pvc'
```
PVCs should be `Bound` to an auto-named `pvc-<uuid>` volume. A `Pending` PVC saying
`waiting for first consumer` alongside an older pod means the pod predates the PVC —
delete the pod and the ReplicaSet makes one that binds.

---

## Doing all of this in one command

Once `cluster.yaml` is on `main`, steps 2-5 collapse to:

```bash
gh workflow run cluster.yaml -f region=us-east-1 -f worker_desired_capacity=1
gh run watch
```

Same Terraform, same `bootstrap.sh`. This runbook stays the manual fallback and the
debugging path.

---

## Cheat sheet

```bash
cd /Users/saed/shahd/PolyAIFursa/infra/tf
export KEY=/Users/saed/shahd-key.pem
export CP=$(terraform output -raw control_plane_public_ip)

ssh -i $KEY ubuntu@$CP 'kubectl get nodes'
ssh -i $KEY ubuntu@$CP 'kubectl -n dev get pods'
ssh -i $KEY ubuntu@$CP 'kubectl -n argocd get applications'
ssh -i $KEY ubuntu@$CP 'sudo tail -40 /var/log/user-data.log'
terraform output
```
