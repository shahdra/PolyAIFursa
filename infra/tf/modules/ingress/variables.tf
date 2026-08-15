variable "cluster_name" {
  description = "Cluster identity; prefixes every resource name in this module."
  type        = string
}

variable "vpc_id" {
  description = "VPC the ALB and the worker nodes live in."
  type        = string
}

variable "subnet_ids" {
  description = <<-EOT
    Public subnet IDs for the ALB. An Application Load Balancer REQUIRES at
    least two subnets in different Availability Zones — the same two the worker
    ASG spans.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "An ALB needs at least two subnets in different AZs."
  }
}

variable "worker_asg_name" {
  description = "Worker Auto Scaling Group to attach the target group to."
  type        = string
}

variable "worker_security_group_id" {
  description = "Security group on the worker nodes; gets a rule allowing the ALB in on the ingress NodePort."
  type        = string
}

variable "ingress_http_node_port" {
  description = <<-EOT
    NodePort the ingress-nginx controller's HTTP port is PINNED to. The target
    group forwards here, so this value must match the `controller.service
    .nodePorts.http` value used when installing the chart (see
    infra/k8s/bootstrap.sh). Pinned rather than auto-allocated precisely because
    Terraform must know it before the controller exists.
  EOT
  type        = number
  default     = 30080
}

variable "base_domain" {
  description = <<-EOT
    Shared hosted zone, looked up with a DATA SOURCE and never managed here.
    Managing it would mean `terraform destroy` deletes the zone every other
    student also depends on.
  EOT
  type        = string
  default     = "fursa.click"
}

variable "subdomain" {
  description = <<-EOT
    Our slice of the shared zone. Everything this project exposes lives under
    <subdomain>.<base_domain>, e.g. shahdra.fursa.click and
    dev.shahdra.fursa.click.
  EOT
  type        = string
  default     = "shahdra"
}

variable "hosts" {
  description = <<-EOT
    Names to create Route 53 alias records for, relative to the domain root.
    "" (empty string) means the root itself. Each becomes an A-record ALIAS
    pointing at the ALB, which is what makes every Ingress host resolvable.
  EOT
  type        = list(string)
  default = [
    "",           # shahdra.fursa.click        -> prod frontend + agent
    "dev",        # dev.shahdra.fursa.click    -> dev frontend + agent
    "argocd",     # argocd.shahdra.fursa.click
    "grafana",    # grafana.shahdra.fursa.click
    "prometheus", # prometheus.shahdra.fursa.click
    "alertmanager",
  ]
}
