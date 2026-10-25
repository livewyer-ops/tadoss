package controller

import (
	"context"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const postgresContainerName = "postgres"

func (r *TadossReconciler) cnpgRolloutReady(ctx context.Context, cluster *cnpgv1.Cluster) (bool, error) {
	pods := &corev1.PodList{}
	if err := r.List(ctx, pods, client.InNamespace(cluster.Namespace), client.MatchingLabels{"cnpg.io/cluster": cluster.Name}); err != nil {
		return false, err
	}
	count := 0
	for _, pod := range pods.Items {
		if !metav1.IsControlledBy(&pod, cluster) {
			continue
		}
		// Bootstrap and backup Jobs have their own owners; only database Pods count.
		if !pod.DeletionTimestamp.IsZero() {
			return false, nil
		}
		ready := false
		for _, container := range pod.Spec.Containers {
			if container.Name == postgresContainerName && container.Image == cluster.Spec.ImageName {
				for _, status := range pod.Status.ContainerStatuses {
					if status.Name == postgresContainerName && status.Ready {
						ready = true
					}
				}
			}
		}
		if !ready {
			return false, nil
		}
		count++
	}
	return count == cluster.Spec.Instances, nil
}
