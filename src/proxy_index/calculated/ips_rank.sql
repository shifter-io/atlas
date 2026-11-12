-- The IP ranks of a build's network lists (calculated v15, netlists.rank()), given as list ids
-- and the network kind of each. A row's place is the API's place key (placeKeyOf in
-- api/internal/build/ranks.go): the upper-cased country code, then region code and region, then
-- city, for c, r and g; the ASN number for a; the city's key and the ASN number for y (City x
-- ASN). A list holds one row per label (a place once per country name, an ASN once per name
-- and usage type), so each list's rows with IPs are summed per place first (s); a place without
-- a country code (an ASN row without a number) has no key and no rank. Each list's sum is ranked
-- among those of the lists of the same kind and window (r): rank() by IPs, 1 = most, ties
-- sharing the better rank as rankOf, and the number of those lists. Every row of the place gets
-- its place's rank and peers.
WITH k AS (
 SELECT r.list_id,r.dimension,r.position,r.ips,u.kind,l.window_hours,
  CASE
   WHEN r.dimension='a' THEN n.asn::text
   WHEN upper(btrim(coalesce(p.country_code,'')))='' OR (r.dimension='y' AND n.asn IS NULL)
    THEN NULL
   ELSE jsonb_build_array(upper(btrim(p.country_code)),
    CASE WHEN r.dimension<>'c' THEN coalesce(p.region_code,'') END,
    CASE WHEN r.dimension<>'c' THEN coalesce(p.region,'') END,
    CASE WHEN r.dimension IN ('g','y') THEN coalesce(p.city,'') END,
    CASE WHEN r.dimension='y' THEN n.asn END)::text
  END AS place
 FROM calculated_list_rows r JOIN unnest(%(lists)s::bigint[],%(kinds)s::text[]) u(list_id,kind)
  USING(list_id) JOIN calculated_lists l USING(list_id) LEFT JOIN place_names p USING(place_id)
  LEFT JOIN asn_labels a USING(asn_label_id) LEFT JOIN asn_names n USING(asn_name_id)
 WHERE r.ips>0 AND r.dimension IN ('c','r','g','a','y')
), s AS (
 SELECT kind,window_hours,dimension,place,list_id,sum(ips) AS ips FROM k
 WHERE place IS NOT NULL GROUP BY 1,2,3,4,5
), r AS (
 SELECT list_id,dimension,place,
  rank() OVER (PARTITION BY kind,window_hours,dimension,place ORDER BY ips DESC) AS ips_rank,
  count(*) OVER (PARTITION BY kind,window_hours,dimension,place) AS ips_peers
 FROM s
)
UPDATE calculated_list_rows t SET ips_rank=r.ips_rank,ips_peers=r.ips_peers
FROM k JOIN r USING(list_id,dimension,place)
WHERE (t.list_id,t.dimension,t.position)=(k.list_id,k.dimension,k.position)
