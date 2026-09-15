.PHONY: generate test build

CONTROLLER_GEN ?= controller-gen
KUSTOMIZE ?= kustomize
CHART := ../deploy/charts/tadoss-operator

generate:
	$(CONTROLLER_GEN) object paths=./api/...
	$(CONTROLLER_GEN) crd rbac:roleName=tadoss-operator paths=./... output:crd:artifacts:config=config/crd/bases output:rbac:artifacts:config=config/rbac
	{ printf '{{- if .Values.crds.install }}\n'; $(KUSTOMIZE) build config/crd; printf '{{- end }}\n'; } > $(CHART)/templates/crd.yaml
	cp config/rbac/role.yaml $(CHART)/templates/role.yaml

test:
	go test -race ./...

build:
	CGO_ENABLED=0 go build -trimpath -o bin/manager ./cmd
