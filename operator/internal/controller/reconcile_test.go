package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func newNamespace(t *testing.T) string {
	t.Helper()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{GenerateName: "tadoss-"}}
	if err := live.Create(context.Background(), ns); err != nil {
		t.Fatal(err)
	}
	return ns.Name
}

func newInstance(namespace, name string, protected bool) *tadossv1.Tadoss {
	return &tadossv1.Tadoss{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
		Spec:       tadossv1.TadossSpec{Profile: tadossv1.ProfileLocalKind, Image: "livewyer/tadoss-api:test", DeletionProtection: ptr.To(protected)},
	}
}

func setStatus(t *testing.T, obj client.Object, mutate func()) {
	t.Helper()
	var last error
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		if last = live.Get(context.Background(), client.ObjectKeyFromObject(obj), obj); last == nil {
			mutate()
			if last = live.Status().Update(context.Background(), obj); last == nil {
				return
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatalf("status update of %s failed: %v", obj.GetName(), last)
}

func TestReconcileManagedInstance(t *testing.T) {
	requireEnvtest(t)
	ctx := context.Background()
	namespace := newNamespace(t)
	cr := newInstance(namespace, "demo", false)
	if err := live.Create(ctx, cr); err != nil {
		t.Fatal(err)
	}
	cluster := &cnpgv1.Cluster{ObjectMeta: metav1.ObjectMeta{Name: "demo-db", Namespace: namespace}}
	eventually(t, "database cluster", func() bool { return live.Get(ctx, client.ObjectKeyFromObject(cluster), cluster) == nil })
	if cluster.Spec.Instances != 1 || !metav1.IsControlledBy(cluster, cr) {
		t.Fatalf("unexpected cluster %+v", cluster.Spec)
	}
	setStatus(t, cluster, func() { cluster.Status.ReadyInstances = 1 })
	app := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "demo-db-app", Namespace: namespace}, StringData: map[string]string{"uri": "postgresql://u:p@demo-db-rw:5432/tadoss"}}
	if err := live.Create(ctx, app); err != nil {
		t.Fatal(err)
	}
	var job batchv1.Job
	eventually(t, "schema job", func() bool {
		jobs := &batchv1.JobList{}
		if err := live.List(ctx, jobs, client.InNamespace(namespace)); err != nil || len(jobs.Items) != 1 {
			return false
		}
		job = jobs.Items[0]
		return true
	})
	if env := job.Spec.Template.Spec.Containers[0].Env; len(env) != 1 || env[0].ValueFrom.SecretKeyRef.Name != "demo-db-app" {
		t.Fatalf("schema job must only receive the database URI: %+v", env)
	}
	setStatus(t, &job, func() {
		now := metav1.Now()
		job.Status.StartTime, job.Status.CompletionTime, job.Status.Succeeded = &now, &now, 1
		job.Status.Conditions = []batchv1.JobCondition{
			{Type: batchv1.JobSuccessCriteriaMet, Status: corev1.ConditionTrue},
			{Type: batchv1.JobComplete, Status: corev1.ConditionTrue},
		}
	})
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "demo-api", Namespace: namespace}}
	eventually(t, "api deployment", func() bool { return live.Get(ctx, client.ObjectKeyFromObject(deployment), deployment) == nil })
	if _, uid := deployment.Spec.Selector.MatchLabels[instanceUID]; uid {
		t.Fatal("selector must not carry the instance UID")
	}
	token := &corev1.Secret{}
	if err := live.Get(ctx, client.ObjectKey{Namespace: namespace, Name: "demo-api-token"}, token); err != nil || len(token.Data["token"]) != 43 {
		t.Fatalf("generated token missing or malformed: %v", err)
	}
	if err := live.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
		t.Fatal(err)
	}
	if cr.Status.ObservedGeneration != 0 || cr.Status.Phase != tadossv1.PhaseProgressing {
		t.Fatalf("progressing instance must not report its generation: %+v", cr.Status)
	}
	setStatus(t, deployment, func() {
		deployment.Status = appsv1.DeploymentStatus{ObservedGeneration: deployment.Generation, Replicas: 1, UpdatedReplicas: 1, ReadyReplicas: 1, AvailableReplicas: 1}
	})
	eventually(t, "ready status", func() bool {
		return live.Get(ctx, client.ObjectKeyFromObject(cr), cr) == nil && cr.Status.Phase == tadossv1.PhaseReady
	})
	if cr.Status.ObservedGeneration != cr.Generation || cr.Status.Endpoint != "http://demo-api."+namespace+".svc:8000" || cr.Status.TokenSecret != "demo-api-token" {
		t.Fatalf("unexpected ready status %+v", cr.Status)
	}
	if err := live.Get(ctx, client.ObjectKey{Namespace: namespace, Name: "demo-api"}, &corev1.Service{}); err != nil {
		t.Fatal(err)
	}
	if err := live.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
		t.Fatal(err)
	}
	if err := live.Delete(ctx, cr); err != nil {
		t.Fatal(err)
	}
	eventually(t, "instance removal", func() bool {
		return apierrors.IsNotFound(live.Get(ctx, client.ObjectKeyFromObject(cr), &tadossv1.Tadoss{}))
	})
	if err := live.Get(ctx, client.ObjectKeyFromObject(cluster), cluster); err != nil || len(cluster.OwnerReferences) != 0 {
		t.Fatalf("Retain must detach the database cluster: %v", err)
	}
}

