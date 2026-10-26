package controller

import (
	"context"
	"testing"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestReleaseSelectionPrecedesManagedChanges(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct{ name, current, pending, reason string }{
		{"install", "", "", "ReleasePending"},
		{"unsupported transition", "unknown", "", "UnsupportedUpgrade"},
		{"interrupted upgrade", "0.1.0", "other", "UpgradeInProgress"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cr := instance()
			cr.Finalizers = []string{finalizer}
			cr.Status.CurrentVersion, cr.Status.PendingVersion = tc.current, tc.pending
			scheme := testScheme(t)
			cli := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr).Build()
			r := &TadossReconciler{Client: cli, Scheme: scheme}
			if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(cr)}); err != nil {
				t.Fatal(err)
			}
			if err := cli.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
				t.Fatal(err)
			}
			condition := meta.FindStatusCondition(cr.Status.Conditions, tadossv1.ConditionReady)
			if condition == nil || condition.Reason != tc.reason {
				t.Fatalf("unexpected condition: %+v", condition)
			}
			if tc.reason == "ReleasePending" && cr.Status.PendingVersion != cr.Spec.Version {
				t.Fatal("target not persisted")
			}
			if cr.Status.CurrentVersion != tc.current {
				t.Fatal("currentVersion advanced before readiness")
			}
			clusters := &cnpgv1.ClusterList{}
			if err := cli.List(ctx, clusters); err != nil || len(clusters.Items) != 0 {
				t.Fatalf("database changed before release selection: %v", err)
			}
			if tc.reason == "ReleasePending" {
				// A replacement controller resumes from the persisted target.
				restarted := &TadossReconciler{Client: cli, Scheme: scheme}
				if _, err := restarted.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(cr)}); err != nil {
					t.Fatal(err)
				}
				if err := cli.List(ctx, clusters); err != nil || len(clusters.Items) != 1 {
					t.Fatalf("installation did not resume: %v", err)
				}
			}
		})
	}
}

func TestStaleGenerationCannotPersistReleaseTarget(t *testing.T) {
	ctx, scheme := context.Background(), testScheme(t)
	cr := instance()
	cr.Generation = 2
	cli := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr).Build()
	r := &TadossReconciler{Client: cli, Scheme: scheme}
	stale := cr.DeepCopy()
	stale.Generation = 1
	stale.Status.PendingVersion = "stale"
	if _, err := r.pending(ctx, stale, "ReleasePending", "waiting"); !apierrors.IsConflict(err) {
		t.Fatalf("expected conflict, got %v", err)
	}
	if err := cli.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
		t.Fatal(err)
	}
	if cr.Status.PendingVersion != "" {
		t.Fatal("stale target was persisted")
	}
}
