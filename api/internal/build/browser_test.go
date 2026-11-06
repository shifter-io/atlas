package build

import (
	"encoding/json"
	"reflect"
	"testing"

	"github.com/Zeus-Development/proxy-index/api/internal/snap"
)

// Without a methodology target_groups, the group and sources come from the
// windows' targets.<id>.browser objects (K-BP6).
func TestTargetGroupsFromTheWindows(t *testing.T) {
	var s snap.Snapshot
	if err := json.Unmarshal([]byte(`{"methodology": {"id": "m", "settings": {}}, "products": [{"id": "1:x", "windows": {
 "24h": {"targets": {"generic_http": {"metrics": {}},
  "ai_answers": {"metrics": {}, "browser": {"group": null, "sources": ["ai_overview", "ai_chatgpt"]}},
  "ai_overview": {"metrics": {}, "browser": {"group": "ai_answers", "sources": null}}}},
 "30d": {"targets": {"ai_chatgpt": {"metrics": {}, "browser": {"group": "ai_answers", "sources": null}}}}}}]}`), &s); err != nil {
		t.Fatal(err)
	}
	group, sources := targetGroups(&s)
	if !reflect.DeepEqual(group, map[string]string{"ai_overview": "ai_answers", "ai_chatgpt": "ai_answers"}) {
		t.Errorf("group %v", group)
	}
	if !reflect.DeepEqual(sources, map[string][]string{"ai_answers": {"ai_overview", "ai_chatgpt"}}) {
		t.Errorf("sources %v", sources)
	}
	if g, src := targetGroups(nil); len(g) != 0 || len(src) != 0 {
		t.Errorf("no snapshot: %v %v", g, src)
	}
}
