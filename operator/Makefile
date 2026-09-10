.PHONY: generate test build

generate:
	go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.21.0 object paths=./api/...
	go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.21.0 crd rbac:roleName=tadoss-operator paths=./... output:crd:artifacts:config=config/crd/bases output:rbac:artifacts:config=config/rbac
	cp config/crd/bases/*.yaml ../deploy/charts/tadoss-operator/crds/
	cp config/rbac/role.yaml ../deploy/charts/tadoss-operator/templates/role.yaml

test:
	go test ./...

build:
	CGO_ENABLED=0 go build -trimpath -o bin/manager ./cmd
