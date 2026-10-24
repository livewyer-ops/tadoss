// The tadoss-operator manager reconciles Tadoss instances.
package main

import (
	"flag"
	"os"
	"strings"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/controller"
	"github.com/livewyer-ops/tadoss/operator/internal/controller/defaults"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/filters"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

// Authenticated metrics need TokenReview and SubjectAccessReview.
// +kubebuilder:rbac:groups=authentication.k8s.io,resources=tokenreviews,verbs=create
// +kubebuilder:rbac:groups=authorization.k8s.io,resources=subjectaccessreviews,verbs=create

func main() {
	var namespaces, metrics, probes string
	var leaderElection, metricsSecure bool
	flag.StringVar(&namespaces, "watch-namespaces", os.Getenv("WATCH_NAMESPACES"), "Comma-separated namespace scope; empty watches all")
	flag.StringVar(&metrics, "metrics-bind-address", ":8443", "Metrics address; 0 disables the endpoint")
	flag.BoolVar(&metricsSecure, "metrics-secure", true, "Serve metrics over HTTPS and require a token with access to /metrics")
	flag.StringVar(&probes, "health-probe-bind-address", ":8081", "Health address")
	flag.BoolVar(&leaderElection, "leader-elect", true, "Use Kubernetes leader election")
	opts := zap.Options{}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()
	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))
	scheme := runtime.NewScheme()
	must(clientgoscheme.AddToScheme(scheme))
	must(tadossv1.AddToScheme(scheme))
	must(cnpgv1.AddToScheme(scheme))
	metricsOptions := metricsserver.Options{BindAddress: metrics, SecureServing: metricsSecure}
	if metricsSecure {
		metricsOptions.FilterProvider = filters.WithAuthenticationAndAuthorization
	}
	options := ctrl.Options{
		Scheme:                        scheme,
		LeaderElection:                leaderElection,
		LeaderElectionID:              "tadoss-operator.tadoss.livewyer.io",
		LeaderElectionReleaseOnCancel: true,
		HealthProbeBindAddress:        probes,
		Metrics:                       metricsOptions,
		// Secrets are watched as metadata only and read live, so credential contents never sit in the cache.
		Cache:  cache.Options{DefaultTransform: cache.TransformStripManagedFields()},
		Client: client.Options{Cache: &client.CacheOptions{DisableFor: []client.Object{&corev1.Secret{}, &corev1.ConfigMap{}}}},
	}
	if namespaces != "" {
		options.Cache.DefaultNamespaces = map[string]cache.Config{}
		for _, namespace := range strings.Split(namespaces, ",") {
			options.Cache.DefaultNamespaces[strings.TrimSpace(namespace)] = cache.Config{}
		}
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), options)
	must(err)
	reconciler := &controller.TadossReconciler{Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("tadoss-operator"), Defaults: defaults.Load(os.Getenv("TADOSS_INSTANCE_DEFAULTS"))}
	must(reconciler.SetupWithManager(mgr))
	must(mgr.AddHealthzCheck("healthz", healthz.Ping))
	must(mgr.AddReadyzCheck("readyz", healthz.Ping))
	must(mgr.Start(ctrl.SetupSignalHandler()))
}

func must(err error) {
	if err != nil {
		ctrl.Log.Error(err, "operator failed")
		os.Exit(1)
	}
}
