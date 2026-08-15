#!/usr/bin/env bash
#
# bootstrap.sh — turn a freshly `kubeadm init`-ed control plane into a working
# PolyAI cluster: CNI, storage, namespaces, secrets, the ingress controller, the
# kube-prometheus-stack, ArgoCD, and the ArgoCD Applications that own all
# workload deploys from then on.
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
#   DOMAIN_ROOT      public domain root (default: shahdra.fursa.click). Must
#                    match `terraform output -raw domain_root`.
#   ALERTS_SNS_TOPIC_ARN
#                    topic Alertmanager publishes to. Defaults to a value
#                    DERIVED from this node's Cluster tag and account, which is
#                    exactly what Terraform names it — so a manual run works
#                    without passing anything.
#   GRAFANA_ADMIN_PASSWORD
#                    Grafana admin password. Generated once and stored in a
#                    Secret if unset.
#   MONITORING_BASIC_AUTH_PASSWORD
#                    password guarding the Prometheus/Alertmanager Ingresses.

set -euo pipefail

# --- pinned versions -------------------------------------------------------
CALICO_VERSION="v3.28.0"
ARGOCD_VERSION="v2.13.2"
EBS_CSI_VERSION="release-1.31"
HELM_VERSION="v3.16.3"
INGRESS_NGINX_CHART_VERSION="4.11.3"      # controller v1.11.3
KUBE_PROM_STACK_CHART_VERSION="65.5.1"    # Prometheus 2.55, Grafana 11.3
CLUSTER_AUTOSCALER_CHART_VERSION="9.43.2" # cluster-autoscaler 1.31, matches k8s 1.31

# --- config ----------------------------------------------------------------
REPO_DIR="${REPO_DIR:-$HOME/PolyAIFursa}"
REPO_URL="${REPO_URL:-https://github.com/shahdra/PolyAIFursa.git}"
REPO_REF="${REPO_REF:-dev}"
DOMAIN_ROOT="${DOMAIN_ROOT:-shahdra.fursa.click}"

# The ingress controller's HTTP NodePort. MUST equal
# var.ingress_http_node_port in infra/tf — the ALB target group forwards there.
INGRESS_HTTP_NODE_PORT=30080
INGRESS_HTTPS_NODE_PORT=30443

export KUBECONFIG="${KUBECONFIG:-/etc/kubernetes/admin.conf}"
# admin.conf is root-owned mode 600; fall back to the ubuntu user's copy when
# this script runs unprivileged.
if [ ! -r "$KUBECONFIG" ] && [ -r "$HOME/.kube/config" ]; then
  export KUBECONFIG="$HOME/.kube/config"
fi

step() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# --- preflight -------------------------------------------------------------
step "0/12 Preflight"

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

# --- who and where are we? -------------------------------------------------
# Region, account and cluster name, discovered from the instance itself. Used to
# build the Alertmanager SNS topic ARN and to find the worker's public IP in the
# summary. Discovering rather than requiring them as inputs keeps a by-hand run
# of this script working with no arguments.
IMDS_TOKEN="$(curl -fsSL -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300" 2>/dev/null || true)"
imds() { curl -fsSL -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" "http://169.254.169.254/latest/meta-data/$1" 2>/dev/null || true; }

AWS_REGION="${AWS_REGION:-$(imds placement/region)}"
THIS_INSTANCE="$(imds instance-id)"

# The Cluster tag is set by Terraform on every node, and every Terraform
# resource name derives from it — which is what makes the SNS ARN derivable.
CLUSTER_TAG=""
if [ -n "$THIS_INSTANCE" ] && command -v aws >/dev/null 2>&1; then
  # shellcheck disable=SC2016  # backticks are JMESPath literals, not a subshell
  CLUSTER_TAG="$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$THIS_INSTANCE" \
    --query 'Reservations[0].Instances[0].Tags[?Key==`Cluster`].Value|[0]' \
    --output text 2>/dev/null || true)"
  [ "$CLUSTER_TAG" = "None" ] && CLUSTER_TAG=""
fi

if [ -z "${ALERTS_SNS_TOPIC_ARN:-}" ]; then
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
  if [ -n "$AWS_ACCOUNT_ID" ] && [ -n "$CLUSTER_TAG" ]; then
    # Matches `name = "${local.cluster_name}-alerts"` in infra/tf/alerts.tf.
    ALERTS_SNS_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${CLUSTER_TAG}-alerts"
  fi
