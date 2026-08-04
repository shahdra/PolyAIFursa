# ArgoCD GitOps — bootstrap & runbook

This directory holds the ArgoCD `Application` manifests that make this repo the
**single source of truth** for the cluster. CI builds images and commits the new
tag into `infra/k8s/<env>/<svc>/`; ArgoCD watches the repo and syncs the cluster
to match. No more SSH-into-the-box `docker compose` deploys.

- **dev apps** (`dev/*.yaml`) → track the **`dev` branch**, deploy to namespace `dev`,
  **auto-sync** (`prune` + `selfHeal`).
- **prod apps** (`prod/*.yaml`) → track the **`main` branch**, deploy to namespace `prod`,
  **manual sync** (review before promoting).
- **`app-of-apps-dev.yaml`** → creates the 6 dev apps (`path: infra/k8s/argo/dev`, branch `dev`).
- **`app-of-apps-prod.yaml`** → creates the 6 prod apps (`path: infra/k8s/argo/prod`, branch `main`).

Promotion model: merge `dev` → `main`. Dev deploys from the `dev` branch; promoting to
prod means merging into `main`, then manually syncing the prod apps.

## Cluster reference — discover it, don't hardcode it

The cluster is Terraform-managed (`infra/tf/`) and **disposable**: instance IDs and
public IPs change on every `terraform apply`. This section used to list them
literally, which went stale the first time the cluster was rebuilt. Read them from
Terraform instead:

```bash
cd infra/tf
terraform workspace select us-east-1        # workspace == region

terraform output                             # everything at once
terraform output -raw control_plane_public_ip
terraform output -raw worker_asg_name
terraform output -raw ssh_command            # ready-to-paste ssh line
```

The worker's public IP is not a Terraform output (the ASG creates workers, so it
changes on every scale event). Ask EC2:

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Cluster,Values=$(terraform output -raw cluster_name)" \
            "Name=tag:Role,Values=worker" \
            "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].PublicIpAddress' --output text
```

> Filter on the `Cluster` tag, not just `Role=worker`. This is a **shared course
> account** — a bare `Role=worker` filter can match another student's instance.

Ports opened by the security groups in `infra/tf/modules/k8s-cluster/main.tf`:

| Port          | Where         | Purpose                                          |
|---------------|---------------|--------------------------------------------------|
| `22`          | both          | SSH                                              |
| `6443`        | control plane | Kubernetes API server                            |
| all           | both          | intra-VPC (`10.0.0.0/16`) — kubelet, etcd, Calico |
| `30000-32767` | worker        | NodePorts — frontend / agent / grafana           |

> **Note:** `8080` is **not** open on the control plane. Reach the ArgoCD UI over an
> SSH tunnel (Option A in step 3), which needs only port 22.

> All `kubectl`/`argocd` commands below run **on the control-plane node**. SSH in first:
> ```bash
> ssh -i <your-key.pem> ubuntu@$(cd infra/tf && terraform output -raw control_plane_public_ip)
> ```

---

## Step 1 — Install ArgoCD (one time)

Creates the `argocd` namespace and installs all ArgoCD components (server, repo
server, application controller, Redis) into it.

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

Wait until every ArgoCD pod is `Running`:

```bash
kubectl -n argocd get pods -w   # Ctrl-C once all show Running/Ready
```

(Optional but recommended) install the `argocd` CLI on the control-plane so you
can sync/inspect from the terminal:

```bash
curl -sSL -o /tmp/argocd https://github.com/argoproj/argo-cd/releases/latest/download/argocd-linux-amd64
sudo install -m 555 /tmp/argocd /usr/local/bin/argocd && rm /tmp/argocd
argocd version --client
```

---

## Step 2 — Get the admin password

The username is `admin`. The initial password is stored in a Secret (base64):

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath="{.data.password}" | base64 -d; echo
```

Copy the printed string — you'll paste it into the UI (step 3) or `argocd login`.

> After first login, change it: `argocd account update-password`, then you can
> delete the initial secret: `kubectl -n argocd delete secret argocd-initial-admin-secret`.

---

