#!/usr/bin/env bash
#
# bootstrap.sh — turn a freshly `kubeadm init`-ed control plane into a working
# PolyAI cluster: CNI, storage, namespaces, secrets, ArgoCD, and the ArgoCD
# Applications that own all workload deploys from then on.
#
# Runs ON the control-plane node. Either pipe it in over SSH:
#
#   ssh ubuntu@<control-plane-ip> \
#     "POLYAI_ENV_FILE=/tmp/polyai.env REPO_DIR=~/PolyAIFursa bash -s" < infra/k8s/bootstrap.sh
#
# ...or run it from a checkout on the node:
#
#   POLYAI_ENV_FILE=/tmp/polyai.env ./infra/k8s/bootstrap.sh
#
# IDEMPOTENT BY DESIGN. The cluster.yaml workflow re-runs this on every dispatch,
# usually against an already-bootstrapped cluster, so every step must be safe to
# repeat:
#   * `kubectl apply`, never `kubectl create` (which fails with AlreadyExists and,
#     under `set -e`, would fail the whole job on run 2)
#   * namespaces and secrets go through `create --dry-run=client | kubectl apply`,
#     the declarative equivalent - this also UPDATES the secret if the env file
#     changed
#   * every remote URL is version-pinned; "stable"/"latest" in a bootstrap script
#     is a future outage with no code change to blame it on
#   * `kubectl wait` between phases rather than `sleep` - a fixed sleep is either
#     too short (flaky) or too long (slow)
#
# Required environment:
#   POLYAI_ENV_FILE  path to an env file (KEY=VALUE lines) holding MODEL,
#                    AWS_REGION, AWS_S3_BUCKET, AWS_ACCESS_KEY_ID,
#                    AWS_SECRET_ACCESS_KEY, GOOGLE_API_KEY. Becomes the
#                    `polyai-secrets` Secret in both dev and prod. Every workload
#                    consumes it via envFrom, so a missing one means six pods in
#                    CreateContainerConfigError.
# Optional environment:
#   REPO_DIR         repo checkout on this node (default: $HOME/PolyAIFursa).
#                    Needed for the storage class and the app-of-apps manifests.
#   REPO_URL         clone source if REPO_DIR is absent.
#   REPO_REF         branch to check out (default: dev).

set -euo pipefail

# --- pinned versions -------------------------------------------------------
CALICO_VERSION="v3.28.0"
ARGOCD_VERSION="v2.13.2"
EBS_CSI_VERSION="release-1.31"

# --- config ----------------------------------------------------------------
REPO_DIR="${REPO_DIR:-$HOME/PolyAIFursa}"
REPO_URL="${REPO_URL:-https://github.com/shahdra/PolyAIFursa.git}"
REPO_REF="${REPO_REF:-dev}"

export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
# admin.conf is root-owned mode 600; fall back to the ubuntu user's copy when
# this script runs unprivileged.
if [ ! -r "$KUBECONFIG" ] && [ -r "$HOME/.kube/config" ]; then
  export KUBECONFIG="$HOME/.kube/config"
fi

step() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
step "0/8 Preflight"

command -v kubectl >/dev/null || fail "kubectl not found. Did control-plane user-data finish?"

[ -n "${POLYAI_ENV_FILE:-}" ] || fail "POLYAI_ENV_FILE is required.
It must point at an env file with MODEL, AWS_*, GOOGLE_API_KEY. Without it the
six app pods cannot start. Locally that file is services/agent/.env; in CI it is
written from the POLYAI_ENV GitHub secret."
[ -f "$POLYAI_ENV_FILE" ] || fail "POLYAI_ENV_FILE=$POLYAI_ENV_FILE does not exist."
[ -s "$POLYAI_ENV_FILE" ] || fail "POLYAI_ENV_FILE=$POLYAI_ENV_FILE is empty."

kubectl cluster-info >/dev/null 2>&1 || fail "cannot reach the API server with KUBECONFIG=$KUBECONFIG"
echo "API server reachable; using KUBECONFIG=$KUBECONFIG"

