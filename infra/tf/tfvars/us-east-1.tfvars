# Per-region values for the us-east-1 workspace.
#
#   terraform workspace select us-east-1
#   terraform apply -var-file=tfvars/us-east-1.tfvars
#
# The workspace name MUST equal `region` (enforced by a precondition in main.tf).
# To add another region, copy this file to tfvars/<region>.tfvars, change the
# region value, and create the matching workspace. No other code changes.
#
# This file is committed on purpose — it holds no secrets. The .gitignore rule
# `!infra/tf/tfvars/*.tfvars` re-includes it after the blanket *.tfvars ignore.

region = "us-east-1"

# Pre-existing EC2 key pair used for SSH to the control plane and workers.
ssh_key_name = "shahd-key"

# Local path to the matching private key. Used only to render the ssh_command /
# fetch_kubeconfig_command outputs as copy-pasteable strings.
ssh_private_key_path = "/Users/saed/shahd-key.pem"

# Set to 0 when you stop working for the day to avoid EC2 charges; 1 while
# working. See variables.tf for the min_size interaction.
worker_desired_capacity = 1
