variable "project_name" {
  description = "Project name — used for all resource names and tags"
  type        = string
}

variable "my_ip_cidr" {
  description = "My public IP CIDR (auto-written by setup.py)"
  type        = string
}