# The storage class and app-of-apps manifests live in the repo, so we need a
# checkout on this node.
if [ -d "$REPO_DIR/.git" ]; then
  echo "Updating existing checkout at $REPO_DIR"
  git -C "$REPO_DIR" fetch --quiet origin "$REPO_REF"
  git -C "$REPO_DIR" checkout --quiet "$REPO_REF"
  git -C "$REPO_DIR" reset --hard --quiet "origin/$REPO_REF"
else
  echo "Cloning $REPO_URL ($REPO_REF) into $REPO_DIR"
  git clone --quiet --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"

# --- 1. Calico CNI ---------------------------------------------------------
step "1/8 Calico CNI ($CALICO_VERSION)"
# Nodes stay NotReady until a CNI is installed - this is the step that makes the
# cluster schedulable. server-side apply because the tigera-operator manifest
# contains CRDs whose annotations exceed the client-side apply size limit.
kubectl apply --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/tigera-operator.yaml"

# Wait for the CRDs to register in the API server's discovery cache before
# applying any custom resource that uses them. Without this the very next apply
# races the cache and fails with:
#   no matches for kind "Installation" in version "operator.tigera.io/v1"
#   ensure CRDs are installed first
# `kubectl apply` does not wait for CRD establishment, and the failure is timing
# dependent - it can pass by hand and fail in CI, or vice versa.
#
# Each CRD gets its OWN bounded retry loop rather than one multi-arg `kubectl
# wait`, because of a subtle failure that killed a real run:
#
#   error: .status.conditions accessor error: <nil> is of the type <nil>,
#          expected []interface{}
#
# `--server-side` apply returns as soon as the object is PERSISTED, but the API
# server populates `.status` a moment later. In that window `.status.conditions`
# is absent (nil), not an empty list - and `kubectl wait` cannot traverse a nil
# field, so it EXITS NON-ZERO IMMEDIATELY instead of waiting. `--timeout` never
# gets a chance to help, and `set -e` then kills the whole bootstrap.
#
# Two consequences drive the shape below:
#   1. The nil-status error must be treated as "not ready yet", not as fatal -
#      hence `|| true` semantics via the if/break, and 2>/dev/null to keep the
#      transient accessor error out of the log.
#   2. One CRD per `kubectl wait` call. With three names in a single call, a nil
#      status on ANY of them fails the whole command even when the others are
#      already Established - which is exactly what happened: apiservers and
#      tigerastatuses reported "condition met" while installations errored.
#
# 24 attempts x 5s = up to 2 minutes per CRD. Establishment normally takes a
# second or two; the generous bound only matters on a cold, loaded API server.
echo "Waiting for Calico CRDs to be established..."
for crd in \
  installations.operator.tigera.io \
  apiservers.operator.tigera.io \
  tigerastatuses.operator.tigera.io
do
  established=0
  for attempt in $(seq 1 24); do
    if kubectl wait --for=condition=Established --timeout=10s "crd/$crd" 2>/dev/null; then
      established=1
      break
    fi
    # Distinguish "not created yet" from "created but status not populated" so a
    # genuinely missing CRD is obvious in the log rather than looking like lag.
    if kubectl get "crd/$crd" >/dev/null 2>&1; then
      echo "  $crd exists but .status is not populated yet (attempt $attempt/24)"
    else
      echo "  $crd not registered yet (attempt $attempt/24)"
    fi
    sleep 5
  done
  [ "$established" -eq 1 ] || fail "CRD $crd never became Established.
The tigera-operator manifest applied but this CRD did not converge. Check:
  kubectl get crd | grep tigera
  kubectl -n tigera-operator get pods
  kubectl -n tigera-operator logs deploy/tigera-operator"
  echo "  $crd Established"
done

