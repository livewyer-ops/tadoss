package controller

import (
	"context"
	"reflect"
	"testing"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/event"
)

func TestWatchNamespaceScope(t *testing.T) {
	for _, raw := range []string{"", " , , "} {
		if scope := ParseWatchNamespaces(raw); scope != nil || !scope.Allows("anything") {
			t.Fatal("an empty namespace scope must watch every namespace")
		}
	}
	scope := ParseWatchNamespaces(" one, two,one, ")
	if len(scope) != 2 || !scope.Allows("one") || !scope.Allows("two") || scope.Allows("three") {
		t.Fatalf("incorrect namespace scope: %v", scope)
	}
	// The request must be rejected before a client is needed.
	r := &TadossReconciler{WatchNamespaces: scope}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: types.NamespacedName{Namespace: "three", Name: "example"}}); err != nil {
		t.Fatal(err)
	}
}

func TestPrimaryResourcePredicate(t *testing.T) {
	filter := primaryResourcePredicate([]string{"retry"})
	original := instance()
	original.Generation = 1
	for _, test := range []struct {
		name   string
		mutate func(*tadossv1.Tadoss)
		want   bool
	}{
		{"status only", func(obj *tadossv1.Tadoss) { obj.Status.Phase = tadossv1.PhaseReady }, false},
		{"resource version only", func(obj *tadossv1.Tadoss) { obj.ResourceVersion = "2" }, false},
		{"generation", func(obj *tadossv1.Tadoss) { obj.Generation++ }, true},
		{"finalizer", func(obj *tadossv1.Tadoss) { obj.Finalizers = []string{finalizer} }, true},
		{"deletion", func(obj *tadossv1.Tadoss) { now := metav1.Now(); obj.DeletionTimestamp = &now }, true},
		{"action", func(obj *tadossv1.Tadoss) { obj.Annotations = map[string]string{"retry": "1"} }, true},
		{"unrelated annotation", func(obj *tadossv1.Tadoss) { obj.Annotations = map[string]string{"other": "1"} }, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			changed := original.DeepCopy()
			test.mutate(changed)
			if got := filter.Update(event.UpdateEvent{ObjectOld: original, ObjectNew: changed}); got != test.want {
				t.Fatalf("got %v, want %v", got, test.want)
			}
		})
	}
	if !filter.Create(event.CreateEvent{Object: original}) || !filter.Delete(event.DeleteEvent{Object: original}) || !filter.Generic(event.GenericEvent{Object: original}) {
		t.Fatal("create, delete and explicit requests must pass")
	}
}

func TestCNPGPodRolloutReadiness(t *testing.T) {
	cluster := &cnpgv1.Cluster{ObjectMeta: metav1.ObjectMeta{Name: "db", Namespace: "test", UID: "database"}, Spec: cnpgv1.ClusterSpec{Instances: 1, ImageName: "postgres:desired"}}
	ready := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "db-1", Namespace: "test", Labels: map[string]string{"cnpg.io/cluster": "db"}, OwnerReferences: []metav1.OwnerReference{{APIVersion: cnpgv1.SchemeGroupVersion.String(), Kind: "Cluster", Name: "db", UID: cluster.UID, Controller: ptr.To(true)}}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: postgresContainerName, Image: cluster.Spec.ImageName}}},
		Status:     corev1.PodStatus{ContainerStatuses: []corev1.ContainerStatus{{Name: postgresContainerName, Ready: true}}},
	}
	for _, test := range []struct {
		name   string
		mutate func(*corev1.Pod)
		want   bool
	}{
		{"ready", func(*corev1.Pod) {}, true},
		{"old image", func(p *corev1.Pod) { p.Spec.Containers[0].Image = "postgres:old" }, false},
		{"not ready", func(p *corev1.Pod) { p.Status.ContainerStatuses[0].Ready = false }, false},
		{"backup pod", func(p *corev1.Pod) { p.OwnerReferences[0].UID = "backup-job" }, false},
		{"terminating", func(p *corev1.Pod) {
			now := metav1.NewTime(time.Now())
			p.DeletionTimestamp = &now
			p.Finalizers = []string{"test"}
		}, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			pod := ready.DeepCopy()
			test.mutate(pod)
			r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(pod).Build()}
			got, err := r.cnpgRolloutReady(context.Background(), cluster)
			if err != nil || got != test.want {
				t.Fatalf("got %v, %v; want %v", got, err, test.want)
			}
		})
	}
}

func TestReferenceIndexPreservesDependencies(t *testing.T) {
	cr := instance()
	cr.Spec.Auth.TokenSecret = "token"
	cr.Spec.Auth.BasicSecret = "basic"
	cr.Spec.Backends.DB.External.ExistingSecret = "external-db"
	cr.Spec.Backends.DB.External.CAConfigMap = &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "ca"}, Key: "cert"}
	cr.Spec.Worker.EnvFrom = []corev1.EnvFromSource{{SecretRef: &corev1.SecretEnvSource{LocalObjectReference: corev1.LocalObjectReference{Name: "extra"}}}}
	cr.Spec.API.Volumes = []corev1.Volume{{Name: "optional", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "optional", Optional: ptr.To(true)}}}}
	before := cr.DeepCopy()
	tadossReferenceIndexValue(cr)
	for _, name := range []string{"token", "basic", "external-db", "ca", "extra", "optional", cr.Name + suffixDBCA, cr.Name + suffixDBApp, cr.Name + suffixToken} {
		r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(cr.DeepCopy()).WithIndex(&tadossv1.Tadoss{}, tadossReferenceIndex, tadossReferenceIndexValue).Build()}
		requests := r.secretRequests(context.Background(), &metav1.PartialObjectMetadata{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: cr.Namespace}})
		if len(requests) != 1 || requests[0].NamespacedName != client.ObjectKeyFromObject(cr) {
			t.Fatalf("reference %q did not enqueue its instance", name)
		}
	}
	if !reflect.DeepEqual(before, cr) {
		t.Fatal("indexing changed the cached resource")
	}
}
