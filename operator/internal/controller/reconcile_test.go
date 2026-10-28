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
		Spec:       tadossv1.TadossSpec{Version: "0.1.0-rc1", Profile: tadossv1.ProfileLocalKind, API: tadossv1.APIWorkloadSpec{WorkloadSpec: tadossv1.WorkloadSpec{Image: "livewyer/tadoss-api:test"}}, DeletionProtection: ptr.To(protected)},
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
	cr.Spec.Worker.EnvFrom = []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "worker-settings"}, Optional: ptr.To(true)}}}
	if err := live.Create(ctx, cr); err != nil {
		t.Fatal(err)
	}
	cluster := &cnpgv1.Cluster{ObjectMeta: metav1.ObjectMeta{Name: "demo-db", Namespace: namespace}}
	eventually(t, "database cluster", func() bool { return live.Get(ctx, client.ObjectKeyFromObject(cluster), cluster) == nil })
	if cluster.Spec.Instances != 1 || !metav1.IsControlledBy(cluster, cr) {
		t.Fatalf("unexpected cluster %+v", cluster.Spec)
	}
	setStatus(t, cluster, func() { cluster.Status.ReadyInstances = 1 })
	databasePod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "demo-db-1", Namespace: namespace, Labels: map[string]string{"cnpg.io/cluster": cluster.Name}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: postgresContainerName, Image: cluster.Spec.ImageName}}},
	}
	if err := controllerutil.SetControllerReference(cluster, databasePod, live.Scheme()); err != nil {
		t.Fatal(err)
	}
	if err := live.Create(ctx, databasePod); err != nil {
		t.Fatal(err)
	}
	setStatus(t, databasePod, func() {
		databasePod.Status.ContainerStatuses = []corev1.ContainerStatus{{Name: postgresContainerName, Image: cluster.Spec.ImageName, ImageID: "test-image", Ready: true}}
	})
	app := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "demo-db-app", Namespace: namespace}, StringData: map[string]string{"uri": "postgresql://u:p@demo-db-rw:5432/tadoss"}}
	if err := live.Create(ctx, app); err != nil {
		t.Fatal(err)
	}
	eventually(t, "missing managed CA status", func() bool {
		if live.Get(ctx, client.ObjectKeyFromObject(cr), cr) != nil {
			return false
		}
		condition := meta.FindStatusCondition(cr.Status.Conditions, tadossv1.ConditionReady)
		return condition != nil && condition.Reason == "DatabaseCAMissing"
	})
	ca := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "demo-db-ca", Namespace: namespace}, Data: map[string][]byte{}}
	if err := live.Create(ctx, ca); err != nil {
		t.Fatal(err)
	}
	eventually(t, "empty managed CA rejected", func() bool {
		if live.Get(ctx, client.ObjectKeyFromObject(cr), cr) != nil {
			return false
		}
		condition := meta.FindStatusCondition(cr.Status.Conditions, tadossv1.ConditionReady)
		return condition != nil && condition.Reason == "DatabaseCAInvalid"
	})
	ca.Data = map[string][]byte{"ca.crt": []byte("trusted CA")}
	if err := live.Update(ctx, ca); err != nil {
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
	if env := job.Spec.Template.Spec.Containers[0].Env; len(env) != 3 || env[0].ValueFrom.SecretKeyRef.Name != "demo-db-app" || env[1].Value != "/etc/tadoss/database-ca/ca.pem" || env[2].Value != "0001_baseline" {
		t.Fatalf("schema job must receive database URI, CA and selected schema target: %+v", env)
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
	if cr.Status.ObservedGeneration != 0 || cr.Status.Phase != tadossv1.PhaseProgressing || cr.Status.PendingVersion != cr.Spec.Version || cr.Status.CurrentVersion != "" {
		t.Fatalf("progressing instance must not report its generation: %+v", cr.Status)
	}
	setStatus(t, deployment, func() {
		deployment.Status = appsv1.DeploymentStatus{ObservedGeneration: deployment.Generation, Replicas: 1, UpdatedReplicas: 1, ReadyReplicas: 1, AvailableReplicas: 1}
	})
	worker := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "demo-worker", Namespace: namespace}}
	eventually(t, "worker deployment", func() bool { return live.Get(ctx, client.ObjectKeyFromObject(worker), worker) == nil })
	setStatus(t, worker, func() {
		worker.Status = appsv1.DeploymentStatus{ObservedGeneration: worker.Generation, Replicas: 1, UpdatedReplicas: 1, ReadyReplicas: 1, AvailableReplicas: 1}
	})
	eventually(t, "ready status", func() bool {
		return live.Get(ctx, client.ObjectKeyFromObject(cr), cr) == nil && cr.Status.Phase == tadossv1.PhaseReady
	})
	if cr.Status.PendingVersion != "" || cr.Status.CurrentVersion != cr.Spec.Version || cr.Status.ObservedGeneration != cr.Generation || cr.Status.Endpoint != "http://demo-api."+namespace+".svc:8000" || cr.Status.TokenSecret != "demo-api-token" {
		t.Fatalf("unexpected ready status %+v", cr.Status)
	}
	if err := live.Get(ctx, client.ObjectKey{Namespace: namespace, Name: "demo-api"}, &corev1.Service{}); err != nil {
		t.Fatal(err)
	}
	if err := live.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
		t.Fatal(err)
	}
	settings := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "worker-settings", Namespace: namespace}, Data: map[string][]byte{"TADOSS_WEBHOOK_ALLOWED_HOSTS": []byte("initial.example")}}
	for _, action := range []string{"create", "update", "delete"} {
		oldFingerprint := deployment.Spec.Template.Annotations["checksum/credentials"]
		var err error
		switch action {
		case "create":
			err = live.Create(ctx, settings)
		case "update":
			settings.Data["TADOSS_WEBHOOK_ALLOWED_HOSTS"] = []byte("rotated.example")
			err = live.Update(ctx, settings)
		case "delete":
			err = live.Delete(ctx, settings)
		}
		if err != nil {
			t.Fatal(err)
		}
		eventually(t, "optional Secret "+action+" rolls workloads", func() bool {
			if live.Get(ctx, client.ObjectKeyFromObject(deployment), deployment) != nil || live.Get(ctx, client.ObjectKeyFromObject(worker), worker) != nil {
				return false
			}
			return deployment.Spec.Template.Annotations["checksum/credentials"] != oldFingerprint && worker.Spec.Template.Annotations["checksum/credentials"] == deployment.Spec.Template.Annotations["checksum/credentials"]
		})
		if err := live.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil || cr.Status.SchemaJob != job.Name {
			t.Fatalf("optional worker Secret must not rerun schema migration: %v", err)
		}
	}
	oldJob, oldFingerprint := job.Name, deployment.Spec.Template.Annotations["checksum/credentials"]
	ca.Data["ca.crt"] = []byte("rotated CA")
	if err := live.Update(ctx, ca); err != nil {
		t.Fatal(err)
	}
	eventually(t, "CA rotation selects new schema Job", func() bool {
		if live.Get(ctx, client.ObjectKeyFromObject(cr), cr) != nil || cr.Status.SchemaJob == oldJob {
			return false
		}
		return live.Get(ctx, client.ObjectKey{Namespace: namespace, Name: cr.Status.SchemaJob}, &job) == nil
	})
	setStatus(t, &job, func() {
		now := metav1.Now()
		job.Status.StartTime, job.Status.CompletionTime, job.Status.Succeeded = &now, &now, 1
		job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobSuccessCriteriaMet, Status: corev1.ConditionTrue}, {Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
	})
	eventually(t, "CA rotation rolls API and worker", func() bool {
		if live.Get(ctx, client.ObjectKeyFromObject(deployment), deployment) != nil || live.Get(ctx, client.ObjectKeyFromObject(worker), worker) != nil {
			return false
		}
		return deployment.Spec.Template.Annotations["checksum/credentials"] != oldFingerprint && worker.Spec.Template.Annotations["checksum/credentials"] == deployment.Spec.Template.Annotations["checksum/credentials"]
	})
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
			cr.Spec.Ingress = tadossv1.IngressSpec{Host: "Invalid_Host"}
		}, "host"},
		"external without secret": {func(cr *tadossv1.Tadoss) {
			cr.Spec.Backends.DB = tadossv1.DatabaseBackendSpec{ProvidedBy: tadossv1.ProviderExternal}
		}, "external PostgreSQL requires existingSecret"},
		"managed with secret": {func(cr *tadossv1.Tadoss) {
			cr.Spec.Backends.DB = tadossv1.DatabaseBackendSpec{ProvidedBy: "invalid"}
		}, "Unsupported value"},
		"no authentication": {func(cr *tadossv1.Tadoss) { cr.Spec.Auth.TokenEnabled = ptr.To(false) }, "disabling query tokens requires"},
		"incomplete oauth":  {func(cr *tadossv1.Tadoss) { cr.Spec.Auth.OAuth2.Issuer = "https://issuer.example" }, "supplied together"},
		"insecure oauth": {func(cr *tadossv1.Tadoss) {
			cr.Spec.Auth.OAuth2 = tadossv1.OAuth2Spec{Issuer: "http://issuer.example", Audience: "tadoss", JWKSURI: "https://issuer.example/jwks"}
		}, "https://"},
		"symmetric oauth":   {func(cr *tadossv1.Tadoss) { cr.Spec.Auth.OAuth2.Algorithms = []string{"HS256"} }, "Unsupported value"},
		"token secret name": {func(cr *tadossv1.Tadoss) { cr.Spec.Auth.TokenSecret = "Not_Valid" }, "should match"},
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
			"spec":     map[string]any{"version": "0.1.0-rc1", "ingress": map[string]any{"className": ""}},
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
		if cr.Spec.DeletionProtection == nil || !*cr.Spec.DeletionProtection || cr.Spec.Backends.DB.ProvidedBy != tadossv1.ProviderManaged {
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

func TestTypedCredentialReadsDoNotCreateFullDataCaches(t *testing.T) {
	requireEnvtest(t)
	ctx, namespace := context.Background(), newNamespace(t)
	for _, object := range []client.Object{
		&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "ca", Namespace: namespace}, Data: map[string]string{"ca.pem": "certificate"}},
		&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "credentials", Namespace: namespace}, Data: map[string][]byte{"token": []byte("secret")}},
	} {
		if err := live.Create(ctx, object); err != nil {
			t.Fatal(err)
		}
		if err := live.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
			t.Fatal(err)
		}
	}
	if fullDataLists.Load() != 0 {
		t.Fatal("typed reads started a full Secret or ConfigMap informer")
	}
}
