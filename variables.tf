variable "project_name" {
  description = "Project name — used for all resource names and tags"
  type        = string
}

variable "region" {
  description = "AWS region to deploy into (set via config.env AWS_DEFAULT_REGION)"
  type        = string
}

variable "my_ip_cidr" {
  description = "My public IP CIDR (auto-written by setup.py)"
  type        = string
}