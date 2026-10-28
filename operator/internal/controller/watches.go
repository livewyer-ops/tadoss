package controller

import (
	"context"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// SetupWithManager registers the controller, its owned resources and its watches.
func (r *TadossReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if err := mgr.GetFieldIndexer().IndexField(
		context.Background(), &tadossv1.Tadoss{}, tadossReferenceIndex, tadossReferenceIndexValue,
	); err != nil {
		return err
	}
	terminating := builder.WithPredicates(predicate.NewPredicateFuncs(func(o client.Object) bool { return !o.GetDeletionTimestamp().IsZero() }))
	b := ctrl.NewControllerManagedBy(mgr).For(&tadossv1.Tadoss{}, builder.WithPredicates(primaryResourcePredicate(nil))).
		Owns(&appsv1.Deployment{}).Owns(&autoscalingv2.HorizontalPodAutoscaler{}).Owns(&batchv1.Job{}).Owns(&corev1.Service{}).
		Owns(&networkingv1.Ingress{}).Owns(&networkingv1.NetworkPolicy{}).Owns(&policyv1.PodDisruptionBudget{}).
		WatchesMetadata(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(r.secretRequests)).
		WatchesMetadata(&corev1.Secret{}, handler.EnqueueRequestsFromMapFunc(r.secretRequests)).
		WatchesMetadata(&corev1.Namespace{}, handler.EnqueueRequestsFromMapFunc(r.namespaceRequests), terminating)
	if _, err := mgr.GetRESTMapper().RESTMapping(cnpgv1.SchemeGroupVersion.WithKind("Cluster").GroupKind(), "v1"); err == nil {
		b = b.Owns(&cnpgv1.Cluster{}).Owns(&cnpgv1.ScheduledBackup{}).Owns(&cnpgv1.Pooler{})
	}
	return b.Complete(r)
}

// namespaceRequests enqueues every instance in a namespace that started terminating.
func (r *TadossReconciler) namespaceRequests(ctx context.Context, object client.Object) []reconcile.Request {
	instances := &tadossv1.TadossList{}
	if err := r.List(ctx, instances, client.InNamespace(object.GetName())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0, len(instances.Items))
	for i := range instances.Items {
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&instances.Items[i])})
	}
	return requests
}

func (r *TadossReconciler) secretRequests(ctx context.Context, object client.Object) []reconcile.Request {
	instances := &tadossv1.TadossList{}
	if err := r.List(ctx, instances,
		client.InNamespace(object.GetNamespace()),
		client.MatchingFields{tadossReferenceIndex: object.GetName()},
	); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0, len(instances.Items))
	for i := range instances.Items {
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&instances.Items[i])})
	}
	return requests
}
