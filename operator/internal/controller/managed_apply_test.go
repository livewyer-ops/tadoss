package controller

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestManagedApplyConvergesAndPreservesServiceAllocations(t *testing.T) {
	requireEnvtest(t)
	ctx, namespace := context.Background(), newNamespace(t)
	desired := func() *corev1.Service {
		return &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: namespace}, Spec: corev1.ServiceSpec{Selector: map[string]string{"app": "api"}, Ports: []corev1.ServicePort{{Name: "http", Port: 8000, TargetPort: intstr.FromString("http")}}}}
	}
	result, err := applyManagedObject(ctx, direct, desired())
	if err != nil || !result.Created || !result.Changed {
		t.Fatalf("create: %+v, %v", result, err)
	}
	result, err = applyManagedObject(ctx, direct, desired())
	if err != nil || result.Created || result.Changed {
		t.Fatalf("repeat apply: %+v, %v", result, err)
	}
	actual := &corev1.Service{}
	key := client.ObjectKeyFromObject(desired())
	if err := direct.Get(ctx, key, actual); err != nil {
		t.Fatal(err)
	}
	allocated := actual.Spec.ClusterIP
	if allocated == "" {
		t.Fatal("Service has no allocated cluster IP")
	}
	patch := []byte(`[{"op":"add","path":"/spec/ports/-","value":{"name":"drift","port":9999,"protocol":"TCP","targetPort":"http"}},{"op":"add","path":"/metadata/annotations","value":{"example.com/keep":"yes"}}]`)
	if err := direct.Patch(ctx, actual, client.RawPatch(types.JSONPatchType, patch), client.FieldOwner("kubectl-patch")); err != nil {
		t.Fatal(err)
	}
	result, err = applyManagedObject(ctx, direct, desired())
	if err != nil || !result.Changed {
		t.Fatalf("drift apply: %+v, %v", result, err)
	}
	if err := direct.Get(ctx, key, actual); err != nil {
		t.Fatal(err)
	}
	if len(actual.Spec.Ports) != 1 || actual.Spec.ClusterIP != allocated || actual.Annotations["example.com/keep"] != "yes" {
		t.Fatalf("failed to remove drift while retaining allocation and annotation: %+v", actual)
	}
	result, err = applyManagedObject(ctx, direct, desired())
	if err != nil || result.Changed {
		t.Fatalf("repaired object did not converge: %+v, %v", result, err)
	}
}

func TestManagedApplyReclaimsUpdatesAndPreservesStatus(t *testing.T) {
	requireEnvtest(t)
	ctx, namespace := context.Background(), newNamespace(t)
	desired := func() *appsv1.Deployment {
		return &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "api", Namespace: namespace}, Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)), Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "api"}},
			Template: corev1.PodTemplateSpec{ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{"app": "api"}}, Spec: corev1.PodSpec{Containers: []corev1.Container{{Name: "api", Image: "example:desired"}}}},
		}}
	}
	created := desired()
	created.Spec.Template.Spec.Containers[0].Env = []corev1.EnvVar{{Name: "UNWANTED", Value: "value"}}
	if err := direct.Create(ctx, created, client.FieldOwner("manager")); err != nil {
		t.Fatal(err)
	}
	created.Status = appsv1.DeploymentStatus{ObservedGeneration: created.Generation, Replicas: 1, ReadyReplicas: 1}
	if err := direct.Status().Update(ctx, created, client.FieldOwner("deployment-controller")); err != nil {
		t.Fatal(err)
	}
	result, err := applyManagedObject(ctx, direct, desired())
	if err != nil || result.Created || !result.Changed {
		t.Fatalf("existing object apply: %+v, %v", result, err)
	}
	actual := &appsv1.Deployment{}
	if err := direct.Get(ctx, client.ObjectKeyFromObject(created), actual); err != nil {
		t.Fatal(err)
	}
	if len(actual.Spec.Template.Spec.Containers[0].Env) != 0 || actual.Status.ReadyReplicas != 1 {
		t.Fatal("apply must remove unwanted environment entries and preserve controller status")
	}
	result, err = applyManagedObject(ctx, direct, desired())
	if err != nil || result.Changed {
		t.Fatalf("repeat apply: %+v, %v", result, err)
	}
}
