package controller

import (
	"context"
	"fmt"
	"reflect"
	"time"

	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
)

func (r *TadossReconciler) pending(ctx context.Context, t *tadossv1.Tadoss, reason, message string) (ctrl.Result, error) {
	if err := r.status(ctx, t, tadossv1.PhaseProgressing, reason, message, false); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
}

func (r *TadossReconciler) status(ctx context.Context, t *tadossv1.Tadoss, phase, reason, message string, ready bool) error {
	live := &tadossv1.Tadoss{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(t), live); err != nil {
		return err
	}
	if live.Generation != t.Generation {
		return apierrors.NewConflict(tadossv1.GroupVersion.WithResource("tadosses").GroupResource(), t.Name, fmt.Errorf("spec changed during reconciliation"))
	}
	before := live.DeepCopy()
	live.Status = t.Status
	live.Status.Phase = phase
	// observedGeneration advances only once a generation has been rolled out or has
	// failed, so waiting on it is meaningful.
	live.Status.ObservedGeneration = before.Status.ObservedGeneration
	if ready || phase == tadossv1.PhaseError {
		live.Status.ObservedGeneration = t.Generation
	}
	operatorstatus.SetConditionBool(&live.Status.Conditions, t.Generation, tadossv1.ConditionReady, ready, reason, message)
	if reflect.DeepEqual(before.Status, live.Status) {
		return nil
	}
	if err := r.Status().Patch(ctx, live, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
		return err
	}
	_, _, changed := operatorstatus.ChangedConditionReason(before.Status.Conditions, live.Status.Conditions, tadossv1.ConditionReady)
	if changed {
		logf.FromContext(ctx).Info("status changed", "phase", phase, "reason", reason, "message", message)
	}
	if r.Recorder != nil && changed {
		eventType := corev1.EventTypeNormal
		if phase == tadossv1.PhaseError || reason == "DeletionBlocked" {
			eventType = corev1.EventTypeWarning
		}
		r.Recorder.Eventf(t, nil, eventType, reason, "Reconcile", "%s", message)
	}
	return nil
}

func deploymentReady(deployment *appsv1.Deployment, desired int32) bool {
	status := deployment.Status
	return status.ObservedGeneration >= deployment.Generation && status.UpdatedReplicas == desired && status.AvailableReplicas == desired && status.Replicas == desired
}
