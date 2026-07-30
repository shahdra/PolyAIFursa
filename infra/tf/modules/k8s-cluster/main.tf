# k8s-cluster — a kubeadm-provisioned Kubernetes cluster on EC2.
#
# Layout:
#   * one control-plane EC2 instance, initialised by user-data on first boot
#   * worker nodes managed by an Auto Scaling Group via a launch template
#   * SSM Parameter Store carries the `kubeadm join` command from the control
#     plane to workers (see "How workers join" below)
#
# ── How workers join the cluster ────────────────────────────────────────────
# The ASG launches workers at unpredictable times — possibly minutes after the
# control plane, possibly days later on a scale-out — so the join command cannot
# be baked into the launch template at apply time (it does not exist yet, and
# kubeadm tokens expire).
#
# Chosen solution: SSM Parameter Store as a one-way channel.
#   1. Control-plane user-data runs `kubeadm init`, then
#      `kubeadm token create --print-join-command`, and writes the result to a
#      SecureString parameter at /polyai/<cluster>/join-command.
#   2. Worker user-data polls that parameter until it exists, then evals it.
#   3. A cron on the control plane refreshes the token every 12h, so a
#      scale-out next week still gets a valid token (kubeadm tokens default to
#      a 24h TTL).
#
# Why this over the alternatives:
#   * vs. Lambda + ASG lifecycle hooks — no Lambda, no SNS topic, no IAM
#     plumbing between them; the whole mechanism is two AWS CLI calls that you
#     can run by hand to debug.
#   * vs. Secrets Manager — functionally identical, but SSM SecureString is free
#     at this scale while Secrets Manager bills ~$0.40/secret/month.
#   * Security: IAM scopes the control plane to PutParameter and workers to
#     GetParameter on /polyai/<cluster>/* only, so neither can read another
#     student's parameters in this shared account.
#
# Node removal on scale-down is MANUAL (`kubectl delete node <name>`), which the
# task explicitly permits. A terminating-lifecycle-hook + Lambda would automate
# it, but the task asks that we only implement what we can fully explain.

# Newest Ubuntu 22.04 LTS AMI in the CURRENT region (owner = Canonical).
# A data-source lookup rather than a hard-coded AMI ID is what makes this module
# work in any region unchanged.
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

locals {
  # SSM parameter path carrying the kubeadm join command from the control plane
  # to ASG-launched workers. The control plane writes it; workers read it.
  #
  # Only ONE parameter is used. An earlier version also published the kubeconfig
  # here, but a kubeconfig is ~5.4 KB and SSM Standard-tier parameters cap at
  # 4096 characters, so PutParameter failed. It also would have put cluster-admin
  # certs in a shared account. The control plane now writes that file to
  # /home/ubuntu/kubeconfig-public.yaml on disk instead.
  join_command_param = "/polyai/${var.cluster_name}/join-command"
}

