.PHONY: generate test lint vet build envtest

CONTROLLER_GEN ?= controller-gen
KUSTOMIZE ?= kustomize
LOCALBIN := $(CURDIR)/bin
ENVTEST_K8S_VERSION ?= 1.36
ENVTEST_VERSION ?= v0.24.1
ENVTEST := $(LOCALBIN)/setup-envtest

generate:
	$(CONTROLLER_GEN) object paths=./api/...
	$(CONTROLLER_GEN) crd rbac:roleName=tadoss-operator paths=./... output:crd:artifacts:config=config/crd/bases output:rbac:artifacts:config=config/rbac
	$(KUSTOMIZE) build ../deploy/operator > ../deploy/operator/install.yaml

$(ENVTEST):
	GOBIN=$(LOCALBIN) go install sigs.k8s.io/controller-runtime/tools/setup-envtest@$(ENVTEST_VERSION)

envtest: $(ENVTEST)
	$(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(LOCALBIN) -p path > /dev/null

# Runs the unit tests and, with a kube-apiserver from setup-envtest, the reconcile tests.
test: envtest
	KUBEBUILDER_ASSETS="$$($(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(LOCALBIN) -p path)" go test -race -count=1 ./...

vet:
	go vet ./...

lint:
	golangci-lint run ./...

build:
	CGO_ENABLED=0 go build -trimpath -o bin/manager ./cmd
