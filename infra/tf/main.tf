# Task006 — Kubernetes cluster as code.
#
# ONE cluster serves both dev and prod (separated by Kubernetes namespaces), per
# the task note. The Terraform WORKSPACE therefore identifies the REGION, not the
# environment: `terraform workspace select us-east-1` + tfvars/us-east-1.tfvars
# deploys to us-east-1, and the identical config works for any other region with
# no code edits.

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.55"
    }
  }

  # Remote state in S3, with per-workspace keys (env:/<workspace>/tfstate.json).
  # NOTE: no variables or interpolation are allowed in a backend block — every
  # value must be hard-coded. The bucket itself was created out-of-band; a
  # bucket managed by the same state it stores is a chicken-and-egg problem.
  backend "s3" {
    bucket  = "shahdra-polyai-tfstate-228281126655"
    key     = "tfstate.json"
    region  = "us-east-1"
    encrypt = true
  }
}

provider "aws" {
  region = var.region

  # No `profile` here on purpose: GitHub Actions authenticates via
  # AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment variables, and a
  # hard-coded profile name would break CI. Locally, the default profile is
  # picked up automatically.

  default_tags {
    tags = {
      Owner       = var.owner
      Project     = "polyai"
      ManagedBy   = "terraform"
      Cluster     = local.cluster_name
      TFWorkspace = terraform.workspace
    }
  }
}

locals {
  # Every resource name derives from this, so switching workspace switches the
  # whole namespace of names. Critical in a SHARED course account: it guarantees
  # we never collide with another student's resources.
  cluster_name = "${var.owner}-polyai-${terraform.workspace}"
}

# Guard against the most likely operator error: applying a region's tfvars while
# a different region's workspace is selected. That would silently build a second
# cluster whose names claim the wrong region.
# terraform_data is built into Terraform >= 1.4 — no extra provider needed.
resource "terraform_data" "workspace_region_guard" {
  input = "${terraform.workspace}/${var.region}"

  lifecycle {
    precondition {
      condition     = terraform.workspace == var.region
      error_message = "Workspace '${terraform.workspace}' does not match region '${var.region}'. Run: terraform workspace select ${var.region}"
    }
  }
}

# AZs actually available in whatever region the provider points at — never
# hard-coded, so the config is portable across regions.
data "aws_availability_zones" "available" {
  state = "available"
}

# --- Networking -------------------------------------------------------------
# The community VPC module builds subnets, route tables, and the internet
# gateway from one block.
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "5.8.1"

  name = "${local.cluster_name}-vpc"
  cidr = var.vpc_cidr

  # slice(...,0,2) takes the first two AZs to satisfy the task's "2 public
  # subnets in different Availability Zones" requirement without naming them.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)

  # cidrsubnet() derives subnet ranges from var.vpc_cidr, so changing the VPC
  # CIDR does not require editing subnet literals.
  # 10.0.0.0/16 -> 10.0.101.0/24, 10.0.102.0/24
  public_subnets = [
    cidrsubnet(var.vpc_cidr, 8, 101),
    cidrsubnet(var.vpc_cidr, 8, 102),
  ]

  # No private subnets: the task says all instances go into the public subnets.
  # That also avoids a NAT gateway (~$32/mo) — nodes reach the internet directly
  # for image pulls via the internet gateway.
  private_subnets    = []
  enable_nat_gateway = false

  # Workers launched by the ASG need public IPs to be reachable on NodePorts and
  # to pull images.
  map_public_ip_on_launch = true

  # kubelet registers nodes by their private DNS name (ip-10-0-x-y.<region>
  # .compute.internal); both of these must be on for that to resolve.
  enable_dns_hostnames = true
  enable_dns_support   = true
}

# --- The cluster ------------------------------------------------------------
module "k8s_cluster" {
  source = "./modules/k8s-cluster"

  cluster_name = local.cluster_name
  region       = var.region

  vpc_id     = module.vpc.vpc_id
  vpc_cidr   = var.vpc_cidr
  subnet_ids = module.vpc.public_subnets

  control_plane_instance_type = var.control_plane_instance_type
  worker_instance_type        = var.worker_instance_type
  worker_desired_capacity     = var.worker_desired_capacity
  root_volume_size            = var.root_volume_size

  kubernetes_version = var.kubernetes_version
  pod_network_cidr   = var.pod_network_cidr

  ssh_key_name     = var.ssh_key_name
  ssh_ingress_cidr = var.ssh_ingress_cidr
}
