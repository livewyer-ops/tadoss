package controller

import (
	"context"
	"testing"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/controller/defaults"
	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	appsv1 "k8s.io/api/apps/v1"
	autoscalingv1 "k8s.io/api/autoscaling/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestAutoscalingReleasesReplicasWithoutReset(t *testing.T) {
	requireEnvtest(t)
	ctx := context.Background()
	cr := instance()
	cr.Namespace = newNamespace(t)
	cr.Spec.API.ReplicaCount = ptr.To(int32(4))
	desired := func() *appsv1.Deployment { return workloads(cr, "db", "token", "test").Deployment }
	if _, err := applyManagedObject(ctx, direct, desired()); err != nil {
		t.Fatal(err)
	}
	cr.Spec.API.ReplicaCount = nil
	cr.Spec.API.Autoscaling.Enabled = true
	if err := defaults.Resolve(cr, defaults.Installation{}); err != nil {
		t.Fatal(err)
	}
	d := desired()
	if _, err := applyManagedObject(ctx, direct, d); err != nil {
		t.Fatal(err)
	}
	if *d.Spec.Replicas != 4 {
		t.Fatalf("HPA transition reset replicas to %d", *d.Spec.Replicas)
	}
	scale := &autoscalingv1.Scale{}
	if err := direct.SubResource("scale").Get(ctx, d, scale); err != nil {
		t.Fatal(err)
	}
	scale.Spec.Replicas = 6
	if err := direct.SubResource("scale").Update(ctx, d, client.WithSubResourceBody(scale), client.FieldOwner("horizontal-pod-autoscaler")); err != nil {
		t.Fatal(err)
	}
	for range 3 {
		d = desired()
		if _, err := applyManagedObject(ctx, direct, d); err != nil {
			t.Fatal(err)
		}
		if *d.Spec.Replicas != 6 {
			t.Fatal("operator fought HPA scaling")
		}
	}
	cr.Spec.API.Autoscaling.Enabled = false
	cr.Spec.API.ReplicaCount = ptr.To(int32(3))
	d = desired()
	if _, err := applyManagedObject(ctx, direct, d); err != nil {
		t.Fatal(err)
	}
	if *d.Spec.Replicas != 3 {
		t.Fatal("manual replica ownership not restored")
	}
}

func TestHighLoadRoutingAndMonitoring(t *testing.T) {
	cr := instance()
	cr.Spec.Profile = tadossv1.ProfileMultiServer
	cr.Spec.Backends.DB.CNPG.Pooler.Enabled = true
	cr.Spec.API.Autoscaling.Enabled = true
	if err := defaults.Resolve(cr, defaults.Installation{Monitoring: tadossv1.MonitoringSpec{Enabled: ptr.To(true), Labels: map[string]string{"release": "shared"}}}); err != nil {
		t.Fatal(err)
	}
	cluster := databaseCluster(cr)
	if !cluster.Spec.PostgresConfiguration.Synchronous.FailoverQuorum || cluster.Spec.PostgresConfiguration.Synchronous.DataDurability != cnpgv1.DataDurabilityLevelRequired {
		t.Fatal("missing durability")
	}
	if cluster.Spec.Affinity.PodAntiAffinityType != cnpgv1.PodAntiAffinityTypeRequired {
		t.Fatal("database replicas must spread across nodes")
	}
	p := databasePooler(cr)
	if p.Spec.PgBouncer.PoolMode != cnpgv1.PgBouncerPoolModeTransaction || p.Spec.PgBouncer.Parameters["max_db_connections"] != "16" {
		t.Fatal("unbounded or session pool")
	}
	set := workloads(cr, "db", "token", "test")
	env := func(c corev1.Container, name string) corev1.EnvVar {
		for _, e := range c.Env {
			if e.Name == name {
				return e
			}
		}
		return corev1.EnvVar{}
	}
	api := set.Deployment.Spec.Template.Spec.Containers[0]
	if set.Deployment.Spec.Replicas != nil || env(api, "TADOSS_DATABASE_HOST").Value != "example-pooler.test.svc" {
		t.Fatal("API must use HPA and Pooler")
	}
	if env(api, "TADOSS_METRICS_TOKEN").ValueFrom.SecretKeyRef.Name != "example-metrics" {
		t.Fatal("metrics credential missing")
	}
	for _, c := range []corev1.Container{set.Worker.Spec.Template.Spec.Containers[0], schemaJob(cr, "db", "test").Spec.Template.Spec.Containers[0]} {
		if env(c, "TADOSS_DATABASE_HOST").Value != "" || env(c, "TADOSS_METRICS_TOKEN").Name != "" {
			t.Fatal("worker and migration must connect directly without scrape credentials")
		}
	}
	for _, obj := range monitoringObjects(cr) {
		if obj.GetLabels()["release"] != "shared" {
			t.Fatal("shared Prometheus discovery labels missing")
		}
	}
	// Monitoring CRDs can be absent while the API remains usable.
	requireEnvtest(t)
	r := &TadossReconciler{Client: direct, Scheme: direct.Scheme()}
	r.reconcileMonitoring(context.Background(), cr)
	for _, c := range cr.Status.Conditions {
		if c.Type == "MonitoringReady" && (c.Status != metav1.ConditionFalse || c.Reason != "APIsMissing") {
			t.Fatal(c)
		}
	}
}