## Step 3 — Open the ArgoCD UI

ArgoCD's `argocd-server` Service serves **HTTPS on 443** inside the cluster with a
**self-signed cert**. `kubectl port-forward` exposes it on a local port. You have
two ways to reach it — pick one.

### Option A (recommended, no extra open port) — SSH tunnel

Only port `22` needs to be open (it already is). Nothing is exposed to the internet.

1. On the **control-plane**, forward the Service to localhost:
   ```bash
   kubectl port-forward svc/argocd-server -n argocd 8080:443
   ```
   (Binds to `127.0.0.1:8080` on the node — not reachable from outside. Leave it running.)

2. On **your laptop**, open an SSH tunnel from your local `8080` to the node's `8080`:
   ```bash
   ssh -i <your-key.pem> -L 8080:localhost:8080 ubuntu@<control-plane-public-ip>
   ```

3. Browse to **`https://localhost:8080`** → accept the self-signed cert warning →
   log in as `admin` with the password from step 2.

### Option B (direct, needs a temporary SG rule)

The old hand-built cluster left `8080` open to `0.0.0.0/0`, so you could bind the
port-forward to all interfaces and browse straight to the node. The Terraform
security groups **deliberately do not open 8080** — the ArgoCD UI is only
password-protected, and this is a shared account, so exposing it to the internet
by default is the wrong trade.

Option A above needs no open port and is the recommended route. If you still want
direct access, add the rule yourself, scoped to your own IP:

```bash
cd infra/tf
MY_IP="$(curl -s https://checkip.amazonaws.com)/32"

aws ec2 authorize-security-group-ingress \
  --group-id "$(terraform output -raw control_plane_security_group_id)" \
  --protocol tcp --port 8080 --cidr "$MY_IP"

# on the control plane:
kubectl port-forward svc/argocd-server -n argocd 8080:443 --address 0.0.0.0
```

Browse to **`https://<control-plane-public-ip>:8080`** → accept the cert → log in.

Revoke it when you're done (`authorize` → `revoke`, same arguments). Note this is a
manual change outside Terraform, so it vanishes on the next cluster rebuild — which
is the intended behaviour, not a bug.

### (Optional) log in with the CLI instead of the browser

```bash
argocd login localhost:8080 --username admin --password '<password>' --insecure
# --insecure accepts the self-signed cert; use with the port-forward from Option A
```

---

## Step 4 — Reconcile the repo to what's LIVE (before auto-sync)

The cluster already runs all six services in `dev` and `prod` (hand-applied).
ArgoCD **adopts existing resources in place** when name/namespace/kind match —
it updates them, it does **not** delete and recreate. Service types and NodePorts
in the manifests already match the live cluster, so the only difference ArgoCD
will find is the **image tag**.

Print what's actually running:

```bash
kubectl -n dev  get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'; echo
kubectl -n prod get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'; echo
```

Compare each to the `image:` line in `infra/k8s/<env>/<svc>/<svc>.yaml`.
**If a live pod runs a newer image than the manifest**, edit the manifest to that
tag and commit/push first — otherwise the first sync will roll the pod **back** to
the manifest's older tag. When git matches live, adoption is a no-op rollout.

---

## Step 5 — Bootstrap DEV (app-of-apps-dev)

There is one parent app per environment. Bootstrap **dev first** — it reads from the
`dev` branch and creates the 6 dev child apps.

> The parent manifests must exist on the branch you apply them from. `app-of-apps-dev`
> is applied from a checkout of the **`dev` branch**; `app-of-apps-prod` from **`main`**.

```bash
# on the control-plane, in a checkout of the dev branch:
kubectl apply -f infra/k8s/argo/app-of-apps-dev.yaml
```

Watch the dev apps appear and settle:

```bash
kubectl -n argocd get applications
# or, prettier:
argocd app list
```

Expected result:

- `app-of-apps-dev` → `Synced` / `Healthy`.
- The 6 dev apps (`yolo-dev`, `agent-dev`, …) → `Synced` / `Healthy` automatically
  (auto-sync is on). They adopt the existing `dev`-namespace workloads in place.

