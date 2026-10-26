// Package releases selects the runtime and schema for an exact product release.
package releases

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"slices"
)

//go:embed catalogue.json
var catalogue []byte

// Release binds a product version to its runtime image and schema.
type Release struct {
	Version      string   `json:"version"`
	APIImage     string   `json:"apiImage"`
	SchemaTarget string   `json:"schemaTarget"`
	UpgradeFrom  []string `json:"upgradeFrom"`
}

// SelectionError reports a release choice that cannot be applied safely.
type SelectionError struct {
	Reason  string
	Message string
}

func (e *SelectionError) Error() string { return e.Message }

// Resolve validates a release transition against the embedded catalogue.
func Resolve(version, current, pending string) (Release, error) {
	var entries []Release
	if err := json.Unmarshal(catalogue, &entries); err != nil {
		return Release{}, err
	}
	return selectRelease(entries, version, current, pending)
}

func selectRelease(entries []Release, version, current, pending string) (Release, error) {
	if pending != "" && pending != version {
		return Release{}, &SelectionError{"UpgradeInProgress", fmt.Sprintf("Complete the selected release %q before changing spec.version", pending)}
	}
	for _, entry := range entries {
		if entry.Version == version {
			if current != "" && current != version && !slices.Contains(entry.UpgradeFrom, current) {
				return Release{}, &SelectionError{"UnsupportedUpgrade", fmt.Sprintf("Release %q cannot be upgraded directly to %q", current, version)}
			}
			return entry, nil
		}
	}
	return Release{}, &SelectionError{"UnsupportedVersion", fmt.Sprintf("Unsupported spec.version %q", version)}
}
