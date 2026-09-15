package controller

import (
	"context"
	"testing"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
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
			cr.Spec.DeletionProtection = true
			r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr, tc.namespace).Build(), Scheme: scheme}
			result, err := r.finalize(ctx, cr)
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