fi
[ -n "${ALERTS_SNS_TOPIC_ARN:-}" ] || fail "could not determine ALERTS_SNS_TOPIC_ARN.
Pass it explicitly:  ALERTS_SNS_TOPIC_ARN=\$(terraform output -raw alerts_sns_topic_arn)
Without it Alertmanager has nowhere to publish and no alert email is ever sent."

echo "region=$AWS_REGION cluster=${CLUSTER_TAG:-unknown} domain=$DOMAIN_ROOT"
echo "alerts topic=$ALERTS_SNS_TOPIC_ARN"

# --- 1. Calico CNI ---------------------------------------------------------
step "1/12 Calico CNI ($CALICO_VERSION)"
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
step "2/12 Waiting for all nodes to become Ready"
# Everything below needs schedulable nodes. The operator takes a moment to create
# the calico-node DaemonSet, so give the condition a generous window.
kubectl wait --for=condition=Ready nodes --all --timeout=420s
kubectl get nodes -o wide

# --- 3. EBS CSI driver + StorageClass -------------------------------------
step "3/12 EBS CSI driver ($EBS_CSI_VERSION) + StorageClass"
# Prometheus and Grafana use PersistentVolumeClaims; without a provisioner those
# PVCs stay Pending and both pods never start. IAM (AmazonEBSCSIDriverPolicy) is
# already attached to the node roles by Terraform, so no secret is needed here.
kubectl apply -k "github.com/kubernetes-sigs/aws-ebs-csi-driver/deploy/kubernetes/overlays/stable/?ref=${EBS_CSI_VERSION}"
kubectl apply -f infra/k8s/ebs-storage-class.yaml

# --- 4. Namespaces --------------------------------------------------------
step "4/12 Namespaces (dev, prod, argocd, monitoring, ingress-nginx)"
# ONE cluster serves both environments, separated by namespace, per task006.
# monitoring and ingress-nginx are created here rather than left to Helm's
# --create-namespace so the Secrets below can be written before the charts that
# consume them are installed.
for ns in dev prod argocd monitoring ingress-nginx; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
done

# --- 5. polyai-secrets ----------------------------------------------------
step "5/12 polyai-secrets in dev and prod"
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

# --- 6. Helm ---------------------------------------------------------------
step "6/12 Helm ($HELM_VERSION)"
# Two of the three things below are Helm charts, so Helm has to exist. It is
# installed HERE rather than in the control-plane user-data on purpose: editing
# user-data forces the instance to be replaced (user_data_replace_on_change),
# and rebuilding a control plane to pick up a new CLI would be absurd.
if command -v helm >/dev/null 2>&1 && helm version --short 2>/dev/null | grep -q "${HELM_VERSION}"; then
  echo "  helm ${HELM_VERSION} already installed"
else
  # Pinned tarball, not the get-helm-3 convenience script: that script installs
  # whatever is newest today, which is the same "latest" trap as an unpinned
  # image tag.
  tmp="$(mktemp -d)"
  curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" | tar -xz -C "$tmp"
  sudo install -m 0755 "$tmp/linux-amd64/helm" /usr/local/bin/helm
  rm -rf "$tmp"
  echo "  installed $(helm version --short)"
fi

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx >/dev/null
helm repo add autoscaler https://kubernetes.github.io/autoscaler >/dev/null
helm repo update >/dev/null
echo "  chart repos updated"

# --- 7. kube-prometheus-stack ---------------------------------------------
step "7/12 kube-prometheus-stack ($KUBE_PROM_STACK_CHART_VERSION)"

# Grafana's admin password. Created ONCE and then left alone — regenerating it
# on every bootstrap would silently invalidate a password you had saved.
if kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  echo "  grafana-admin secret already exists (password unchanged)"
else
  GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)}"
  kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="$GRAFANA_ADMIN_PASSWORD" >/dev/null
  echo "  grafana-admin created -> admin / $GRAFANA_ADMIN_PASSWORD"
  echo "  (SAVE THIS. It is printed only on the run that creates it.)"
fi

# Basic-auth credentials for the Prometheus and Alertmanager Ingresses. Neither
# app has any authentication of its own and both are about to be published on
# the public internet — an open Alertmanager lets a stranger silence your alerts.
# The Secret must be in htpasswd format under the key `auth`.
if kubectl -n monitoring get secret monitoring-basic-auth >/dev/null 2>&1; then
  echo "  monitoring-basic-auth secret already exists"
else
  MONITORING_BASIC_AUTH_PASSWORD="${MONITORING_BASIC_AUTH_PASSWORD:-$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)}"
  kubectl -n monitoring create secret generic monitoring-basic-auth \
    --from-literal=auth="polyai:$(openssl passwd -apr1 "$MONITORING_BASIC_AUTH_PASSWORD")" >/dev/null
  echo "  monitoring-basic-auth created -> polyai / $MONITORING_BASIC_AUTH_PASSWORD"
  echo "  (SAVE THIS TOO.)"
