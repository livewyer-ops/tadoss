package controller

import (
	"context"
	"testing"

	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestOptionalSecretLifecycleChangesWorkloadFingerprint(t *testing.T) {
	for _, source := range []string{"envFrom", "volume"} {
		t.Run(source, func(t *testing.T) {
			ctx, scheme := context.Background(), testScheme(t)
			cr := instance()
			cr.Spec.Backends.DB.ProvidedBy = tadossv1.ProviderExternal
			cr.Spec.Auth.TokenSecret = "token"
			if source == "envFrom" {
				cr.Spec.Worker.EnvFrom = []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "settings"}, Optional: ptr.To(true)}}}
			} else {
				cr.Spec.API.Volumes = []corev1.Volume{{Name: "settings", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "settings", Optional: ptr.To(true)}}}}
			}
			database := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "db", Namespace: cr.Namespace}}
			token := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "token", Namespace: cr.Namespace}, Data: map[string][]byte{"token": []byte("test-token")}}
			secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "settings", Namespace: cr.Namespace}, Data: map[string][]byte{"setting": []byte("initial")}}
			r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr, database, token).WithIndex(&tadossv1.Tadoss{}, tadossReferenceIndex, tadossReferenceIndexValue).Build(), Scheme: scheme}
			fingerprint := func() string {
				t.Helper()
				credentials, _, err := r.reconcileCredentials(ctx, cr, database)
				if err != nil || credentials == nil {
					t.Fatalf("optional Secret blocked reconciliation: %v", err)
				}
				return referenceFingerprint(credentials.workloadReferences...)
			}
			absent := fingerprint()
			previous := absent
			for _, action := range []string{"create", "update", "delete"} {
				var err error
				switch action {
				case "create":
					err = r.Create(ctx, secret)
				case "update":
					secret.Data["setting"] = []byte("rotated")
					err = r.Update(ctx, secret)
				case "delete":
					err = r.Delete(ctx, secret)
				}
				if err != nil {
					t.Fatal(err)
				}
				metadata := &metav1.PartialObjectMetadata{ObjectMeta: secret.ObjectMeta}
				if requests := r.secretRequests(ctx, metadata); len(requests) != 1 || requests[0].NamespacedName != client.ObjectKeyFromObject(cr) {
					t.Errorf("%s did not enqueue the instance: %v", action, requests)
				}
				current := fingerprint()
				if current == previous {
					t.Errorf("%s did not change the workload fingerprint", action)
				}
				previous = current
			}
			if previous != absent {
				t.Fatal("deleted optional Secret must restore the absent fingerprint")
			}
		})
	}
}

func TestRequiredSecretReferenceWinsOverOptionalReference(t *testing.T) {
	for _, requiredSource := range []string{"envFrom", "volume", "auth"} {
		t.Run(requiredSource, func(t *testing.T) {
			ctx, scheme := context.Background(), testScheme(t)
			cr := instance()
			cr.Spec.Backends.DB.ProvidedBy = tadossv1.ProviderExternal
			cr.Spec.Auth.TokenSecret = "token"
			cr.Spec.API.EnvFrom = []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "settings"}, Optional: ptr.To(requiredSource != "envFrom")}}}
			cr.Spec.Worker.Volumes = []corev1.Volume{{Name: "settings", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "settings", Optional: ptr.To(requiredSource != "volume")}}}}
			if requiredSource == "auth" {
				cr.Spec.Auth.BasicSecret = "settings"
			}
			database := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "db", Namespace: cr.Namespace}}
			token := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "token", Namespace: cr.Namespace}, Data: map[string][]byte{"token": []byte("test-token")}}
			r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr, token).Build(), Scheme: scheme}
			credentials, result, err := r.reconcileCredentials(ctx, cr, database)
			if err != nil || credentials != nil || result.RequeueAfter == 0 {
				t.Fatalf("missing required Secret must block reconciliation: credentials=%v, result=%v, err=%v", credentials != nil, result, err)
			}
		})
	}
}
