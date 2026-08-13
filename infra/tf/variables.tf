# Root input variables.
#
# Only `region` is required — it comes from tfvars/<region>.tfvars. Everything
# else has a sensible default so a bare `terraform apply -var-file=...` works.
#
# Note what is NOT here: there is no `env` variable. Task006 provisions ONE
# cluster serving both dev and prod, separated by Kubernetes namespaces, so the
# workspace identifies the REGION only (see locals.cluster_name in main.tf).
# There is also no `ami_id` — hard-coding an AMI would pin us to one region and
# break the "works in any region" requirement; the AMI is looked up per-region
# by a data source inside the k8s-cluster module.

variable "region" {
  description = "AWS region to deploy into. Must match the workspace name."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the cluster VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "control_plane_instance_type" {
  description = "EC2 instance type for the Kubernetes control plane."
  type        = string
  default     = "t3.medium" # required by task006
}

variable "worker_instance_type" {
  description = "EC2 instance type for the ASG-managed worker nodes."
  type        = string
  default     = "t3.medium"
}

variable "worker_desired_capacity" {
  description = <<-EOT
    Desired number of worker nodes. Set to 0 when you finish working to avoid
    unnecessary cost; set to >0 (e.g. 1) while working on the cluster.
    NOTE: min_size is 1, so a desired of 0 is honoured by the ASG but the next
    `terraform apply` will raise it back to min_size unless you also pass this
    variable as 0.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.worker_desired_capacity >= 0 && var.worker_desired_capacity <= 3
    error_message = "worker_desired_capacity must be between 0 and 3 (the ASG max_size)."
  }
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB for all cluster nodes."
  type        = number
  default     = 20 # required by task006
}

variable "kubernetes_version" {
  description = <<-EOT
    Kubernetes minor version series used for the pkgs.k8s.io apt repo, e.g.
    "v1.31". Pinned deliberately: an unpinned repo would let a rebuilt node join
    with a different kubelet version than the control plane.
  EOT
  type        = string
  default     = "v1.31"
}

variable "pod_network_cidr" {
  description = <<-EOT
    Pod network CIDR passed to `kubeadm init`. MUST match the ipPool CIDR in
    Calico's custom-resources.yaml (default 192.168.0.0/16) — if you change one
    without the other, nodes install the CNI but never become Ready.
  EOT
  type        = string
  default     = "192.168.0.0/16"
}

variable "ssh_key_name" {
  description = <<-EOT
    Name of a PRE-EXISTING EC2 key pair used for SSH to all nodes (e.g.
    "shahd-key"). Deliberately not created by Terraform: Terraform cannot hand
    you a private key without writing it into state, and a .pem sitting in the
    S3 state bucket is worse than a key you created by hand.
  EOT
  type        = string
}

variable "ssh_ingress_cidr" {
  description = <<-EOT
    CIDR allowed to reach SSH (22) and the Kubernetes API (6443). Defaults to
    open because the CI bootstrap job runs on GitHub-hosted runners with no
    stable egress IP. Narrow this to your own /32 when working locally.
  EOT
  type        = string
  default     = "0.0.0.0/0"
}

variable "ssh_private_key_path" {
  description = <<-EOT
    Local path to the private key matching ssh_key_name. Affects ONLY the
    convenience strings in ssh_command / fetch_kubeconfig_command outputs — no
    AWS resource reads it, and Terraform never reads the file itself.
  EOT
  type        = string
  default     = "~/.ssh/shahd-key.pem"
}

variable "owner" {
  description = "Owner tag applied to every resource. Namespaces our resources in the shared course account."
  type        = string
  default     = "shahdra"
}

# --- task007: public ingress ------------------------------------------------

variable "base_domain" {
  description = <<-EOT
    Shared Route 53 hosted zone. Looked up with a data source and NEVER managed
    by this stack — every student in the account shares it, so a `terraform
    destroy` here must not be able to delete it.
  EOT
  type        = string
  default     = "fursa.click"
}

variable "subdomain" {
  description = <<-EOT
    Our slice of the shared zone. Everything is exposed under
    <subdomain>.<base_domain>: shahdra.fursa.click (prod),
    dev.shahdra.fursa.click, grafana.shahdra.fursa.click, and so on.
  EOT
  type        = string
  default     = "shahdra"
}

variable "ingress_http_node_port" {
  description = <<-EOT
    NodePort the ingress-nginx controller's HTTP port is pinned to. The ALB
    target group forwards here, so Terraform must know the port BEFORE the
    controller exists — which is why it is pinned instead of auto-allocated.
    Changing it means changing `controller.service.nodePorts.http` in
    infra/k8s/bootstrap.sh in the same commit.
  EOT
  type        = number
  default     = 30080
}

variable "alert_email" {
  description = <<-EOT
    Mailbox subscribed to the alerts SNS topic. AWS emails a confirmation link
    on first apply; until it is clicked, SNS silently drops every alert.
  EOT
  type        = string
  default     = "rayanshahd55@gmail.com"
}
