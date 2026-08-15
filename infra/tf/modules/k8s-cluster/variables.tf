# Inputs for the k8s-cluster module.
#
# Deliberately no defaults on the identity/networking inputs: a missing value
# should be a loud error at plan time, not a silent deploy into the wrong VPC.

variable "cluster_name" {
  description = "Cluster identity. Prefixes every resource name and the SSM parameter paths."
  type        = string
}

variable "region" {
  description = "AWS region. Passed explicitly into user-data so the in-instance AWS CLI targets the right endpoint."
  type        = string
}

variable "vpc_id" {
  description = "VPC the cluster lives in."
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR. Used for the 'allow all intra-VPC traffic' security group rules."
  type        = string
}

variable "subnet_ids" {
  description = "Public subnet IDs (>= 2, in different AZs). Control plane lands in the first; the ASG spans all."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "At least 2 subnets in different AZs are required."
  }
}

variable "control_plane_instance_type" {
  description = "EC2 instance type for the control plane."
  type        = string
}

variable "worker_instance_type" {
  description = "EC2 instance type for worker nodes."
  type        = string
}

variable "worker_desired_capacity" {
  description = "Desired worker count. 0 parks the cluster with no workers."
  type        = number
}

variable "root_volume_size" {
  description = "Root EBS volume size in GiB."
  type        = number
}

variable "kubernetes_version" {
  description = "Kubernetes minor version series for the pkgs.k8s.io apt repo, e.g. v1.31."
  type        = string
}

variable "pod_network_cidr" {
  description = "Pod network CIDR for kubeadm init. Must match Calico's ipPool."
  type        = string
}

variable "ssh_key_name" {
  description = "Pre-existing EC2 key pair name for SSH access."
  type        = string
}

variable "ssh_ingress_cidr" {
  description = "CIDR allowed to reach SSH (22) and the Kubernetes API (6443)."
  type        = string
}

variable "worker_min_size" {
  description = "ASG minimum size."
  type        = number
  default     = 1 # required by task006
}

variable "worker_max_size" {
  description = "ASG maximum size."
  type        = number
  default     = 3 # required by task006
}

variable "alerts_sns_topic_arn" {
  description = <<-EOT
    SNS topic Alertmanager publishes to. The worker role gets sns:Publish on it
    (and nothing else), so the Alertmanager pod can authenticate through the
    node's instance profile instead of a static access key in a Secret.
  EOT
  type        = string
}
