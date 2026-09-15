package controller

import (
	"context"
	"encoding/json"
	"testing"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func instance() *tadossv1.Tadoss {
	return &tadossv1.Tadoss{ObjectMeta: metav1.ObjectMeta{Name: "example", Namespace: "test", UID: types.UID("uid-1")}, Spec: tadossv1.TadossSpec{Profile: "local-kind", Image: "livewyer/tadoss-api:test", ImagePullPolicy: corev1.PullIfNotPresent, Database: tadossv1.DatabaseSpec{Provider: "managed", Storage: "5Gi"}}}
}

func TestProfilesAndCredentialRollouts(t *testing.T) {
	for _, profile := range []string{"local-kind", "edge", "single-server", "multi-server"} {
		t.Run(profile, func(t *testing.T) {
			cr := instance()
			cr.Spec.Profile = profile
			wantAPI, wantDB := int32(1), 1
			if profile == "multi-server" {
				wantAPI, wantDB = 2, 3
			}
			if replicas(cr) != wantAPI || databaseInstances(cr) != wantDB {
				t.Fatal("wrong profile defaults")
			}
			deployment := workloads(cr, "db", "token", "checksum")[0].(*appsv1.Deployment)
			container := deployment.Spec.Template.Spec.Containers[0]
			if *deployment.Spec.Replicas != wantAPI || *deployment.Spec.Template.Spec.AutomountServiceAccountToken || !*container.SecurityContext.ReadOnlyRootFilesystem {
				t.Fatal("invalid workload security or replicas")
			}
			if container.Env[0].ValueFrom.SecretKeyRef.Name != "db" || container.Env[1].ValueFrom.SecretKeyRef.Name != "token" {
				t.Fatal("credentials must be secret references")
			}
			cr.Spec.Replicas = ptr.To(int32(4))
			if replicas(cr) != 4 {
				t.Fatal("explicit replicas ignored")
			}
		})
	}
	cr := instance()
	first := schemaJob(cr, "db", "token", "database-v1")
	service := workloads(cr, "db", "token", "checksum")[1].(*corev1.Service)
	matches := true
	for key, value := range service.Spec.Selector {
		if first.Spec.Template.Labels[key] != value {
			matches = false
		}
	}
	if matches {
		t.Fatal("API Service must not route requests to schema migration pods")
	}
	if first.Name != schemaJob(cr, "db", "rotated-token", "database-v1").Name {
		t.Fatal("token rotation must not repeat schema migration")
	}
	if first.Name == schemaJob(cr, "db", "token", "database-v2").Name {
		t.Fatal("database credential changes must retry migration")
	}
	cr.Spec.Image = "livewyer/tadoss-api:new"
	if first.Name == schemaJob(cr, "db", "token", "database-v1").Name {
		t.Fatal("image upgrades must run their migration")
	}
}

func TestOwnershipTokenPersistenceAndRetention(t *testing.T) {
	ctx := context.Background()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{tadossv1.AddToScheme, corev1.AddToScheme, cnpgv1.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	cr := instance()
	cr.Finalizers = []string{finalizer}
	r := &TadossReconciler{Client: fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(cr).WithObjects(cr).Build(), Scheme: scheme}
	first, err := r.token(ctx, cr)
	if err != nil {
		t.Fatal(err)
	}
	second, err := r.token(ctx, cr)
	if err != nil || string(first.Data["token"]) != string(second.Data["token"]) {
		t.Fatal("reconciliation changed the token")
	}
	foreign := &corev1.Service{ObjectMeta: metadata(cr, "-api")}
	if err := r.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if r.apply(ctx, cr, foreign.DeepCopy()) == nil {
		t.Fatal("foreign resource was adopted")
	}
	cluster := databaseCluster(cr)
	if err := controllerutil.SetControllerReference(cr, cluster, scheme); err != nil {
		t.Fatal(err)
	}
	if err := r.Create(ctx, cluster); err != nil {
		t.Fatal(err)
	}
	cr.Spec.DeletionPolicy = tadossv1.DeletionPolicyRetain
	cr.Spec.DeletionProtection = ptr.To(false)
	if _, err := r.finalize(ctx, cr); err != nil {
		t.Fatal(err)
	}
	retained := &cnpgv1.Cluster{}
	if err := r.Get(ctx, client.ObjectKeyFromObject(cluster), retained); err != nil {
		t.Fatal(err)
	}
	if len(retained.OwnerReferences) != 0 {
		t.Fatal("retained database would be garbage collected")
	}
}

func TestDatabaseAndIngressOverrides(t *testing.T) {
	cr := instance()
	cluster := databaseCluster(cr)
	if cluster.Spec.ImageName != tadossv1.DefaultPostgresImage || cluster.Spec.Resources.Limits.Memory().String() != "1Gi" {
		t.Fatal("managed database defaults not applied")
	}
	cr.Spec.Database.Image = "registry.example.com/postgresql:18"
	cr.Spec.Database.Resources = corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")}}
	cluster = databaseCluster(cr)
	if cluster.Spec.ImageName != cr.Spec.Database.Image || cluster.Spec.Resources.Requests.Memory().String() != "2Gi" || cluster.Spec.Resources.Limits.Memory().String() != "1Gi" {
		t.Fatal("managed database overrides ignored")
	}
	cr.Spec.Ingress = tadossv1.IngressSpec{Host: "api.example.com", ClassName: "traefik", TLSSecretName: "tls", ClusterIssuer: "issuer", Annotations: map[string]string{"cert-manager.io/cluster-issuer": "other", "example.com/redirect": "true"}}
	ingress := workloads(cr, "db", "token", "checksum")[2].(*networkingv1.Ingress)
	if ingress.Annotations["cert-manager.io/cluster-issuer"] != "issuer" || ingress.Annotations["example.com/redirect"] != "true" {
		t.Fatal("ingress annotations must merge with the issuer annotation winning")
	}
	var typed tadossv1.Tadoss
	if err := json.Unmarshal([]byte(`{"spec":{}}`), &typed); err != nil || typed.Spec.DeletionProtection != nil {
		t.Fatal("an absent deletionProtection must stay unset so the CRD default applies")
	}
}