fi

# Three values in the committed values file are only knowable after `terraform
# apply`, so they are placeholders that get substituted into a temp copy here.
# `sed` rather than envsubst: envsubst comes from gettext-base, which is not
# guaranteed on a minimal Ubuntu image, and the values file also contains Go
# template syntax we must not touch.
VALUES_RENDERED="$(mktemp)"
trap 'rm -f "$VALUES_RENDERED"' EXIT
sed -e "s|__ALERTS_SNS_TOPIC_ARN__|${ALERTS_SNS_TOPIC_ARN}|g" \
    -e "s|__AWS_REGION__|${AWS_REGION}|g" \
    -e "s|__DOMAIN_ROOT__|${DOMAIN_ROOT}|g" \
    infra/k8s/monitoring/values.yaml > "$VALUES_RENDERED"
grep -q '__' "$VALUES_RENDERED" && fail "unsubstituted placeholder left in the rendered values file"

# `upgrade --install` is the idempotent form: installs on first run, upgrades in
# place afterwards. --wait would block for the full timeout on every re-run, so
# it is left off and the readiness check happens below.
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --version "$KUBE_PROM_STACK_CHART_VERSION" \
  --values "$VALUES_RENDERED" \
  --timeout 10m

# The CRDs this chart installs (ServiceMonitor, PrometheusRule) are what the
# NEXT step and the ArgoCD monitoring apps depend on, so wait for them to be
# established before moving on — same discovery-cache race as Calico's.
for crd in servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com; do
  kubectl wait --for=condition=Established --timeout=120s "crd/$crd" >/dev/null 2>&1 \
    || fail "CRD $crd never became Established"
  echo "  $crd Established"
done

# --- 8. ingress-nginx ------------------------------------------------------
step "8/12 ingress-nginx ($INGRESS_NGINX_CHART_VERSION)"
# Installed AFTER kube-prometheus-stack because its values enable a
# ServiceMonitor, and that CRD has to exist first.
#
# The node ports are pinned on the command line as well as in the values file so
# that the coupling to Terraform is visible right here, at the install call.
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --version "$INGRESS_NGINX_CHART_VERSION" \
  --values infra/k8s/ingress-nginx/values.yaml \
  --set "controller.service.nodePorts.http=${INGRESS_HTTP_NODE_PORT}" \
  --set "controller.service.nodePorts.https=${INGRESS_HTTPS_NODE_PORT}" \
  --timeout 10m

kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=300s

# A wrong node port here means the ALB target group health-checks a port nothing
# listens on, the targets go unhealthy, and every hostname returns 503 with
# nothing in any pod log to explain it. Assert it instead.
ACTUAL_PORT="$(kubectl -n ingress-nginx get svc ingress-nginx-controller \
  -o jsonpath='{.spec.ports[?(@.name=="http")].nodePort}')"
[ "$ACTUAL_PORT" = "$INGRESS_HTTP_NODE_PORT" ] \
  || fail "ingress-nginx HTTP nodePort is $ACTUAL_PORT but the ALB target group expects $INGRESS_HTTP_NODE_PORT"
echo "  HTTP NodePort pinned at $ACTUAL_PORT (matches the ALB target group)"

# --- 8b. Cluster Autoscaler (task007 Part III, bonus) ----------------------
step "8b/12 Cluster Autoscaler ($CLUSTER_AUTOSCALER_CHART_VERSION)"
# Skipped rather than fatal when the cluster name is unknown: auto-discovery is
# tag-based and needs the exact name, and a misconfigured autoscaler that
# silently matches nothing is worse than not installing one.
if [ -z "$CLUSTER_TAG" ]; then
  echo "  SKIPPED - cluster name unknown, cannot configure ASG auto-discovery"
else
  CA_VALUES="$(mktemp)"
  sed -e "s|__CLUSTER_NAME__|${CLUSTER_TAG}|g" \
      -e "s|__AWS_REGION__|${AWS_REGION}|g" \
      infra/k8s/cluster-autoscaler/values.yaml > "$CA_VALUES"

  helm upgrade --install cluster-autoscaler autoscaler/cluster-autoscaler \
    --namespace kube-system \
    --version "$CLUSTER_AUTOSCALER_CHART_VERSION" \
    --values "$CA_VALUES" \
    --timeout 5m
  rm -f "$CA_VALUES"

  kubectl -n kube-system rollout status deploy/cluster-autoscaler-aws-cluster-autoscaler --timeout=180s
  echo "  watching ASG tagged k8s.io/cluster-autoscaler/${CLUSTER_TAG}=owned"
