package status

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func TestConditionTransitionsAndEvents(t *testing.T) {
	var conditions []metav1.Condition
	SetConditionBool(&conditions, 1, "Ready", false, "Waiting", "waiting")
	before := append([]metav1.Condition(nil), conditions...)
	SetConditionBool(&conditions, 2, "Ready", false, "Waiting", "waiting")
	if conditions[0].ObservedGeneration != 2 || !conditions[0].LastTransitionTime.Equal(&before[0].LastTransitionTime) {
		t.Fatal("unchanged readiness must retain transition time and update the generation")
	}
	if _, _, changed := ChangedConditionReason(before, conditions, "Ready"); changed {
		t.Fatal("a generation-only change must not emit an event")
	}
	SetConditionBool(&conditions, 2, "Ready", true, "Available", "ready")
	reason, message, changed := ChangedConditionReason(before, conditions, "Ready")
	if !changed || reason != "Available" || message != "ready" {
		t.Fatal("readiness transition was lost")
	}
}
