package controller

import (
	"context"
	"errors"
	"testing"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
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

func TestDeploymentReadyWaitsForOldReplicas(t *testing.T) {
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Generation: 2}, Status: appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 3, UpdatedReplicas: 2, AvailableReplicas: 2}}
	if deploymentReady(deployment, 2) {
		t.Fatal("old replica must block rollout completion")
	}
	deployment.Status.Replicas = 2
	if !deploymentReady(deployment, 2) {
		t.Fatal("complete rollout was not ready")
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
	cr.Spec.Auth.TokenSecret = "mine"
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

func TestReferenceFingerprintUsesOnlyObjectRevisions(t *testing.T) {
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "db", Namespace: "test", UID: "secret-uid", ResourceVersion: "1"}, Data: map[string][]byte{"uri": []byte("password-one")}}
	ca := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "ca", Namespace: "test", UID: "ca-uid", ResourceVersion: "2"}, Data: map[string]string{"ca.pem": "certificate"}}
	fingerprint := referenceFingerprint(secret, ca)
	if fingerprint != referenceFingerprint(ca, secret, secret) {
		t.Fatal("reference order or duplication changed fingerprint")
	}
	secret.Data["uri"] = []byte("password-two")
	ca.Data["ca.pem"] = "different certificate"
	if fingerprint != referenceFingerprint(secret, ca) {
		t.Fatal("fingerprint exposes a content verifier")
	}
	for _, object := range []client.Object{secret, ca} {
		before := referenceFingerprint(secret, ca)
		object.SetResourceVersion("3")
		if before == referenceFingerprint(secret, ca) {
			t.Fatal("rotation did not change fingerprint")
		}
	}
	cr := instance()
	job := schemaJob(cr, secret.Name, referenceFingerprint(secret, ca))
	ca.UID = "replacement-ca"
	if job.Name == schemaJob(cr, secret.Name, referenceFingerprint(secret, ca)).Name {
		t.Fatal("CA replacement must rerun migration")
	}
}

func TestRetainDetachesBackupAndPreservesOtherOwners(t *testing.T) {
	ctx, scheme := context.Background(), testScheme(t)
	cr := instance()
	cr.Finalizers = []string{finalizer}
	cr.Spec.DeletionProtection = ptr.To(false)
	cr.Spec.DeletionPolicy = tadossv1.DeletionPolicyRetain
	cluster := databaseCluster(cr)
	backup := &cnpgv1.ScheduledBackup{ObjectMeta: metadata(cr, "-backup")}
	for _, object := range []client.Object{cluster, backup} {
		if err := controllerutil.SetControllerReference(cr, object, scheme); err != nil {
			t.Fatal(err)
		}
		object.SetOwnerReferences(append(object.GetOwnerReferences(), metav1.OwnerReference{APIVersion: "v1", Kind: "ConfigMap", Name: "foreign", UID: "foreign-uid"}))
	}
	failBackupPatch := true
	cli := fake.NewClientBuilder().WithScheme(scheme).WithObjects(cr, cluster, backup).WithInterceptorFuncs(interceptor.Funcs{
		Patch: func(ctx context.Context, cli client.WithWatch, obj client.Object, patch client.Patch, options ...client.PatchOption) error {
			if _, ok := obj.(*cnpgv1.ScheduledBackup); ok && failBackupPatch {
				failBackupPatch = false
				return errors.New("transient patch failure")
			}
			return cli.Patch(ctx, obj, patch, options...)
		},
	}).Build()
	r := &TadossReconciler{Client: cli, Scheme: scheme}
	if _, err := r.handleDeletion(ctx, cr); err == nil {
		t.Fatal("backup patch failure must retain finalizer")
	}
	if err := r.Get(ctx, client.ObjectKeyFromObject(cr), cr); err != nil {
		t.Fatal(err)
	}
	if !controllerutil.ContainsFinalizer(cr, finalizer) {
		t.Fatal("finalizer released before detaching backup")
	}
	for range 2 {
		if _, err := r.handleDeletion(ctx, cr); err != nil {
			t.Fatal(err)
		}
		for _, object := range []client.Object{cluster, backup} {
			if err := r.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
				t.Fatal(err)
			}
			if owners := object.GetOwnerReferences(); len(owners) != 1 || owners[0].UID != "foreign-uid" {
				t.Fatalf("unexpected owners: %v", owners)
			}
		}
	}
}
