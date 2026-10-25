// Package controller reconciles Tadoss instances into their Kubernetes resources.
package controller

import (
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/controller/resource"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	instanceUID = "tadoss.livewyer.io/instance-uid"
	apiPort     = 8000
	suffixAPI   = "-api"
	suffixDB    = "-db"
	suffixDBApp = "-db-app"
	suffixDBCA  = "-db-ca"
	suffixToken = "-api-token"

	// The migration waits up to five minutes for a restarting database, so the Job
	// deadline must comfortably exceed that plus the retries.
	schemaJobBackoffLimit    = 4
	schemaJobDeadlineSeconds = 900
)

func replicas(t *tadossv1.Tadoss) int32 {
	if t.Spec.API.ReplicaCount != nil {
		return *t.Spec.API.ReplicaCount
	}
	if t.Spec.Profile == tadossv1.ProfileMultiServer {
		return 2
	}
	return 1
}

// selectorLabels identify API pods without the instance UID, so a recreated
// instance keeps matching the workloads that survived it.
func selectorLabels(t *tadossv1.Tadoss) map[string]string {
	return resource.Labels(resource.AppName, t.Name, "api")
}

func labels(t *tadossv1.Tadoss) map[string]string {
	values := resource.Labels(resource.AppName, t.Name, "")
	values[instanceUID] = string(t.UID)
	return values
}

func metadata(t *tadossv1.Tadoss, suffix string) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: t.Name + suffix, Namespace: t.Namespace, Labels: labels(t)}
}

func withDefaults(requirements corev1.ResourceRequirements, requests, limits corev1.ResourceList) corev1.ResourceRequirements {
	resources := requirements.DeepCopy()
	if resources.Requests == nil {
		resources.Requests = corev1.ResourceList{}
	}
	if resources.Limits == nil {
		resources.Limits = corev1.ResourceList{}
	}
	for key, value := range requests {
		if _, explicit := resources.Requests[key]; !explicit {
			if limit, ok := resources.Limits[key]; ok && value.Cmp(limit) > 0 {
				value = limit
			}
			resources.Requests[key] = value.DeepCopy()
		}
	}
	for key, value := range limits {
		if _, explicit := resources.Limits[key]; !explicit {
			if request, ok := resources.Requests[key]; ok && value.Cmp(request) < 0 {
				value = request
			}
			resources.Limits[key] = value.DeepCopy()
		}
	}
	return *resources
}
