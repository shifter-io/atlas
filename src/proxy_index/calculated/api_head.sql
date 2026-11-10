-- The current build's API head (calculated_snapshots.api_head, calculated v14), written when a
-- build is published (parts.write_api_head()) and only when its head is a v4 head without one:
-- the head without "parts" and "overlap" (its rows are per cohort; a network's are its
-- overlap.peers), with only the every-group ("*") rows of "fleet", as text. It is what the API
-- built from the head before v14 (api/internal/store/network.go networkHeadSQL), and still
-- builds for a head without it; the API's tests write it with this file too.
UPDATE calculated_snapshots s SET api_head=((s.payload-'parts'-'overlap'-'fleet')
 || CASE WHEN s.payload ? 'fleet' THEN jsonb_build_object('fleet',(SELECT coalesce(jsonb_agg(f
  ORDER BY i),'[]'::jsonb) FROM jsonb_array_elements(s.payload->'fleet') WITH ORDINALITY e(f,i)
  WHERE f->>'cohort'='*')) ELSE '{}'::jsonb END)::text
FROM current_calculation c
WHERE c.build_id=s.build_id AND s.api_head IS NULL AND s.payload ? 'networks'
 AND s.payload->>'schema_version' IS NOT DISTINCT FROM '4'
