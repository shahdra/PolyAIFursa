# ingress — the public front door for the cluster.
#
#   internet
#      │  https://*.shahdra.fursa.click
#      ▼
#   Route 53 alias A-records ──► Application Load Balancer (:443, ACM cert)
#                                      │  plain HTTP to the node port
#                                      ▼
#                                Target Group (:30080, instance targets)
#                                      │  attached to the worker ASG
#                                      ▼
#                                ingress-nginx controller (NodePort 30080)
#                                      │  Host/path routing
#                                      ▼
#                                frontend / agent / grafana / prometheus / argocd
#
# ── Why the ALB terminates TLS and talks HTTP to the node ───────────────────
# The controller's HTTPS NodePort (30443) exists and is reachable, but pointing
# the target group at it would mean the ALB validating a certificate that
# ingress-nginx self-signs — so the health check fails unless the target group
# is set to ignore certificates anyway. Terminating at the ALB is the standard
# pattern: one ACM cert, renewed automatically, and the hop from ALB to node
# never leaves the VPC.
#
# ── Why instance targets and not IP targets ────────────────────────────────
# `target_type = "instance"` is what lets `aws_autoscaling_attachment` register
# workers automatically as the ASG scales. With IP targets we would have to
# discover pod IPs, which nothing in this stack does.

# The hosted zone is SHARED with every other student in this account. A data
# source reads it; we never declare it as a resource. That is the whole point:
# `terraform destroy` on this stack removes our records and leaves the zone.
data "aws_route53_zone" "base" {
  name         = "${var.base_domain}."
  private_zone = false
}

locals {
  # Domain root for this project, e.g. "shahdra.fursa.click".
  domain_root = "${var.subdomain}.${var.base_domain}"

  # "" -> the root itself; "dev" -> dev.shahdra.fursa.click
  fqdns = { for h in var.hosts : h => h == "" ? local.domain_root : "${h}.${local.domain_root}" }
}

# ---------------------------------------------------------------------------
# ACM certificate
# ---------------------------------------------------------------------------
# One cert covers the root and every first-level name under it. Note that the
# account's shared `*.fursa.click` wildcard does NOT cover
# `dev.shahdra.fursa.click` — a wildcard matches exactly one label — which is
# why this stack issues its own.
resource "aws_acm_certificate" "this" {
  domain_name               = local.domain_root
  subject_alternative_names = ["*.${local.domain_root}"]
  validation_method         = "DNS"

  tags = { Name = "${var.cluster_name}-alb" }

  # ACM will not let you delete a certificate that is still attached to a
  # listener, and any change to the domain names forces a new certificate.
  lifecycle {
    create_before_destroy = true
  }
}

# The CNAME records that prove we control the domain. for_each over
# domain_validation_options rather than [0] so adding a SAN needs no edit here.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.this.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id = data.aws_route53_zone.base.zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60

  # The root and the wildcard validate through the SAME record name. Without
  # allow_overwrite the second one collides with the first and the apply fails.
  allow_overwrite = true
}

# Blocks until ACM reports ISSUED. Without it, the listener below can be created
# with a still-PENDING_VALIDATION certificate and TLS fails at request time.
resource "aws_acm_certificate_validation" "this" {
  certificate_arn         = aws_acm_certificate.this.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name        = "${var.cluster_name}-alb"
  description = "Public ALB fronting the ingress-nginx controller"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.cluster_name}-alb" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from the internet"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  # Only ever answers with a 301 to https:// (see the :80 listener below), but
  # it has to be open for that redirect to happen at all — otherwise typing a
  # bare hostname in a browser times out instead of upgrading.
  description = "HTTP from the internet (redirected to HTTPS)"
  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 80
  to_port     = 80
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_nodes" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the ingress NodePort on the workers"
  referenced_security_group_id = var.worker_security_group_id
  from_port                    = var.ingress_http_node_port
  to_port                      = var.ingress_http_node_port
  ip_protocol                  = "tcp"
}

# The worker SG already allows the whole NodePort range from anywhere (kept for
# direct `http://<node-ip>:3xxxx` debugging), so this rule is redundant TODAY.
# It is here so that tightening that broad rule later does not silently break
# the ALB — the dependency is stated in code rather than remembered.
resource "aws_vpc_security_group_ingress_rule" "worker_from_alb" {
  security_group_id            = var.worker_security_group_id
  description                  = "ingress-nginx HTTP NodePort, from the ALB"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.ingress_http_node_port
  to_port                      = var.ingress_http_node_port
  ip_protocol                  = "tcp"
}

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------
resource "aws_lb" "this" {
  name               = substr("${var.cluster_name}-alb", 0, 32) # ALB names cap at 32 chars
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.subnet_ids

  # A lab cluster gets torn down often; deletion protection would just make
  # `terraform destroy` fail.
  enable_deletion_protection = false

  tags = { Name = "${var.cluster_name}-alb" }
}

resource "aws_lb_target_group" "ingress_nginx" {
  name     = substr("${var.cluster_name}-ing", 0, 32)
  port     = var.ingress_http_node_port
  protocol = "HTTP"
  vpc_id   = var.vpc_id

  # EC2 instances, registered automatically by the ASG attachment below.
  target_type = "instance"

  health_check {
    path     = "/healthz"
    protocol = "HTTP"
    port     = "traffic-port"
    # ingress-nginx answers /healthz with 200 on its main listener. The 404 in
    # the matcher is deliberate insurance: on a controller version where that
    # location is not defined, the default backend replies 404 — which still
    # proves the controller is alive and serving, and an UNHEALTHY target group
    # means a 503 for the whole site.
    matcher             = "200,404"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Deregistration is quick: these are stateless HTTP hops and a scale-down
  # should not hold the ASG for the default 300s.
  deregistration_delay = 30

  tags = { Name = "${var.cluster_name}-ingress-nginx" }

  # The ALB listener references this target group, so a replacement must be
  # created before the old one is destroyed.
  lifecycle {
    create_before_destroy = true
  }
}

# THIS is what makes the whole thing self-healing: every instance the ASG
# launches (including one the Cluster Autoscaler adds) is registered with the
# target group automatically, and deregistered on terminate.
resource "aws_autoscaling_attachment" "workers" {
  autoscaling_group_name = var.worker_asg_name
  lb_target_group_arn    = aws_lb_target_group.ingress_nginx.arn
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  # TLS 1.2+ only. The default policy still negotiates TLS 1.0/1.1.
  ssl_policy = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  # Reference the VALIDATION resource, not the certificate: it is the one that
  # only exists once ACM has issued.
  certificate_arn = aws_acm_certificate_validation.this.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ingress_nginx.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  # Never forward plaintext to the cluster; bounce it to 443 instead.
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ---------------------------------------------------------------------------
# Route 53
# ---------------------------------------------------------------------------
# ALIAS A-records, not CNAMEs: an ALB's IPs change, a CNAME cannot sit at a zone
# apex, and ALIAS queries are free.
resource "aws_route53_record" "hosts" {
  for_each = local.fqdns

  zone_id = data.aws_route53_zone.base.zone_id
  name    = each.value
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = false
  }
}
