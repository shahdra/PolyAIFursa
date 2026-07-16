#!/usr/bin/env bash
#
# fix-frontend-ip.sh — re-point a k8s frontend/agent at the worker's CURRENT
# public IP after a node restart. Works for the dev or prod namespace.
#
#   ./infra/k8s/fix-frontend-ip.sh [dev|prod]     # default: dev
#
# Why this is needed: the worker has no Elastic IP, so its public IP changes on
# every stop/start. The frontend calls the agent directly from the browser, and
# that URL (NEXT_PUBLIC_AGENT_URL) is COMPILED INTO the frontend image at build
# time — a runtime env var cannot change it. So a new IP means:
#   1. rebuild + push the frontend image with the new agent NodePort URL, and
#   2. update the agent's ALLOWED_ORIGINS (CORS) to the new frontend origin.
#
# Run this ON THE CONTROL-PLANE after a worker restart.
# Requires: kubectl, aws CLI, docker (logged in as the image-repo owner).
set -euo pipefail

# --- args ------------------------------------------------------------------
NS="${1:-dev}"
case "${NS}" in
  dev)  FRONTEND_NODEPORT="30300"; AGENT_NODEPORT="30800" ;;
  prod) FRONTEND_NODEPORT="31300"; AGENT_NODEPORT="31800" ;;
  *) echo "ERROR: namespace must be 'dev' or 'prod' (got '${NS}')" >&2; exit 1 ;;
esac

# --- config ----------------------------------------------------------------
WORKER_INSTANCE_ID="i-0d10765e29b9bd911"     # shahd-worker
IMAGE_REPO="shahdra/frontend-service"
FRONTEND_CTX="services/frontend"             # docker build context (relative to repo root)
DOCKER="sudo docker"                         # this host needs root for the docker daemon
# ---------------------------------------------------------------------------

# Always operate from the repo root, regardless of where the script is invoked
# from (the script lives at <repo-root>/infra/k8s/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"
echo "==> Namespace: ${NS}  (frontend :${FRONTEND_NODEPORT}, agent :${AGENT_NODEPORT})"
echo "==> Working from repo root: ${REPO_ROOT}"

echo "==> Discovering the worker's current public IP (${WORKER_INSTANCE_ID})"
WORKER_IP="$(aws ec2 describe-instances \
  --instance-ids "${WORKER_INSTANCE_ID}" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text)"

if [[ -z "${WORKER_IP}" || "${WORKER_IP}" == "None" ]]; then
  echo "ERROR: could not determine the worker's public IP. Is the instance running?" >&2
  exit 1
fi

AGENT_URL="http://${WORKER_IP}:${AGENT_NODEPORT}"
FRONTEND_ORIGIN="http://${WORKER_IP}:${FRONTEND_NODEPORT}"
IMAGE_TAG="${NS}-${WORKER_IP//./-}"          # e.g. prod-44-201-87-27 (unique per ns+IP)
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"

echo "    worker IP       = ${WORKER_IP}"
echo "    agent URL       = ${AGENT_URL}   (baked into frontend)"
echo "    frontend origin = ${FRONTEND_ORIGIN}   (agent CORS)"
echo "    image           = ${IMAGE}"
echo

echo "==> Building frontend image with NEXT_PUBLIC_AGENT_URL=${AGENT_URL}"
${DOCKER} build \
  --build-arg NEXT_PUBLIC_AGENT_URL="${AGENT_URL}" \
  -t "${IMAGE}" \
  "${FRONTEND_CTX}"

# Sanity check: confirm the new IP is actually baked into the built bundle.
echo "==> Verifying the baked URL in the image bundle"
if ${DOCKER} run --rm "${IMAGE}" sh -c "grep -rq '${WORKER_IP}:${AGENT_NODEPORT}' .next" ; then
  echo "    OK: ${AGENT_URL} found in bundle"
else
  echo "ERROR: ${AGENT_URL} not found in the built bundle — build-arg did not take." >&2
  exit 1
fi

echo "==> Pushing ${IMAGE}"
${DOCKER} push "${IMAGE}"

echo "==> Updating agent ALLOWED_ORIGINS -> ${FRONTEND_ORIGIN}"
kubectl -n "${NS}" set env deploy/agent "ALLOWED_ORIGINS=${FRONTEND_ORIGIN}"

echo "==> Pointing frontend deployment at ${IMAGE}"
kubectl -n "${NS}" set image deploy/frontend "frontend=${IMAGE}"

echo "==> Waiting for rollouts"
kubectl -n "${NS}" rollout status deploy/agent --timeout=120s
kubectl -n "${NS}" rollout status deploy/frontend --timeout=180s

echo
echo "==> Done. Open the app at: ${FRONTEND_ORIGIN}"
echo "    (hard-refresh the browser to drop the old cached JS bundle)"
