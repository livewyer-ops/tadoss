// Package releases selects the runtime and schema for an exact product release.
package releases

import (
	_ "embed"
	"encoding/json"
	"fmt"
)

//go:embed catalogue.json
var catalogue []byte

// Release binds a product version to its runtime image and schema.
type Release struct {
	Version      string `json:"version"`
	APIImage     string `json:"apiImage"`
	SchemaTarget string `json:"schemaTarget"`
}

// Resolve requires an exact product version from the embedded catalogue.
func Resolve(version string) (Release, error) {
	var entries []Release
	if err := json.Unmarshal(catalogue, &entries); err != nil {
		return Release{}, err
	}
	for _, entry := range entries {
		if entry.Version == version {
			return entry, nil
		}
	}
	return Release{}, fmt.Errorf("unsupported spec.version %q", version)
}