# ---------------------------------------------------------------------------
# IAM — shared trust policy
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# IAM — control plane
# ---------------------------------------------------------------------------
resource "aws_iam_role" "control_plane" {
  name               = "${var.cluster_name}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

# The three managed policies task006 requires by name.
resource "aws_iam_role_policy_attachment" "control_plane_eks_cluster" {
  role = aws_iam_role.control_plane.name
  # Required by the task spec. (A kubeadm cluster does not strictly need it —
  # it grants the AWS-managed EKS control plane its ENI/ELB permissions — but
  # the task names it explicitly.)
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "control_plane_ebs_csi" {
  role = aws_iam_role.control_plane.name
  # Lets the EBS CSI controller create/attach/delete volumes, which backs the
  # Prometheus and Grafana PersistentVolumeClaims.
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "control_plane_ecr" {
  role = aws_iam_role.control_plane.name
  # Pull container images from ECR.
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Not required by the task, but it enables `aws ssm start-session` for shell
# access without SSH, which is invaluable when a user-data script fails and the
# instance never opens port 22.
resource "aws_iam_role_policy_attachment" "control_plane_ssm_core" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Least privilege: the control plane may write ONLY its own cluster's parameters.
data "aws_iam_policy_document" "control_plane_ssm_write" {
  statement {
    sid = "ManageOwnClusterParameters"
    actions = [
      "ssm:PutParameter",
      "ssm:GetParameter",
      "ssm:DeleteParameter",
      "ssm:AddTagsToResource",
    ]
    resources = [
      "arn:aws:ssm:${var.region}:*:parameter${local.join_command_param}",
    ]
  }
}

resource "aws_iam_role_policy" "control_plane_ssm_write" {
  name   = "${var.cluster_name}-control-plane-ssm"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane_ssm_write.json
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "${var.cluster_name}-control-plane"
  role = aws_iam_role.control_plane.name
}

# ---------------------------------------------------------------------------
# IAM — workers
# ---------------------------------------------------------------------------
resource "aws_iam_role" "worker" {
  name               = "${var.cluster_name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "worker_ecr" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "worker_ebs_csi" {
  role = aws_iam_role.worker.name
  # The EBS CSI *node* plugin runs on workers and needs DescribeVolumes /
  # AttachVolume to mount the Prometheus and Grafana volumes.
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "worker_ssm_core" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Workers may READ the join command and nothing else.
data "aws_iam_policy_document" "worker_ssm_read" {
  statement {
    sid       = "ReadJoinCommand"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:*:parameter${local.join_command_param}"]
  }
}

resource "aws_iam_role_policy" "worker_ssm_read" {
  name   = "${var.cluster_name}-worker-ssm"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_ssm_read.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.cluster_name}-worker"
  role = aws_iam_role.worker.name
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------
resource "aws_security_group" "control_plane" {
  name        = "${var.cluster_name}-control-plane"
  description = "Kubernetes control plane: SSH, API server, and all intra-VPC traffic"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.cluster_name}-control-plane" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_ssh" {
  security_group_id = aws_security_group.control_plane.id
  description       = "SSH"
  cidr_ipv4         = var.ssh_ingress_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_apiserver" {
  security_group_id = aws_security_group.control_plane.id
  description       = "Kubernetes API server - reached by kubectl from CI and your laptop"
  cidr_ipv4         = var.ssh_ingress_cidr
  from_port         = 6443
  to_port           = 6443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_intra_vpc" {
  security_group_id = aws_security_group.control_plane.id
  # One rule for all intra-VPC traffic, as the task specifies. It covers kubelet
  # (10250), etcd (2379-2380), NodePort range, and Calico's BGP/VXLAN/IPIP
  # without enumerating them — enumeration is where CNI setups silently break.
  description = "All traffic from within the VPC"
  cidr_ipv4   = var.vpc_cidr
  ip_protocol = "-1"
}

resource "aws_vpc_security_group_egress_rule" "control_plane_all" {
  security_group_id = aws_security_group.control_plane.id
  description       = "All outbound - apt, image pulls, AWS APIs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "worker" {
  name        = "${var.cluster_name}-worker"
  description = "Kubernetes workers: SSH, NodePorts, and all intra-VPC traffic"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.cluster_name}-worker" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "worker_ssh" {
  security_group_id = aws_security_group.worker.id
  description       = "SSH"
  cidr_ipv4         = var.ssh_ingress_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "worker_intra_vpc" {
  security_group_id = aws_security_group.worker.id
  description       = "All traffic from within the VPC"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "worker_nodeports" {
  security_group_id = aws_security_group.worker.id
  # The browser talks to frontend (30300/31300), agent (30800/31800) and grafana
  # (30301/31301) directly on the worker's public IP via NodePort Services.
  description = "Kubernetes NodePort range - browser reaches frontend/agent/grafana here"
  cidr_ipv4   = "0.0.0.0/0"
  from_port   = 30000
  to_port     = 32767
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "worker_all" {
  security_group_id = aws_security_group.worker.id
  description       = "All outbound - apt, image pulls, AWS APIs"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------------------------------------------------------------------------
# Control plane instance
# ---------------------------------------------------------------------------
resource "aws_instance" "control_plane" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = var.control_plane_instance_type

  subnet_id                   = var.subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.control_plane.id]
  iam_instance_profile        = aws_iam_instance_profile.control_plane.name
  key_name                    = var.ssh_key_name
  associate_public_ip_address = true

  root_block_device {
    volume_size           = var.root_volume_size
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/templates/control-plane-user-data.sh.tftpl", {
    cluster_name       = var.cluster_name
    region             = var.region
    kubernetes_version = var.kubernetes_version
    pod_network_cidr   = var.pod_network_cidr
    join_command_param = local.join_command_param
  })

  # user-data only executes on FIRST boot, so editing the script must replace the
  # instance for the change to take effect. Without this, a script edit produces
  # a successful apply that changes nothing on the box — a genuinely confusing
  # failure mode.
  user_data_replace_on_change = true

  # IAM must exist before boot: user-data's very first AWS call is a PutParameter,
  # and an instance profile attached a second late means a hard failure.
  depends_on = [
    aws_iam_role_policy.control_plane_ssm_write,
    aws_iam_role_policy_attachment.control_plane_ssm_core,
  ]

  tags = {
    Name = "${var.cluster_name}-control-plane"
    Role = "control-plane"
  }
}

# ---------------------------------------------------------------------------
# Workers — launch template + ASG
# ---------------------------------------------------------------------------
resource "aws_launch_template" "worker" {
  name_prefix   = "${var.cluster_name}-worker-"
  image_id      = data.aws_ami.ubuntu.id
  instance_type = var.worker_instance_type
  key_name      = var.ssh_key_name

  vpc_security_group_ids = [aws_security_group.worker.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.worker.name
  }

  block_device_mappings {
    device_name = "/dev/sda1" # Ubuntu's root device
    ebs {
      volume_size           = var.root_volume_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  metadata_options {
    http_tokens                 = "required" # IMDSv2 only
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2 # containers may need IMDS
  }

  user_data = base64encode(templatefile("${path.module}/templates/worker-user-data.sh.tftpl", {
    cluster_name       = var.cluster_name
    region             = var.region
    kubernetes_version = var.kubernetes_version
    join_command_param = local.join_command_param
  }))

  # Launch-template tags do NOT propagate to launched instances; tag_specifications
  # is what actually labels the EC2 instances the ASG creates.
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${var.cluster_name}-worker"
      Role = "worker"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name = "${var.cluster_name}-worker-root"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "workers" {
  name = "${var.cluster_name}-workers"

  vpc_zone_identifier = var.subnet_ids # spans both AZs

  min_size         = var.worker_min_size
  max_size         = var.worker_max_size
  desired_capacity = var.worker_desired_capacity

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }

  # EC2 health checks only. An ELB health check would be wrong here: nothing
  # fronts these nodes with a load balancer, and NodePort readiness depends on
  # pod scheduling rather than instance health.
  health_check_type         = "EC2"
  health_check_grace_period = 300 # allow time for deps install + kubeadm join

  # Wait for the control plane before launching workers. The worker's SSM poll
  # loop makes it eventually-correct regardless, but this avoids a worker
  # burning 5 minutes polling for a parameter that doesn't exist yet.
  depends_on = [aws_instance.control_plane]

  # Editing worker user-data should recycle the fleet, not leave stale nodes.
  instance_refresh {
    strategy = "Rolling"
    preferences {
      # 0% ensures a 1-node ASG can still refresh (it terminates then replaces).
      min_healthy_percentage = 0
    }
  }

  tag {
    key                 = "Name"
    value               = "${var.cluster_name}-worker"
    propagate_at_launch = true
  }

  tag {
    key                 = "Role"
    value               = "worker"
    propagate_at_launch = true
  }

  # Terraform's default_tags do not reach ASG-launched instances; repeat the ones
  # that matter for cost attribution in the shared account.
  tag {
    key                 = "Owner"
    value               = "shahdra"
    propagate_at_launch = true
  }

  tag {
    key                 = "Cluster"
    value               = var.cluster_name
    propagate_at_launch = true
  }

  # NOTE on desired_capacity and manual scaling.
  # desired_capacity is deliberately NOT in ignore_changes. The cluster.yaml
  # workflow exposes a worker_desired input, and an ignored attribute would make
  # that input silently do nothing — a worse failure than visible drift.
  #
  # Consequence: if you scale by hand with
  #   aws autoscaling set-desired-capacity ... --desired-capacity 0
  # the next `terraform apply` will raise it back to worker_desired_capacity.
  # That is intended and visible in the plan output. To park the cluster durably,
  # apply with `-var worker_desired_capacity=0` instead of using the CLI.
}
