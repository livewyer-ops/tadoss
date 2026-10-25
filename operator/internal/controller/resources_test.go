package controller

import (
	"context"
	"encoding/json"
	"testing"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
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
	return &tadossv1.Tadoss{ObjectMeta: metav1.ObjectMeta{Name: "example", Namespace: "test", UID: types.UID("uid-1")}, Spec: tadossv1.TadossSpec{Version: "0.1.0", Profile: "local-kind", API: tadossv1.WorkloadSpec{Image: "livewyer/tadoss-api:test", ImagePullPolicy: corev1.PullIfNotPresent}, Backends: tadossv1.BackendsSpec{DB: tadossv1.DatabaseBackendSpec{ProvidedBy: "cnpg", CNPG: tadossv1.DatabaseSpec{Storage: "5Gi"}}}}}
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
			deployment := workloads(cr, "db", "token", "checksum").Deployment
			container := deployment.Spec.Template.Spec.Containers[0]
			if *deployment.Spec.Replicas != wantAPI || *deployment.Spec.Template.Spec.AutomountServiceAccountToken || !*container.SecurityContext.ReadOnlyRootFilesystem {
				t.Fatal("invalid workload security or replicas")
			}
			if container.Env[0].ValueFrom.SecretKeyRef.Name != "db" || container.Env[1].ValueFrom.SecretKeyRef.Name != "token" {
				t.Fatal("credentials must be secret references")
			}
			cr.Spec.API.ReplicaCount = ptr.To(int32(4))
			if replicas(cr) != 4 {
				t.Fatal("explicit replicas ignored")
			}
		})
	}
	cr := instance()
	set := workloads(cr, "db", "token", "checksum")
	deployment := set.Deployment
	if _, ok := deployment.Spec.Selector.MatchLabels[instanceUID]; ok {
		t.Fatal("the Deployment selector must not depend on the instance UID")
	}
	for key, value := range deployment.Spec.Selector.MatchLabels {
		if deployment.Spec.Template.Labels[key] != value {
			t.Fatal("the pod template must match the Deployment selector")
		}
	}
	first := schemaJob(cr, "db", "database-v1")
	if len(first.Spec.Template.Spec.Containers[0].Env) != 2 || first.Spec.Template.Spec.Containers[0].Env[0].Name != "TADOSS_DATABASE" {
		t.Fatal("the schema Job must receive the database URI and CA")
	}
	if objects := set.objects(); len(objects) != 3 || objects[0] != client.Object(set.Deployment) || objects[1] != client.Object(set.Service) {
		t.Fatal("single-replica workloads must include API, Service and worker")
	}
	service := set.Service
	matches := true
	for key, value := range service.Spec.Selector {
		if first.Spec.Template.Labels[key] != value {
			matches = false
		}
	}
	if matches {
		t.Fatal("API Service must not route requests to schema migration pods")
	}
	if first.Name != schemaJob(cr, "db", "database-v1").Name {
		t.Fatal("the Job name must be stable for unchanged inputs")
	}
	if first.Name == schemaJob(cr, "db", "database-v2").Name {
		t.Fatal("database credential changes must retry migration")
	}
	cr.Spec.API.Resources = corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")}}
	if first.Name == schemaJob(cr, "db", "database-v1").Name {
		t.Fatal("resource changes must produce a new Job so a stuck migration can retry")
	}
	cr.Spec.API.Resources = corev1.ResourceRequirements{}
	cr.Spec.API.Image = "livewyer/tadoss-api:new"
	if first.Name == schemaJob(cr, "db", "database-v1").Name {
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
	if _, err := r.handleDeletion(ctx, cr); err != nil {
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
	cr.Spec.Backends.DB.CNPG.Image = "registry.example.com/postgresql:18"
	cr.Spec.Backends.DB.CNPG.Resources = corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")}}
	cluster = databaseCluster(cr)
	if cluster.Spec.ImageName != cr.Spec.Backends.DB.CNPG.Image || cluster.Spec.Resources.Requests.Memory().String() != "2Gi" || cluster.Spec.Resources.Limits.Memory().String() != "2Gi" {
		t.Fatal("managed database overrides ignored")
	}
	cr.Spec.Ingress = tadossv1.IngressSpec{Host: "api.example.com", ClassName: "traefik", TLSSecretName: "tls", ClusterIssuer: "issuer", Annotations: map[string]string{"cert-manager.io/cluster-issuer": "other", "example.com/redirect": "true"}}
	ingress := workloads(cr, "db", "token", "checksum").Ingress
	if ingress.Annotations["cert-manager.io/cluster-issuer"] != "issuer" || ingress.Annotations["example.com/redirect"] != "true" {
		t.Fatal("ingress annotations must merge with the issuer annotation winning")
	}
	if ingress.Annotations["cert-manager.io/subject-organizations"] == "" {
		t.Fatal("self-signed certificates need a nonempty issuer distinguished name")
	}
	cr.Spec.Ingress.Annotations["cert-manager.io/subject-organizations"] = "Example"
	updatedIngress := workloads(cr, "db", "token", "checksum").Ingress
	if updatedIngress.Annotations["cert-manager.io/subject-organizations"] != "Example" {
		t.Fatal("explicit certificate subject must be preserved")
	}
	if updatedIngress.Labels["tadoss.livewyer.io/certificate-settings"] == ingress.Labels["tadoss.livewyer.io/certificate-settings"] {
		t.Fatal("changing certificate settings must trigger ingress-shim certificate reconciliation")
	}
	var typed tadossv1.Tadoss
	if err := json.Unmarshal([]byte(`{"spec":{}}`), &typed); err != nil || typed.Spec.DeletionProtection != nil {
		t.Fatal("an absent deletionProtection must stay unset so the CRD default applies")
	}
}

func TestNativeWorkloadAndRecoveryConfiguration(t *testing.T) {
	cr := instance()
	cr.Status.Resolved.SchemaTarget = "0001_baseline"
	cr.Spec.API.Env = map[string]string{"TADOSS_HOST": "wrong", "CUSTOM": "value", "TADOSS_API_TOKEN": "must-not-override"}
	cr.Spec.Auth.BasicSecret = "basic"
	cr.Spec.Backends.DB.ProvidedBy = tadossv1.ProviderExternal
	cr.Spec.Backends.DB.External.CAConfigMap = &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "db-ca"}, Key: "root.pem"}
	cr.Spec.API.Resources = corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("32Mi")}}
	template := pod(cr, "db", "token")
	container := template.Spec.Containers[0]
	seen := map[string]corev1.EnvVar{}
	for _, env := range container.Env {
		if _, ok := seen[env.Name]; ok {
			t.Fatalf("duplicate env %s", env.Name)
		}
		seen[env.Name] = env
	}
	if seen["TADOSS_HOST"].Value != "0.0.0.0" || seen["CUSTOM"].Value != "value" || seen["TADOSS_API_TOKEN"].ValueFrom == nil || seen["TADOSS_DATABASE_SSL_ROOT_CERT"].Value == "" {
		t.Fatal("typed settings must own their variables")
	}
	if container.Resources.Requests.Memory().Cmp(*container.Resources.Limits.Memory()) > 0 || container.StartupProbe == nil {
		t.Fatal("invalid resource defaults or missing startup probe")
	}
	cr.Spec.Backends.DB.CNPG.Backup = tadossv1.BackupSpec{Enabled: true, RetentionPolicy: "30d", ObjectStore: &cnpgv1.BarmanObjectStoreConfiguration{DestinationPath: "s3://backups/"}}
	cluster := databaseCluster(cr)
	if cluster.Spec.Backup.BarmanObjectStore.DestinationPath != "s3://backups/" {
		t.Fatal("backup configuration lost")
	}
	cr.Spec.Backends.DB.CNPG.Restore = tadossv1.RestoreSpec{Enabled: true, Source: "original-db", TargetTime: "2026-10-20 12:00:00+00", ObjectStore: cr.Spec.Backends.DB.CNPG.Backup.ObjectStore}
	cluster = databaseCluster(cr)
	if cluster.Spec.Bootstrap.InitDB != nil || cluster.Spec.Bootstrap.Recovery.Source != "original-db" || len(cluster.Spec.ExternalClusters) != 1 {
		t.Fatal("restore must use a separate recovery bootstrap")
	}
}

