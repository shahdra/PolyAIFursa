# Alerting fan-out: Alertmanager -> SNS -> email.
#
# Alertmanager has a native `sns_config` receiver, so no bridge service is
# needed — it signs a Publish call with SigV4 and SNS does the delivery. The
# topic and the email subscription are provisioned here, as task007 requires.
#
# ── How Alertmanager authenticates ─────────────────────────────────────────
# It does NOT hold an access key. The Alertmanager pod runs on a worker node,
# and the AWS SDK inside it falls back to the EC2 instance metadata service,
# picking up the WORKER instance role's temporary credentials. That role is
# granted `sns:Publish` on this topic only (see modules/k8s-cluster/main.tf).
# The launch template already sets http_put_response_hop_limit = 2, which is
# what lets a container — one network hop further out than the host — reach
# IMDSv2 at all.
#
# ── The email subscription needs ONE manual click ──────────────────────────
# AWS sends a confirmation link to the address the first time this is applied,
# and SNS will not deliver anything until it is clicked. Terraform reports the
# subscription as created either way, so `pending_confirmation` in the console
# is the thing to check when alerts fire but no mail arrives. Confirmation
# survives `terraform destroy` only if the topic ARN is unchanged — and it is,
# because the name derives from the deterministic cluster_name.

resource "aws_sns_topic" "alerts" {
  name         = "${local.cluster_name}-alerts"
  display_name = "PolyAI alerts" # becomes the email's From name; SNS caps it at 10 chars for SMS

  tags = { Name = "${local.cluster_name}-alerts" }
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email

  # `email` subscriptions cannot be managed after creation the way HTTP ones
  # can: AWS returns the ARN as the literal string "pending confirmation" until
  # the link is clicked, which Terraform would otherwise try to reconcile on
  # every plan.
  lifecycle {
    ignore_changes = [confirmation_timeout_in_minutes]
  }
}
