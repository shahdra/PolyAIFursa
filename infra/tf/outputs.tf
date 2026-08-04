# Root outputs. These are the contract consumed by .github/workflows/cluster.yaml
# — the bootstrap job reads them with `terraform output -raw <name>` — so renaming
# one means updating the workflow.

output "cluster_name" {
  description = "Cluster identity; prefixes every resource name and the SSM parameter paths."
  value       = local.cluster_name
}

output "region" {
  description = "Region this cluster is deployed in (equals the workspace name)."
  value       = var.region
}

output "control_plane_public_ip" {
  description = "Public IP of the control plane. Used by CI to SSH in and bootstrap."
  value       = module.k8s_cluster.control_plane_public_ip
}

output "control_plane_private_ip" {
  description = "Private IP of the control plane (the API server's advertise address)."
  value       = module.k8s_cluster.control_plane_private_ip
}

output "control_plane_instance_id" {
  description = "Instance ID of the control plane, for aws ec2 / ssm commands."
  value       = module.k8s_cluster.control_plane_instance_id
}

output "control_plane_security_group_id" {
  description = <<-EOT
    Security group on the control plane. Handy for adding a temporary rule, e.g.
    exposing the ArgoCD UI to your own IP — see infra/k8s/argo/README.md step 3.
  EOT
  value       = module.k8s_cluster.control_plane_security_group_id
}

output "worker_security_group_id" {
  description = "Security group on the worker nodes."
  value       = module.k8s_cluster.worker_security_group_id
}

output "worker_asg_name" {
  description = <<-EOT
    Worker Auto Scaling Group name. Scale down without a terraform apply:
      aws autoscaling set-desired-capacity \
        --auto-scaling-group-name $(terraform output -raw worker_asg_name) \
        --desired-capacity 0
  EOT
  value       = module.k8s_cluster.worker_asg_name
}

output "join_command_ssm_parameter" {
  description = <<-EOT
    SSM parameter holding the `kubeadm join` command. Inspect it when a worker
    fails to join:
      aws ssm get-parameter --name <this> --with-decryption \
        --query Parameter.Value --output text
  EOT
  value       = module.k8s_cluster.join_command_ssm_parameter
}

output "fetch_kubeconfig_command" {
  description = <<-EOT
    Copy a kubeconfig that works from your laptop (server URL = the public IP,
    which is already in the API server cert's SANs).
  EOT
  value       = "scp -i ${var.ssh_private_key_path} ubuntu@${module.k8s_cluster.control_plane_public_ip}:${module.k8s_cluster.kubeconfig_path_on_control_plane} ./kubeconfig-${var.region}.yaml"
}

output "vpc_id" {
  description = "ID of the cluster VPC."
  value       = module.vpc.vpc_id
}

output "public_subnet_ids" {
  description = "The two public subnet IDs (one per AZ) holding all cluster instances."
  value       = module.vpc.public_subnets
}

output "ssh_command" {
  description = "Ready-to-paste SSH command for the control plane."
  value       = "ssh -i ${var.ssh_private_key_path} ubuntu@${module.k8s_cluster.control_plane_public_ip}"
}
