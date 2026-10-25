package controller

import (
	"context"
	"fmt"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func (r *TadossReconciler) apply(ctx context.Context, t *tadossv1.Tadoss, desired client.Object) error {
	empty, err := r.Scheme.New(desired.GetObjectKind().GroupVersionKind())
	if err != nil {
		return err
	}
	live := empty.(client.Object)
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), live); err == nil {
		if !metav1.IsControlledBy(live, t) {
			return fmt.Errorf("%s %s is not owned by this instance", desired.GetObjectKind().GroupVersionKind().Kind, desired.GetName())
		}
	} else if !apierrors.IsNotFound(err) {
		return err
	}
	if err := controllerutil.SetControllerReference(t, desired, r.Scheme); err != nil {
		return err
	}
	_, err = applyManagedObject(ctx, r.Client, desired)
	return err
}

func (r *TadossReconciler) prune(ctx context.Context, t *tadossv1.Tadoss, currentJob string, desired workloadSet) error {
	var stale []client.Object
	key := client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffixAPI}
	if t.Spec.Ingress.Host == "" {
		ingress, err := r.owned(ctx, t, key, &networkingv1.Ingress{})
		if err != nil {
			return err
		}
		stale = append(stale, ingress)
	}
	if desired.Budget == nil {
		budget, err := r.owned(ctx, t, key, &policyv1.PodDisruptionBudget{})
		if err != nil {
			return err
		}
		stale = append(stale, budget)
	}
	if desired.WorkerBudget == nil {
		budget, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-worker"}, &policyv1.PodDisruptionBudget{})
		if err != nil {
			return err
		}
		stale = append(stale, budget)
	}
	if !ptr.Deref(t.Spec.NetworkPolicy.Enabled, t.Spec.Profile == tadossv1.ProfileMultiServer) {
		for _, component := range []string{"api", "worker"} {
			policy, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-" + component}, &networkingv1.NetworkPolicy{})
			if err != nil {
				return err
			}
			stale = append(stale, policy)
		}
	}
	if t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged && !t.Spec.Backends.DB.CNPG.Backup.Enabled {
		backup, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + "-backup"}, &cnpgv1.ScheduledBackup{})
		if err != nil {
			return err
		}
		stale = append(stale, backup)
	}
	jobs := &batchv1.JobList{}
	if err := r.List(ctx, jobs, client.InNamespace(t.Namespace), client.MatchingLabels{instanceUID: string(t.UID)}); err != nil {
		return err
	}
	for i := range jobs.Items {
		if jobs.Items[i].Name != currentJob && metav1.IsControlledBy(&jobs.Items[i], t) {
			stale = append(stale, &jobs.Items[i])
		}
	}
	for _, object := range stale {
		if object == nil {
			continue
		}
		if err := r.Delete(ctx, object, client.PropagationPolicy(metav1.DeletePropagationBackground)); client.IgnoreNotFound(err) != nil {
			return err
		}
	}
	return nil
}

// owned reads an object into empty and returns it only when this instance controls it.
func (r *TadossReconciler) owned(ctx context.Context, t *tadossv1.Tadoss, key client.ObjectKey, empty client.Object) (client.Object, error) {
	if err := r.Get(ctx, key, empty); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(empty, t) {
		return nil, nil
	}
	return empty, nil
}
