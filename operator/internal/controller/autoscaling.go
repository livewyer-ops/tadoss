package controller

import (
	"context"
	"encoding/json"
	"fmt"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"
	appsv1 "k8s.io/api/apps/v1"
	autoscalingv2 "k8s.io/api/autoscaling/v2"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func (r *TadossReconciler) reconcileAutoscaling(ctx context.Context, t *tadossv1.Tadoss) error {
	a := t.Spec.API.Autoscaling
	if !a.Enabled {
		object, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffixAPI}, &autoscalingv2.HorizontalPodAutoscaler{})
		if err != nil {
			return err
		}
		if object != nil {
			if err := r.Delete(ctx, object); client.IgnoreNotFound(err) != nil {
				return err
			}
		}
		operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "AutoscalingReady", true, "Disabled", "API replica count is explicit")
		return nil
	}
	hpa := &autoscalingv2.HorizontalPodAutoscaler{
		TypeMeta: metav1.TypeMeta{APIVersion: "autoscaling/v2", Kind: "HorizontalPodAutoscaler"}, ObjectMeta: metadata(t, suffixAPI),
		Spec: autoscalingv2.HorizontalPodAutoscalerSpec{
			ScaleTargetRef: autoscalingv2.CrossVersionObjectReference{APIVersion: "apps/v1", Kind: "Deployment", Name: t.Name + suffixAPI},
			MinReplicas:    ptr.To(ptr.Deref(a.MinReplicas, 2)), MaxReplicas: ptr.Deref(a.MaxReplicas, 8), Metrics: a.Metrics, Behavior: a.Behavior,
		},
	}
	if err := r.apply(ctx, t, hpa); err != nil {
		return err
	}
	ready, reason, message := false, "MetricsPending", "Waiting for HPA metrics; API replicas continue serving"
	for _, condition := range hpa.Status.Conditions {
		if condition.Type == autoscalingv2.ScalingActive {
			ready, reason, message = condition.Status == corev1.ConditionTrue, condition.Reason, condition.Message
		}
	}
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "AutoscalingReady", ready, reason, message)
	return nil
}

// Release main-resource ownership before omitting replicas from SSA. Otherwise
// removing the field would reset a manually scaled Deployment to the default of 1.
func releaseReplicaOwnership(ctx context.Context, c client.Client, live *appsv1.Deployment) error {
	entries := live.GetManagedFields()
	changed := false
	for i := range entries {
		entry := &entries[i]
		if entry.Subresource != "" || entry.FieldsV1 == nil {
			continue
		}
		var fields map[string]any
		if err := json.Unmarshal(entry.FieldsV1.GetRawBytes(), &fields); err != nil {
			return fmt.Errorf("decode deployment field ownership: %w", err)
		}
		spec, ok := fields["f:spec"].(map[string]any)
		if !ok {
			continue
		}
		if _, found := spec["f:replicas"]; !found {
			continue
		}
		delete(spec, "f:replicas")
		if len(spec) == 0 {
			delete(fields, "f:spec")
		}
		raw, err := json.Marshal(fields)
		if err != nil {
			return err
		}
		entry.FieldsV1 = &metav1.FieldsV1{Raw: raw}
		changed = true
	}
	if !changed {
		return nil
	}
	live.SetManagedFields(entries)
	return c.Update(ctx, live)
}