> Applying the parent app is preferred over clicking **+ New App** in the UI —
> it's declarative, version-controlled, and recreatable.

## Step 6 — Promote to PROD (app-of-apps-prod)

When dev is proven and you're ready to release, promote by merging `dev` → `main`,
then bootstrap the prod parent from a checkout of **`main`**:

```bash
# on the control-plane, in a checkout of the main branch:
kubectl apply -f infra/k8s/argo/app-of-apps-prod.yaml
```

The 6 prod apps get **created** but are **manual-sync**, so they show `OutOfSync` and
deploy nothing until you sync them by hand — **this is the promotion gate**, not an error:

```bash
argocd app sync yolo-prod
argocd app sync agent-prod        # etc. per service
```

---

## Step 7 — Verify prerequisites on the cluster

- **`polyai-secrets` Secret** must exist in both `dev` and `prod` — every workload
  uses `envFrom.secretRef: polyai-secrets` (AWS creds, S3 bucket, …). It is
  intentionally **not** committed to git or included in the synced dirs. Confirm:
  ```bash
  kubectl -n dev  get secret polyai-secrets
  kubectl -n prod get secret polyai-secrets
  ```
  If missing, create it out-of-band (e.g. `kubectl -n <env> create secret generic
  polyai-secrets --from-env-file=<your.env>`).
- **EBS StorageClass / CSI driver** must be installed — Prometheus/Grafana PVCs need
  it. See `infra/k8s/ebs-storage-class.yaml`. Check: `kubectl get storageclass`.
- **Repo access:** this repo is public, so ArgoCD needs no credentials to read it.
  If it becomes private, add a repo credential (Settings → Repositories in the UI,
  or `argocd repo add`) or a deploy key.

---

## How deploys flow now (GitOps)

**Builds run on the `dev` branch only.** CI never touches the protected `main`
branch or the cluster.

**Dev (automatic):**
1. Push code to the `dev` branch under `services/<svc>/**`.
2. `.github/workflows/build-<svc>.yaml`:
   - builds & pushes `shahdra/<svc>-service:<sha>` to Docker Hub, then
   - `sed`s that tag into `infra/k8s/dev/<svc>/<svc>.yaml` and pushes the bump
     straight to `dev` (`[skip ci]`; a rebase-retry loop handles concurrent
     per-service pushes).
3. The `<svc>-dev` ArgoCD app (tracks `dev`, auto-sync) rolls it out immediately.

**Prod (promote by merge, then manual sync):**
1. Open a PR from `dev` → `main` and merge it (satisfies branch protection +
   the `test` check). The already-bumped `<sha>` tags ride along into
   `infra/k8s/prod/<svc>/<svc>.yaml`.
2. The `<svc>-prod` ArgoCD app (tracks `main`, **manual-sync**) shows `OutOfSync`.
3. Promote when ready: `argocd app sync <svc>-prod`.

**Frontend is a single image for both envs.** The agent URL is resolved at
runtime in the browser from `window.location` (agent NodePort = frontend
NodePort + 500; see `services/frontend/lib/api.ts`) — no per-env build arg, no
`-dev`/`-prod` tag split. It promotes by merge exactly like the others.

The old SSH/`docker compose` deploy jobs and `deploy-monitoring.yaml` have been
removed. Monitoring config lives inline in the Prometheus/Grafana manifest
ConfigMaps and syncs like any other manifest change (edit → commit → sync).

## Troubleshooting

- **App stuck `OutOfSync` on dev** — check the diff: `argocd app diff <app>` (or the
  UID **App Diff** tab). Usually a manifest field that drifted; commit the fix.
- **`Health: Degraded`** — a pod is crashing. `kubectl -n <env> describe pod <pod>`
  and `kubectl -n <env> logs <pod>`.
- **Can't reach the UI** — is the `port-forward` still running? It dies when its
  shell closes; use `tmux`/`screen`, or re-run it.
- **`ImagePullBackOff` after a sync** — the committed tag isn't on Docker Hub (build
  failed). Check the workflow run in the Actions tab.
