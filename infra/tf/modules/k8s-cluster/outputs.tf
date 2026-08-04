output "control_plane_public_ip" {
  description = "Public IP of the control plane."
  value       = aws_instance.control_plane.public_ip
}

output "control_plane_private_ip" {
  description = "Private IP of the control plane (the API server advertise address)."
  value       = aws_instance.control_plane.private_ip
}

output "control_plane_instance_id" {
  description = "Instance ID of the control plane."
  value       = aws_instance.control_plane.id
}

output "control_plane_security_group_id" {
  description = "Security group ID attached to the control plane."
  value       = aws_security_group.control_plane.id
}

output "worker_asg_name" {
  description = "Name of the worker Auto Scaling Group."
  value       = aws_autoscaling_group.workers.name
}

output "worker_security_group_id" {
  description = "Security group ID attached to worker nodes."
  value       = aws_security_group.worker.id
}

output "worker_launch_template_id" {
  description = "ID of the worker launch template."
  value       = aws_launch_template.worker.id
}

output "join_command_ssm_parameter" {
  description = "SSM parameter path holding the kubeadm join command."
  value       = local.join_command_param
}

output "kubeconfig_path_on_control_plane" {
  description = "Path on the control plane to a kubeconfig whose server URL is the PUBLIC IP. Fetch with scp."
  value       = "/home/ubuntu/kubeconfig-public.yaml"
}

output "ami_id" {
  description = "Ubuntu AMI resolved for this region."
  value       = data.aws_ami.ubuntu.id
}
