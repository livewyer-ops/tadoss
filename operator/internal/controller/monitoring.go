package controller

import (
	"context"
	"fmt"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	operatorstatus "github.com/livewyer-ops/tadoss/operator/internal/status"
	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func monitoringObjects(t *tadossv1.Tadoss) []client.Object {
	var result []client.Object
	for _, component := range []string{"api", "worker", "database", "pooler"} {
		if component == "database" && t.Spec.Backends.DB.ProvidedBy != tadossv1.ProviderManaged {
			continue
		}
		if component == "pooler" && !t.Spec.Backends.DB.CNPG.Pooler.Enabled {
			continue
		}
		selector := selectorLabels(t)
		selector["app.kubernetes.io/component"] = component
		port := "http"
		switch component {
		case "database":
			selector, port = map[string]string{"cnpg.io/cluster": t.Name + suffixDB, "cnpg.io/podRole": "instance"}, "metrics"
		case "pooler":
			selector, port = map[string]string{"tadoss.livewyer.io/pooler": t.Name + "-pooler"}, "metrics"
		}
		endpoint := monitoringv1.PodMetricsEndpoint{Port: ptr.To(port), Path: "/metrics", Interval: "15s", ScrapeTimeout: "5s",
			RelabelConfigs: []monitoringv1.RelabelConfig{{TargetLabel: "tadoss_instance", Replacement: ptr.To(t.Name)}},
		}
		if component == "api" {
			endpoint.Authorization = &monitoringv1.SafeAuthorization{Type: "Bearer", Credentials: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: t.Name + "-metrics"}, Key: "token"}}
		}
		monitor := &monitoringv1.PodMonitor{
			TypeMeta: metav1.TypeMeta{APIVersion: monitoringv1.SchemeGroupVersion.String(), Kind: "PodMonitor"}, ObjectMeta: metadata(t, "-"+component),
			Spec: monitoringv1.PodMonitorSpec{Selector: metav1.LabelSelector{MatchLabels: selector}, PodMetricsEndpoints: []monitoringv1.PodMetricsEndpoint{endpoint}},
		}
		monitor.Labels = mergeStringMaps(t.Spec.Monitoring.Labels, monitor.Labels)
		result = append(result, monitor)
	}
	selector := fmt.Sprintf(`namespace=%q,tadoss_instance=%q`, t.Namespace, t.Name)
	rules := &monitoringv1.PrometheusRule{
		TypeMeta: metav1.TypeMeta{APIVersion: monitoringv1.SchemeGroupVersion.String(), Kind: "PrometheusRule"}, ObjectMeta: metadata(t, "-alerts"),
		Spec: monitoringv1.PrometheusRuleSpec{Groups: []monitoringv1.RuleGroup{{Name: "tadoss." + t.Namespace + "." + t.Name, Rules: []monitoringv1.Rule{
			{Alert: "TADOSSRequestErrors", Expr: intstr.FromString(fmt.Sprintf(`sum(rate(tadoss_http_requests_total{%s,status=~"5.."}[5m])) / clamp_min(sum(rate(tadoss_http_requests_total{%s}[5m])), 0.001) > 0.01`, selector, selector)), For: ptr.To(monitoringv1.Duration("5m")), Labels: map[string]string{"severity": "warning"}, Annotations: map[string]string{"summary": "TADOSS HTTP error rate exceeds 1%; inspect admission and database waits"}},
			{Alert: "TADOSSDatabaseAdmissionWaiting", Expr: intstr.FromString(fmt.Sprintf(`max(tadoss_database_waiting{%s}) > 0`, selector)), For: ptr.To(monitoringv1.Duration("5m")), Labels: map[string]string{"severity": "warning"}, Annotations: map[string]string{"summary": "TADOSS requests are persistently waiting for database capacity"}},
			{Alert: "TADOSSWorkerBacklog", Expr: intstr.FromString(fmt.Sprintf(`max(tadoss_queue_oldest_seconds{%s}) > 60`, selector)), For: ptr.To(monitoringv1.Duration("5m")), Labels: map[string]string{"severity": "warning"}, Annotations: map[string]string{"summary": "TADOSS background jobs are not keeping up"}},
			{Alert: "TADOSSExporterUnavailable", Expr: intstr.FromString(fmt.Sprintf(`min(up{%s}) == 0`, selector)), For: ptr.To(monitoringv1.Duration("2m")), Labels: map[string]string{"severity": "warning"}, Annotations: map[string]string{"summary": "A TADOSS metrics target cannot be scraped"}},
		}}}},
	}
	rules.Labels = mergeStringMaps(t.Spec.Monitoring.Labels, rules.Labels)
	return append(result, rules)
}

func (r *TadossReconciler) reconcileMonitoring(ctx context.Context, t *tadossv1.Tadoss) {
	enabled := ptr.Deref(t.Spec.Monitoring.Enabled, false)
	for _, kind := range []string{"PodMonitor", "PrometheusRule"} {
		if _, err := r.RESTMapper().RESTMapping(schema.GroupKind{Group: monitoringv1.SchemeGroupVersion.Group, Kind: kind}, "v1"); err != nil {
			reason, message := "Disabled", "Monitoring integration is disabled"
			if enabled {
				reason, message = "APIsMissing", "Install Prometheus Operator CRDs; API service remains available"
			}
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "MonitoringReady", !enabled, reason, message)
			return
		}
	}
	desired := map[string]bool{}
	if enabled {
		for _, object := range monitoringObjects(t) {
			if err := r.apply(ctx, t, object); err != nil {
				operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "MonitoringReady", false, "IntegrationFailed", err.Error())
				return
			}
			desired[object.GetName()] = true
		}
	}
	for _, suffix := range []string{"-api", "-worker", "-database", "-pooler", "-alerts"} {
		if desired[t.Name+suffix] {
			continue
		}
		var empty client.Object = &monitoringv1.PodMonitor{}
		if suffix == "-alerts" {
			empty = &monitoringv1.PrometheusRule{}
		}
		object, err := r.owned(ctx, t, client.ObjectKey{Namespace: t.Namespace, Name: t.Name + suffix}, empty)
		if err == nil && object != nil {
			err = r.Delete(ctx, object)
		}
		if client.IgnoreNotFound(err) != nil {
			operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "MonitoringReady", false, "CleanupFailed", err.Error())
			return
		}
	}
	reason, message := "Disabled", "Monitoring integration is disabled"
	if enabled {
		reason, message = "Configured", "Scrape targets and rules configured; verify discovery and up in Prometheus"
	}
	operatorstatus.SetConditionBool(&t.Status.Conditions, t.Generation, "MonitoringReady", true, reason, message)
}