func TestAdmissionRules(t *testing.T) {
	requireEnvtest(t)
	ctx := context.Background()
	namespace := newNamespace(t)
	cases := map[string]struct {
		mutate  func(*tadossv1.Tadoss)
		message string
	}{
		"issuer without tls secret": {func(cr *tadossv1.Tadoss) {
			cr.Spec.Ingress = tadossv1.IngressSpec{Host: "api.example.com", ClusterIssuer: "issuer"}
		}, "ingress.clusterIssuer requires ingress.tlsSecretName"},
		"external without secret": {func(cr *tadossv1.Tadoss) {
			cr.Spec.Database = tadossv1.DatabaseSpec{Provider: tadossv1.ProviderExternal}
		}, "external PostgreSQL requires database.existingSecret"},
		"managed with secret": {func(cr *tadossv1.Tadoss) {
			cr.Spec.Database = tadossv1.DatabaseSpec{Provider: tadossv1.ProviderManaged, ExistingSecret: "creds"}
		}, "applies only to provider external"},
		"token secret name": {func(cr *tadossv1.Tadoss) { cr.Spec.TokenSecret = "Not_Valid" }, "should match"},
		"name length":       {func(cr *tadossv1.Tadoss) { cr.Name = strings.Repeat("a", 41) }, "at most 40 characters"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			cr := newInstance(namespace, "invalid", false)
			tc.mutate(cr)
			err := live.Create(ctx, cr)
			if err == nil || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("expected rejection containing %q, got %v", tc.message, err)
			}
		})
	}
	t.Run("empty class name", func(t *testing.T) {
		obj := &unstructured.Unstructured{Object: map[string]any{
			"apiVersion": "tadoss.livewyer.io/v1alpha1", "kind": "Tadoss",
			"metadata": map[string]any{"name": "invalid", "namespace": namespace},
			"spec":     map[string]any{"ingress": map[string]any{"className": ""}},
		}}
		if err := live.Create(ctx, obj); err == nil || !strings.Contains(err.Error(), "className") {
			t.Fatalf("empty className accepted: %v", err)
		}
	})
	t.Run("defaults", func(t *testing.T) {
		cr := newInstance(namespace, "valid", false)
		cr.Spec.DeletionProtection = nil
		if err := live.Create(ctx, cr); err != nil {
			t.Fatal(err)
		}
		if cr.Spec.DeletionProtection == nil || !*cr.Spec.DeletionProtection || cr.Spec.Database.Provider != tadossv1.ProviderManaged || cr.Spec.Database.Image != tadossv1.DefaultPostgresImage {
			t.Fatalf("defaults not applied: %+v", cr.Spec)
		}
	})
}

func TestDeletionProtectionAndNamespaceTermination(t *testing.T) {
	requireEnvtest(t)
	ctx := context.Background()
	namespace := newNamespace(t)
	cr := newInstance(namespace, "protected", true)
	if err := live.Create(ctx, cr); err != nil {
		t.Fatal(err)
	}
	eventually(t, "finalizer", func() bool {
		return live.Get(ctx, client.ObjectKeyFromObject(cr), cr) == nil && controllerutil.ContainsFinalizer(cr, finalizer)
	})
	if err := live.Delete(ctx, cr); err != nil {
		t.Fatal(err)
	}
	eventually(t, "blocked deletion status", func() bool {
		if err := live.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
			return false
		}
		condition := meta.FindStatusCondition(cr.Status.Conditions, tadossv1.ConditionReady)
		return condition != nil && condition.Reason == "DeletionBlocked"
	})
	time.Sleep(2 * time.Second)
	if err := live.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil || !controllerutil.ContainsFinalizer(cr, finalizer) {
		t.Fatalf("protected instance was released: %v", err)
	}
	// envtest runs no namespace controller, so a deleted namespace stays Terminating,
	// which is exactly the state the finalizer must yield to.
	if err := live.Delete(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatal(err)
	}
	eventually(t, "release on namespace termination", func() bool {
		return apierrors.IsNotFound(live.Get(ctx, client.ObjectKeyFromObject(cr), &tadossv1.Tadoss{}))
	})
}
