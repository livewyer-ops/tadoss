package controller

import (
	"context"
	"testing"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{tadossv1.AddToScheme, corev1.AddToScheme, cnpgv1.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	return scheme
}

func TestFinalizeReleasesWhenNamespaceTerminates(t *testing.T) {
	ctx := context.Background()
	live := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "test"}}
	terminating := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "test", DeletionTimestamp: &metav1.Time{Time: time.Now()}, Finalizers: []string{"kubernetes"}}}
	for name, tc := range map[string]struct {
		namespace *corev1.Namespace
		released  bool
	}{
		"live namespace keeps a protected instance": {live, false},
		"terminating namespace releases it":         {terminating, true},
	} {
		t.Run(name, func(t *testing.T) {
			scheme := testScheme(t)
			cr := instance()
			cr.Finalizers = []string{finalizer}
			cr.Spec.DeletionProtection = ptr.To(true)
			r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr, tc.namespace).Build(), Scheme: scheme}
			result, err := r.handleDeletion(ctx, cr)
			if err != nil {
				t.Fatal(err)
			}
			current := &tadossv1.Tadoss{}
			if err := r.Get(ctx, client.ObjectKeyFromObject(cr), current); err != nil {
				t.Fatal(err)
			}
			if controllerutil.ContainsFinalizer(current, finalizer) == tc.released {
				t.Fatalf("finalizer present=%v, want released=%v", !tc.released, tc.released)
			}
			if !tc.released && result.RequeueAfter != time.Minute {
				t.Fatalf("blocked deletion requeued after %s, want 1m", result.RequeueAfter)
			}
			if tc.released && !result.IsZero() {
				t.Fatal("released instance must not requeue")
			}
		})
	}
}

func TestNamespaceRequestsEnqueueInstances(t *testing.T) {
	scheme := testScheme(t)
	cr := instance()
	r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr).Build(), Scheme: scheme}
	namespace := &metav1.PartialObjectMetadata{ObjectMeta: metav1.ObjectMeta{Name: "test"}}
	requests := r.namespaceRequests(context.Background(), namespace)
	if len(requests) != 1 || requests[0].NamespacedName != client.ObjectKeyFromObject(cr) {
		t.Fatalf("unexpected requests %v", requests)
	}
	if len(r.namespaceRequests(context.Background(), &metav1.PartialObjectMetadata{ObjectMeta: metav1.ObjectMeta{Name: "other"}})) != 0 {
		t.Fatal("unrelated namespace enqueued instances")
	}
}

func TestSecretRequestsAcceptMetadata(t *testing.T) {
	scheme := testScheme(t)
	cr := instance()
	r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr).Build(), Scheme: scheme}
	token := &metav1.PartialObjectMetadata{ObjectMeta: metav1.ObjectMeta{Name: "example-api-token", Namespace: "test"}}
	if requests := r.secretRequests(context.Background(), token); len(requests) != 1 || requests[0].NamespacedName != client.ObjectKeyFromObject(cr) {
		t.Fatalf("unexpected requests %v", requests)
	}
	unrelated := &metav1.PartialObjectMetadata{ObjectMeta: metav1.ObjectMeta{Name: "unrelated", Namespace: "test"}}
	if len(r.secretRequests(context.Background(), unrelated)) != 0 {
		t.Fatal("unrelated Secret enqueued instances")
	}
}

func TestMigrateDeploymentOrphansLegacySelector(t *testing.T) {
	ctx := context.Background()
	scheme := testScheme(t)
	if err := appsv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	cr := instance()
	desired := workloads(cr, "db", "token", "checksum").Deployment
	legacy := desired.DeepCopy()
	legacy.Spec.Selector = &metav1.LabelSelector{MatchLabels: labels(cr)}
	if err := controllerutil.SetControllerReference(cr, legacy, scheme); err != nil {
		t.Fatal(err)
	}
	r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr, legacy).Build(), Scheme: scheme}
	waiting, err := r.migrateDeployment(ctx, cr, desired)
	if err != nil || !waiting {
		t.Fatalf("legacy selector not migrated: waiting=%v err=%v", waiting, err)
	}
	if err := r.Get(ctx, client.ObjectKeyFromObject(desired), &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Fatalf("legacy Deployment still present: %v", err)
	}
	if waiting, err := r.migrateDeployment(ctx, cr, desired); err != nil || waiting {
		t.Fatal("a missing Deployment needs no migration")
	}
	terminating := desired.DeepCopy()
	terminating.DeletionTimestamp = &metav1.Time{Time: time.Now()}
	terminating.Finalizers = []string{"orphan"}
	if err := controllerutil.SetControllerReference(cr, terminating, scheme); err != nil {
		t.Fatal(err)
	}
	r = &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr, terminating).Build(), Scheme: scheme}
	if waiting, err := r.migrateDeployment(ctx, cr, desired); err != nil || !waiting {
		t.Fatal("must wait while the old Deployment is still terminating")
	}
	current := desired.DeepCopy()
	if err := controllerutil.SetControllerReference(cr, current, scheme); err != nil {
		t.Fatal(err)
	}
	r = &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr, current).Build(), Scheme: scheme}
	if waiting, err := r.migrateDeployment(ctx, cr, desired); err != nil || waiting {
		t.Fatal("a current selector must not be migrated")
	}
}

func TestTokenSelfHealsOnlyWhenGenerated(t *testing.T) {
	ctx := context.Background()
	scheme := testScheme(t)
	cr := instance()
	emptied := &corev1.Secret{ObjectMeta: metadata(cr, suffixToken), Data: map[string][]byte{"token": nil}}
	if err := controllerutil.SetControllerReference(cr, emptied, scheme); err != nil {
		t.Fatal(err)
	}
	r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr, emptied).Build(), Scheme: scheme}
	secret, err := r.token(ctx, cr)
	if err != nil || len(secret.Data["token"]) == 0 {
		t.Fatalf("emptied generated Secret not refilled: %v", err)
	}
	supplied := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "mine", Namespace: "test"}, Data: map[string][]byte{"token": nil}}
	cr.Spec.TokenSecret = "mine"
	r = &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr, supplied).Build(), Scheme: scheme}
	if secret, err := r.token(ctx, cr); err != nil || len(secret.Data["token"]) != 0 {
		t.Fatal("a user-supplied Secret must not be modified")
	}
}

func TestObservedGenerationAdvancesOnlyWhenSettled(t *testing.T) {
	ctx := context.Background()
	scheme := testScheme(t)
	cr := instance()
	cr.Generation = 3
	r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr).Build(), Scheme: scheme}
	current := func() tadossv1.TadossStatus {
		live := &tadossv1.Tadoss{}
		if err := r.Get(ctx, client.ObjectKeyFromObject(cr), live); err != nil {
			t.Fatal(err)
		}
		return live.Status
	}
	if _, err := r.pending(ctx, cr, "SchemaPending", "waiting"); err != nil {
		t.Fatal(err)
	}
	if status := current(); status.ObservedGeneration != 0 || status.Phase != tadossv1.PhaseProgressing {
		t.Fatalf("progressing must not advance observedGeneration: %+v", status)
	}
	if err := r.status(ctx, cr, tadossv1.PhaseReady, "Available", "ready", true); err != nil {
		t.Fatal(err)
	}
	if status := current(); status.ObservedGeneration != 3 || status.Phase != tadossv1.PhaseReady {
		t.Fatalf("ready must record the generation: %+v", status)
	}
}
