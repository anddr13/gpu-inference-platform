output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "kubeconfig_command" {
  description = "Writes a separate kubeconfig (the Makefile's EKS_KUBECONFIG); ~/.kube/config and its current context are not touched."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name} --kubeconfig \"$HOME/.kube/eks-${module.eks.cluster_name}.yaml\""
}
