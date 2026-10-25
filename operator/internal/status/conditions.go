// Package status updates conditions and identifies changes worth reporting.
package status

import (
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SetConditionBool records a boolean condition for the observed generation.
func SetConditionBool(conditions *[]metav1.Condition, generation int64, conditionType string, ok bool, reason, message string) {
	status := metav1.ConditionFalse
	if ok {
		status = metav1.ConditionTrue
	}
	SetConditionStatus(conditions, generation, conditionType, status, reason, message)
}

// SetConditionStatus preserves the transition time when condition status is unchanged.
func SetConditionStatus(conditions *[]metav1.Condition, generation int64, conditionType string, status metav1.ConditionStatus, reason, message string) {
	meta.SetStatusCondition(conditions, metav1.Condition{
		Type:               conditionType,
		Status:             status,
		ObservedGeneration: generation,
		Reason:             reason,
		Message:            message,
	})
}

// ChangedConditionReason reports changes to a condition's status, reason or message.
func ChangedConditionReason(oldConditions, newConditions []metav1.Condition, conditionType string) (string, string, bool) {
	newCondition := meta.FindStatusCondition(newConditions, conditionType)
	if newCondition == nil {
		return "", "", false
	}
	oldCondition := meta.FindStatusCondition(oldConditions, conditionType)
	if oldCondition != nil &&
		oldCondition.Status == newCondition.Status &&
		oldCondition.Reason == newCondition.Reason &&
		oldCondition.Message == newCondition.Message {
		return "", "", false
	}
	return newCondition.Reason, newCondition.Message, true
}
