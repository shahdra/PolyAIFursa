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

## Cluster reference (this deployment)

| Node                | Instance              | Private IP     | Security group                       |
|---------------------|-----------------------|----------------|--------------------------------------|
| control-plane       | `i-0fe97cd46f415418b` | `10.0.1.195`   | `sg-0f6488261f10197b1` (control-sg)  |
| worker              | `i-0d10765e29b9bd911` | `10.0.1.164`   | `sg-0a960d8dcd201bd93` (worker-sg)   |

Ports already open (no SG changes needed for this runbook):

| Port          | Where (SG)   | Purpose                                             |
|---------------|--------------|-----------------------------------------------------|
| `22`          | both         | SSH                                                 |
| `6443`        | control      | Kubernetes API server                               |
| `8080`        | control      | ArgoCD UI via `kubectl port-forward` (see step 3)   |
| `30000-32767` | worker       | NodePorts — how you reach frontend/agent/grafana    |

> All `kubectl`/`argocd` commands below run **on the control-plane node**. SSH in first:
> ```bash
> ssh -i <your-key.pem> ubuntu@<control-plane-public-ip>
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

### Option B (quick, uses the already-open port 8080) — direct

The control-plane SG already allows `8080` from `0.0.0.0/0`, so you can bind the
port-forward to all interfaces and hit the node's public IP directly.

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443 --address 0.0.0.0
```

Browse to **`https://<control-plane-public-ip>:8080`** → accept the cert → log in.

> ⚠️ Option B exposes the UI to the whole internet (it's only password-protected).
> For anything beyond a quick check, prefer Option A, or narrow the `8080` inbound
> rule from `0.0.0.0/0` to your own IP (EC2 → Security Groups → `sg-0f6488261f10197b1`
> → edit the port-8080 rule → Source = "My IP").

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

1. You push code to `dev` (or `main`) under `services/<svc>/**`.
2. The matching `.github/workflows/build-<svc>.yaml` workflow:
   - builds & pushes `shahdra/<svc>-service:<sha>` to Docker Hub, then
   - `sed`s that tag into `infra/k8s/<env>/<svc>/<svc>.yaml` and commits it back
     with `[skip ci]` (a `concurrency` group serializes these commits so parallel
     service builds don't clash on `git push`).
3. ArgoCD sees the new commit:
   - **dev** → auto-syncs and rolls out immediately.
   - **prod** → shows `OutOfSync`; you sync it manually when ready.

The old SSH/`docker compose` deploy jobs and `deploy-monitoring.yaml` have been
removed — **CI never touches the cluster directly anymore**. Monitoring config
lives inline in the Prometheus/Grafana manifest ConfigMaps and syncs like any
other manifest change (edit the manifest → commit → ArgoCD applies it).

## Troubleshooting

- **App stuck `OutOfSync` on dev** — check the diff: `argocd app diff <app>` (or the
  UID **App Diff** tab). Usually a manifest field that drifted; commit the fix.
- **`Health: Degraded`** — a pod is crashing. `kubectl -n <env> describe pod <pod>`
  and `kubectl -n <env> logs <pod>`.
- **Can't reach the UI** — is the `port-forward` still running? It dies when its
  shell closes; use `tmux`/`screen`, or re-run it.
- **`ImagePullBackOff` after a sync** — the committed tag isn't on Docker Hub (build
  failed). Check the workflow run in the Actions tab.
