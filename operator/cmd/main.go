package main

import (
	"flag"
	"os"
	"strings"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	"github.com/livewyer-ops/tadoss/operator/internal/controller"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

func main() {
	var namespaces, metrics, probes string
	var leaderElection bool
	flag.StringVar(&namespaces, "watch-namespaces", "", "Comma-separated namespace scope; empty watches all")
	flag.StringVar(&metrics, "metrics-bind-address", ":8080", "Metrics address")
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
	options := ctrl.Options{Scheme: scheme, LeaderElection: leaderElection, LeaderElectionID: "tadoss-operator.tadoss.livewyer.io", HealthProbeBindAddress: probes, Metrics: metricsserver.Options{BindAddress: metrics}}
	if namespaces != "" {
		options.Cache.DefaultNamespaces = map[string]cache.Config{}
		for _, namespace := range strings.Split(namespaces, ",") {
			options.Cache.DefaultNamespaces[strings.TrimSpace(namespace)] = cache.Config{}
		}
	}
	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), options)
	must(err)
	reconciler := &controller.TadossReconciler{Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("tadoss-operator")}
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
