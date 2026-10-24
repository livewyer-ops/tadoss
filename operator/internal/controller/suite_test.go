package controller

import (
	"context"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	cnpgv1 "github.com/cloudnative-pg/cloudnative-pg/api/v1"
	"github.com/go-logr/logr"
	tadossv1 "github.com/livewyer-ops/tadoss/operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
)

// live is the client of a manager running against an envtest API server; nil when
// KUBEBUILDER_ASSETS is unset, in which case the reconcile tests skip themselves.
var live client.Client
var fullDataLists atomic.Int64

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestMain(m *testing.M) {
	if os.Getenv("KUBEBUILDER_ASSETS") == "" {
		os.Exit(m.Run())
	}
	ctrl.SetLogger(logr.Discard())
	env := &envtest.Environment{
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "config", "crd", "bases"), cnpgCRDs()},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := env.Start()
	if err != nil {
		panic(err)
	}
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{clientgoscheme.AddToScheme, tadossv1.AddToScheme, cnpgv1.AddToScheme} {
		if err := add(scheme); err != nil {
			panic(err)
		}
	}
	cfg.WrapTransport = func(next http.RoundTripper) http.RoundTripper {
		return roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			if req.Method == http.MethodGet && (strings.HasSuffix(req.URL.Path, "/configmaps") || strings.HasSuffix(req.URL.Path, "/secrets")) && !strings.Contains(req.Header.Get("Accept"), "as=PartialObjectMetadata") {
				fullDataLists.Add(1)
			}
			return next.RoundTrip(req)
		})
	}
	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: "0"},
		HealthProbeBindAddress: "0",
		Client:                 client.Options{Cache: &client.CacheOptions{DisableFor: []client.Object{&corev1.Secret{}, &corev1.ConfigMap{}}}},
	})
	if err != nil {
		panic(err)
	}
	reconciler := &TadossReconciler{Client: mgr.GetClient(), Scheme: scheme, Recorder: mgr.GetEventRecorder("tadoss-operator")}
	if err := reconciler.SetupWithManager(mgr); err != nil {
		panic(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		if err := mgr.Start(ctx); err != nil {
			panic(err)
		}
	}()
	mgr.GetCache().WaitForCacheSync(ctx)
	live = mgr.GetClient()
	code := m.Run()
	cancel()
	_ = env.Stop()
	os.Exit(code)
}

func cnpgCRDs() string {
	out, err := exec.Command("go", "list", "-m", "-f", "{{.Dir}}", "github.com/cloudnative-pg/cloudnative-pg").Output()
	if err != nil {
		panic(err)
	}
	return filepath.Join(strings.TrimSpace(string(out)), "config", "crd", "bases")
}

func requireEnvtest(t *testing.T) {
	t.Helper()
	if live == nil {
		t.Skip("KUBEBUILDER_ASSETS is not set; run make test")
	}
}

func eventually(t *testing.T, what string, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(45 * time.Second)
	for time.Now().Before(deadline) {
		if check() {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}