fi

# --- 9. ArgoCD ------------------------------------------------------------
step "9/12 ArgoCD ($ARGOCD_VERSION)"
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

# Serve the UI over plain HTTP so it can sit behind the TLS-terminating ALB.
# Without this, argocd-server answers every http:// request with a 307 to
# https://, the ALB terminates TLS and forwards http:// again, and the browser
# gives up on a redirect loop (ERR_TOO_MANY_REDIRECTS).
#
# `kubectl patch` on the ConfigMap rather than editing a copy of ArgoCD's
# install.yaml: we apply that manifest straight from upstream, so a local fork
# of it would have to be re-forked on every version bump.
kubectl -n argocd patch configmap argocd-cmd-params-cm \
  --type merge -p '{"data":{"server.insecure":"true"}}'
# argocd-server reads that ConfigMap only at startup.
kubectl -n argocd rollout restart deploy/argocd-server

step "9b/12 Waiting for ArgoCD to be ready"
# The application controller must be up before the app-of-apps parents are
# applied, or the child Applications sit unprocessed.
kubectl -n argocd rollout status deploy/argocd-server      --timeout=300s
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=300s
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=300s

# --- 7. ArgoCD Applications (app-of-apps) --------------------------------
step "10/12 ArgoCD Applications"
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

# --- 11. Platform Ingresses ------------------------------------------------
step "11/12 Platform Ingresses (argocd, grafana, prometheus, alertmanager)"
# These are applied HERE, not by ArgoCD, because they route to the things this
# script installs — including ArgoCD's own UI. Letting ArgoCD own the Ingress
# that fronts ArgoCD means the front door depends on the thing behind it.
#
# The dev/prod app Ingresses are the opposite case and ARE owned by ArgoCD:
# infra/k8s/{dev,prod}/ingress/, via the ingress-dev / ingress-prod child apps.
kubectl apply -f infra/k8s/platform/ingress.yaml

# --- 12. Summary ----------------------------------------------------------
step "12/12 Summary"

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

echo
echo "Public URLs (HTTPS, via the ALB -> ingress-nginx):"
echo "  prod frontend  https://${DOMAIN_ROOT}/            (after argocd app sync)"
echo "  prod agent     https://${DOMAIN_ROOT}/chat"
echo "  dev  frontend  https://dev.${DOMAIN_ROOT}/"
echo "  dev  agent     https://dev.${DOMAIN_ROOT}/chat"
echo "  argocd         https://argocd.${DOMAIN_ROOT}"
echo "  grafana        https://grafana.${DOMAIN_ROOT}"
echo "  prometheus     https://prometheus.${DOMAIN_ROOT}       (basic auth: polyai)"
echo "  alertmanager   https://alertmanager.${DOMAIN_ROOT}     (basic auth: polyai)"

# The worker's public IP is still worth printing: NodePort access bypasses the
# ALB entirely, which is how you tell a broken load balancer apart from a broken
# cluster when a hostname returns 503.
#
# We cannot read it from the Node object — kubelet only reports an ExternalIP
# when it runs with an AWS cloud provider, and this is a plain kubeadm cluster.
# Ask EC2 instead, scoped to OUR Cluster tag: `Role=worker` alone would be
# account-wide, and in this shared course account that could print another
# student's node.
WORKER_IP=""
if [ -n "$CLUSTER_TAG" ] && command -v aws >/dev/null 2>&1; then
  WORKER_IP="$(aws ec2 describe-instances --region "$AWS_REGION" \
    --filters "Name=tag:Cluster,Values=${CLUSTER_TAG}" \
              "Name=tag:Role,Values=worker" \
              "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].PublicIpAddress' \
    --output text 2>/dev/null | tr '\t' '\n' | grep -v '^None$' | head -n1 || true)"
fi

echo
if [ -n "$WORKER_IP" ]; then
  echo "Bypass the ALB for debugging (goes straight to the node port):"
  echo "  curl -H 'Host: dev.${DOMAIN_ROOT}' http://${WORKER_IP}:${INGRESS_HTTP_NODE_PORT}/"
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
echo
echo "If alerts never arrive by email, the SNS subscription is probably still"
echo "unconfirmed - check your inbox for the AWS confirmation link:"
echo "  aws sns list-subscriptions-by-topic --topic-arn ${ALERTS_SNS_TOPIC_ARN}"
