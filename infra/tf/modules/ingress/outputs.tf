output "alb_dns_name" {
  description = "The ALB's own DNS name. Useful for curl -H 'Host: ...' when debugging DNS."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Hosted zone ID of the ALB (the alias target)."
  value       = aws_lb.this.zone_id
}

output "alb_security_group_id" {
  description = "Security group attached to the ALB."
  value       = aws_security_group.alb.id
}

output "target_group_arn" {
  description = "Target group forwarding to the ingress-nginx NodePort."
  value       = aws_lb_target_group.ingress_nginx.arn
}

output "certificate_arn" {
  description = "ACM certificate on the HTTPS listener."
  value       = aws_acm_certificate_validation.this.certificate_arn
}

output "domain_root" {
  description = "Domain root for this project, e.g. shahdra.fursa.click."
  value       = local.domain_root
}

output "hostnames" {
  description = "Every FQDN this stack publishes, keyed by its short name."
  value       = local.fqdns
}

output "urls" {
  description = "Ready-to-open URLs for the exposed services."
  value = {
    prod_frontend = "https://${local.fqdns[""]}"
    prod_agent    = "https://${local.fqdns[""]}/chat"
    dev_frontend  = "https://${local.fqdns["dev"]}"
    dev_agent     = "https://${local.fqdns["dev"]}/chat"
    argocd        = "https://${local.fqdns["argocd"]}"
    grafana       = "https://${local.fqdns["grafana"]}"
    prometheus    = "https://${local.fqdns["prometheus"]}"
    alertmanager  = "https://${local.fqdns["alertmanager"]}"
  }
}
