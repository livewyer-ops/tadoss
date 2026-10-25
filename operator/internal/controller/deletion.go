package controller

import (
	"context"
	"slices"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

func (r *TadossReconciler) handleDeletion(ctx context.Context, t *tadossv1.Tadoss) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(t, finalizer) {
		return ctrl.Result{}, nil
	}
	terminating, err := r.namespaceTerminating(ctx, t.Namespace)
	if err != nil {
		return ctrl.Result{}, err
	}
	switch {
	case terminating:
		logf.FromContext(ctx).Info("namespace is terminating; releasing the finalizer regardless of deletionProtection and deletionPolicy")
	case ptr.Deref(t.Spec.DeletionProtection, true):
		if err := r.status(ctx, t, tadossv1.PhaseProgressing, "DeletionBlocked", "Set spec.deletionProtection=false to permit deletion", false); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: time.Minute}, nil
	case t.Spec.Backends.DB.ProvidedBy == tadossv1.ProviderManaged && t.Spec.DeletionPolicy == tadossv1.DeletionPolicyRetain:
		retained := []client.Object{
			&cnpgv1.Cluster{ObjectMeta: metadata(t, suffixDB)},
			&cnpgv1.ScheduledBackup{ObjectMeta: metadata(t, "-backup")},
		}
		for _, object := range retained {
			if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
				if apierrors.IsNotFound(err) {
					continue
				}
				return ctrl.Result{}, err
			}
			before := object.DeepCopyObject().(client.Object)
			owners := slices.DeleteFunc(slices.Clone(object.GetOwnerReferences()), func(owner metav1.OwnerReference) bool { return owner.UID == t.UID })
			if len(owners) == len(object.GetOwnerReferences()) {
				continue
			}
			object.SetOwnerReferences(owners)
			if err := r.Patch(ctx, object, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
				return ctrl.Result{}, err
			}
		}
	}
	before := t.DeepCopy()
	controllerutil.RemoveFinalizer(t, finalizer)
	return ctrl.Result{}, r.Patch(ctx, t, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{}))
}

// namespaceTerminating reports whether the instance namespace carries a deletion timestamp.
func (r *TadossReconciler) namespaceTerminating(ctx context.Context, name string) (bool, error) {
	ns := &metav1.PartialObjectMetadata{}
	ns.SetGroupVersionKind(corev1.SchemeGroupVersion.WithKind("Namespace"))
	if err := r.Get(ctx, client.ObjectKey{Name: name}, ns); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	return !ns.DeletionTimestamp.IsZero(), nil
}