# The Installation CR is written inline rather than fetched from upstream
# custom-resources.yaml, for one important reason: `encapsulation`.
#
# Upstream defaults to VXLANCrossSubnet, which encapsulates pod traffic ONLY when
# the two nodes are in different subnets and sends RAW pod-IP packets when they
# share one. On AWS that raw path is silently dropped:
#   * the VPC route table has no route for 192.168.0.0/16, and
#   * the ENI source/destination check rejects packets whose source is a pod IP.
#
# Our ASG spans two subnets, so whether any given pair of nodes shares one is
# luck. When they do, cross-node pod traffic dies - which shows up as DNS
# timeouts ("lookup argocd-redis: i/o timeout") and CrashLoopBackOff for anything
# that resolves a Service from a different node than CoreDNS.
#
# `encapsulation: VXLAN` always tunnels, so the underlay only ever sees node-IP
# UDP traffic that AWS is happy to route. The cost is ~50 bytes of overhead per
# packet, which is the right trade for correctness.
#
# ipPool cidr MUST match `kubeadm init --pod-network-cidr` (192.168.0.0/16 - set
# in the Terraform module's pod_network_cidr variable). Change one, change both.
#
# Retry loop: even after the CRDs report Established, the aggregated discovery
# cache in front of the API server can lag a few seconds. Three attempts at 10s
# covers it without masking a real error (the last failure still surfaces).
for attempt in 1 2 3; do
  if kubectl apply -f - <<'CALICO_INSTALLATION'; then
apiVersion: operator.tigera.io/v1
kind: Installation
metadata:
  name: default
spec:
  calicoNetwork:
    ipPools:
      - name: default-ipv4-ippool
        blockSize: 26
        cidr: 192.168.0.0/16
        encapsulation: VXLAN
        natOutgoing: Enabled
        nodeSelector: all()
---
apiVersion: operator.tigera.io/v1
kind: APIServer
metadata:
  name: default
spec: {}
CALICO_INSTALLATION
    break
  fi
  [ "$attempt" -eq 3 ] && fail "could not apply the Calico Installation after 3 attempts"
  echo "  discovery cache still catching up; retrying in 10s (attempt $attempt/3)"
  sleep 10
done

# --- 2. Wait for nodes Ready ----------------------------------------------
step "2/8 Waiting for all nodes to become Ready"
# Everything below needs schedulable nodes. The operator takes a moment to create
# the calico-node DaemonSet, so give the condition a generous window.
kubectl wait --for=condition=Ready nodes --all --timeout=420s
kubectl get nodes -o wide

# --- 3. EBS CSI driver + StorageClass -------------------------------------
step "3/8 EBS CSI driver ($EBS_CSI_VERSION) + StorageClass"
# Prometheus and Grafana use PersistentVolumeClaims; without a provisioner those
# PVCs stay Pending and both pods never start. IAM (AmazonEBSCSIDriverPolicy) is
# already attached to the node roles by Terraform, so no secret is needed here.
kubectl apply -k "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=${EBS_CSI_VERSION}"
kubectl apply -f infra/k8s/ebs-storage-class.yaml

# --- 4. Namespaces --------------------------------------------------------
step "4/8 Namespaces (dev, prod, argocd)"
# ONE cluster serves both environments, separated by namespace, per task006.
for ns in dev prod argocd; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

# --- 5. polyai-secrets ----------------------------------------------------
step "5/8 polyai-secrets in dev and prod"
# Deliberately NOT in git. create --dry-run | apply makes this both idempotent
# and updating: change the env file, re-run, and the Secret is patched.
# Output is suppressed because kubectl echoes the resource, and on some versions
# a diff can surface data keys.
for ns in dev prod; do
  kubectl -n "$ns" create secret generic polyai-secrets \
    --from-env-file="$POLYAI_ENV_FILE" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "  polyai-secrets applied in $ns ($(grep -c '=' "$POLYAI_ENV_FILE") keys)"
done

# --- 6. ArgoCD ------------------------------------------------------------
step "6/8 ArgoCD ($ARGOCD_VERSION)"
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

step "6b/8 Waiting for ArgoCD to be ready"
# The application controller must be up before the app-of-apps parents are
# applied, or the child Applications sit unprocessed.
kubectl -n argocd rollout status deploy/argocd-server      --timeout=300s
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=300s
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=300s

