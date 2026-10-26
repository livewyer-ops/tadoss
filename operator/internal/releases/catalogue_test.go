package releases

import (
	"errors"
	"testing"
)

func TestReleaseTransitions(t *testing.T) {
	entries := []Release{
		{Version: "old", SchemaTarget: "baseline"},
		{Version: "new", SchemaTarget: "next", UpgradeFrom: []string{"old"}},
		{Version: "later", SchemaTarget: "later", UpgradeFrom: []string{"new"}},
	}
	for _, tc := range []struct{ name, version, current, pending, reason string }{
		{"fresh", "new", "", "", ""},
		{"same", "old", "old", "", ""},
		{"upgrade", "new", "old", "", ""},
		{"resume", "new", "old", "new", ""},
		{"resume install", "new", "", "new", ""},
		{"downgrade", "old", "new", "", "UnsupportedUpgrade"},
		{"skip", "later", "old", "", "UnsupportedUpgrade"},
		{"unknown source", "new", "unknown", "", "UnsupportedUpgrade"},
		{"unknown target", "missing", "old", "", "UnsupportedVersion"},
		{"change pending", "later", "old", "new", "UpgradeInProgress"},
		{"abort pending", "old", "old", "new", "UpgradeInProgress"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			selected, err := selectRelease(entries, tc.version, tc.current, tc.pending)
			if tc.reason == "" {
				if err != nil || selected.Version != tc.version {
					t.Fatalf("selection: %+v, %v", selected, err)
				}
				return
			}
			var selection *SelectionError
			if !errors.As(err, &selection) || selection.Reason != tc.reason {
				t.Fatalf("want %s, got %v", tc.reason, err)
			}
		})
	}
}