func TestWorkerAndMigrationUseManagedCAWithoutAPIAuth(t *testing.T) {
	cr := instance()
	cr.Spec.Auth = tadossv1.AuthSpec{BasicSecret: "basic", OAuth2: tadossv1.OAuth2Spec{Issuer: "https://issuer.example", Audience: "tadoss", JWKSURI: "https://issuer.example/jwks"}}
	set := workloads(cr, "db", "token", "revision")
	job := schemaJob(cr, "db", "revision")
	for _, template := range []corev1.PodTemplateSpec{set.Deployment.Spec.Template, set.Worker.Spec.Template, job.Spec.Template} {
		foundCA := false
		for _, volume := range template.Spec.Volumes {
			if volume.Name == "database-ca" {
				foundCA = volume.Secret != nil && volume.Secret.SecretName == "example-db-ca" && volume.Secret.Items[0].Key == "ca.crt"
			}
		}
		if !foundCA {
			t.Fatal("managed CA must be mounted in all database clients")
		}
		container := template.Spec.Containers[0]
		rootCert := false
		for _, env := range container.Env {
			if container.Name != "api" && apiAuthEnv(env) {
				t.Fatalf("%s received API auth: %s", container.Name, env.Name)
			}
			rootCert = rootCert || env.Name == "TADOSS_DATABASE_SSL_ROOT_CERT" && env.Value == "/etc/tadoss/database-ca/ca.pem"
		}
		if !rootCert {
			t.Fatal("database clients must enable verify-full")
		}
	}
	cr.Spec.Ingress.Host = "example.test"
	if workloads(cr, "db", "token", "revision").Ingress.Spec.IngressClassName != nil {
		t.Fatal("unset class must allow cluster default")
	}
}