# --- 7. ArgoCD Applications (app-of-apps) --------------------------------
step "7/8 ArgoCD Applications"
# Two parents, one per environment. Each parent's targetRevision pins which
# branch its children track (dev parent -> dev branch, prod parent -> main), so
# applying both from a single checkout is correct: the parent manifests
# themselves are branch-independent.
#
# dev children  = auto-sync (prune + selfHeal) -> deploy on every push to dev
# prod children = manual sync                  -> promotion gate; they will show
#                                                 OutOfSync until you run
#                                                 `argocd app sync <svc>-prod`.
#                                                 That is the gate, not an error.
kubectl apply -f infra/k8s/argo/app-of-apps-dev.yaml
kubectl apply -f infra/k8s/argo/app-of-apps-prod.yaml

# --- 8. Summary ----------------------------------------------------------
step "8/8 Summary"

echo "ArgoCD admin password:"
# Once you rotate the password and delete this Secret, the get returns non-zero.
# Guard it so `set -e` doesn't fail an otherwise-successful bootstrap.
if kubectl -n argocd get secret argocd-initial-admin-secret >/dev/null 2>&1; then
  kubectl -n argocd get secret argocd-initial-admin-secret \
    -o jsonpath='{.data.password}' | base64 -d
  echo
else
  echo "  (initial secret is gone - the password was already changed)"
fi

# Resolve the worker's PUBLIC ip.
#
# Note: we cannot read this from the Node object. kubelet only reports an
# ExternalIP address when it runs with an AWS cloud provider; this is a plain
# kubeadm cluster, so Node.status.addresses contains InternalIP and Hostname
# only. Ask EC2 instead, matching on the ASG's instance Name tag.
WORKER_IP=""
if command -v aws >/dev/null 2>&1; then
  # Derive the cluster name from this node's own Cluster tag so the lookup below
  # is scoped to OUR cluster. `Role=worker` alone would be account-wide, and this
  # is a shared course account - another student using the same generic tag would
  # have us print their node's IP.
  IMDS_TOKEN="$(curl -fsSL -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null || true)"
  THIS_INSTANCE="$(curl -fsSL -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
    "http://169.254.169.254/latest/meta-data/instance-id" 2>/dev/null || true)"

  CLUSTER_TAG=""
  if [ -n "$THIS_INSTANCE" ]; then
    # shellcheck disable=SC2016  # backticks are JMESPath literals for --query,
    # not command substitution; single quotes are required here.
    CLUSTER_TAG="$(aws ec2 describe-instances --instance-ids "$THIS_INSTANCE" \
      --query 'Reservations[0].Instances[0].Tags[?Key==`Cluster`].Value|[0]' \
      --output text 2>/dev/null || true)"
    [ "$CLUSTER_TAG" = "None" ] && CLUSTER_TAG=""
  fi

  if [ -n "$CLUSTER_TAG" ]; then
    WORKER_IP="$(aws ec2 describe-instances \
      --filters "Name=tag:Cluster,Values=${CLUSTER_TAG}" \
                "Name=tag:Role,Values=worker" \
                "Name=instance-state-name,Values=running" \
      --query 'Reservations[].Instances[].PublicIpAddress' \
      --output text 2>/dev/null | tr '\t' '\n' | grep -v '^None$' | head -n1 || true)"
  fi
fi

echo
if [ -n "$WORKER_IP" ]; then
  echo "App URLs (NodePort on the worker's public IP):"
  echo "  dev  frontend  http://${WORKER_IP}:30300"
  echo "  dev  grafana   http://${WORKER_IP}:30301"
  echo "  prod frontend  http://${WORKER_IP}:31300   (after argocd app sync)"
  echo "  prod grafana   http://${WORKER_IP}:31301"
else
  echo "No worker with a public IP found yet."
  echo "  If the ASG desired capacity is 0, scale it up; if a worker was just"
  echo "  launched, it needs ~6-8 min to install cri-o and join."
fi

echo
echo "Applications:"
kubectl -n argocd get applications 2>/dev/null || echo "  (none yet - the controller may still be reconciling)"

step "Bootstrap complete"
echo "Reminder: prod apps are manual-sync by design. Promote with:"
echo "  argocd app sync <service>-prod"
