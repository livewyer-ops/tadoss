package controller

import (
	"context"
	"testing"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	policyv1 "k8s.io/api/policy/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/util/intstr"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func TestPruneWorkerBudgetUsesWorkerConfiguration(t *testing.T) {
	for _, tc := range []struct {
		name     string
		profile  string
		api      int32
		worker   *int32
		minimum  *intstr.IntOrString
		replicas int32
		keep     bool
	}{
		{"default multi-server workers", tadossv1.ProfileMultiServer, 1, nil, nil, 2, true},
		{"default local workers", tadossv1.ProfileLocalKind, 2, nil, nil, 1, false},
		{"explicit worker replicas", tadossv1.ProfileLocalKind, 1, ptr.To(int32(2)), nil, 2, true},
		{"explicit worker budget", tadossv1.ProfileLocalKind, 1, nil, ptr.To(intstr.FromInt32(1)), 1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, scheme := context.Background(), testScheme(t)
			if err := clientgoscheme.AddToScheme(scheme); err != nil {
				t.Fatal(err)
			}
			cr := instance()
			cr.Spec.Profile = tc.profile
			cr.Spec.API.ReplicaCount = ptr.To(tc.api)
			cr.Spec.Worker.ReplicaCount = tc.worker
			cr.Spec.Worker.MinAvailable = tc.minimum
			desired := workloads(cr, "db", "token", "checksum")
			if *desired.Worker.Spec.Replicas != tc.replicas || (desired.WorkerBudget != nil) != tc.keep {
				t.Fatal("unexpected worker deployment or budget")
			}
			budget := &policyv1.PodDisruptionBudget{ObjectMeta: metadata(cr, "-worker")}
			if err := controllerutil.SetControllerReference(cr, budget, scheme); err != nil {
				t.Fatal(err)
			}
			r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(budget).Build(), Scheme: scheme}
			for range 2 {
				if err := r.prune(ctx, cr, "current-job", desired); err != nil {
					t.Fatal(err)
				}
				err := r.Get(ctx, client.ObjectKeyFromObject(budget), &policyv1.PodDisruptionBudget{})
				if (tc.keep && err != nil) || (!tc.keep && !apierrors.IsNotFound(err)) {
					t.Fatalf("keep worker budget=%v, get error=%v", tc.keep, err)
				}
			}
		})
	}
}