func TestMonitoringAddedAfterOperatorStart(t *testing.T) {
	requireEnvtest(t)
	ctx := context.Background()
	cr := newInstance(newNamespace(t), "late-monitoring", false)
	cr.Spec.Paused = true
	cr.Spec.Monitoring.Enabled = ptr.To(true)
	if err := direct.Create(ctx, cr); err != nil {
		t.Fatal(err)
	}
	r := &TadossReconciler{Client: direct, Scheme: direct.Scheme()}
	r.reconcileMonitoring(ctx, cr)
	for _, definition := range []struct{ plural, kind string }{{"podmonitors", "PodMonitor"}, {"prometheusrules", "PrometheusRule"}} {
		object := &unstructured.Unstructured{Object: map[string]interface{}{
			"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
			"metadata": map[string]interface{}{"name": definition.plural + ".monitoring.coreos.com"},
			"spec": map[string]interface{}{"group": "monitoring.coreos.com", "scope": "Namespaced",
				"names": map[string]interface{}{"plural": definition.plural, "kind": definition.kind},
				"versions": []interface{}{map[string]interface{}{"name": "v1", "served": true, "storage": true,
					"schema": map[string]interface{}{"openAPIV3Schema": map[string]interface{}{"type": "object", "x-kubernetes-preserve-unknown-fields": true}},
				}},
			},
		}}
		if err := direct.Create(ctx, object); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if err := direct.Delete(ctx, object); err != nil {
				t.Error(err)
			}
		})
	}
	eventually(t, "monitoring APIs discovered without restarting operator", func() bool {
		r.reconcileMonitoring(ctx, cr)
		monitor := &monitoringv1.PodMonitor{}
		return direct.Get(ctx, client.ObjectKey{Namespace: cr.Namespace, Name: cr.Name + "-api"}, monitor) == nil && metav1.IsControlledBy(monitor, cr)
	})
	secret, err := r.tokenSecret(ctx, cr, cr.Name+"-metrics", true)
	if err != nil {
		t.Fatal(err)
	}
	fingerprint := referenceFingerprint(secret)
	secret.Data["token"] = []byte("rotated-scrape-token")
	if err := direct.Update(ctx, secret); err != nil {
		t.Fatal(err)
	}
	rotated, err := r.tokenSecret(ctx, cr, secret.Name, true)
	if err != nil || string(rotated.Data["token"]) != "rotated-scrape-token" || referenceFingerprint(rotated) == fingerprint {
		t.Fatal("scrape rotation must persist and roll workloads", err)
	}
	cr.Spec.Monitoring.Enabled = ptr.To(false)
	r.reconcileMonitoring(ctx, cr)
	if err := direct.Get(ctx, client.ObjectKey{Namespace: cr.Namespace, Name: cr.Name + "-api"}, &monitoringv1.PodMonitor{}); err == nil {
		t.Fatal("disabled monitor not removed")
	}
}
