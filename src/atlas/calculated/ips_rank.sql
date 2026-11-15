-- The IP ranks of a build's network lists (calculated v15, netlists.rank()), given as list ids
-- and the network kind of each. A row's place is the API's place key (placeKeyOf in
-- api/internal/build/ranks.go): the upper-cased country code, then region code and region, then
-- city, for c, r and g; the ASN number for a; the city's key and the ASN number for y (City x
-- ASN). A list holds one row per label (a place once per country name, an ASN once per name
-- and usage type), so a list's IPs at a place are the sum over its rows of the place (s); a
-- place without a country code (an ASN row without a number) has no key and no rank. Each list
-- is ranked among the lists of the same kind and window at the place (r): 1 + the lists with
-- strictly more IPs there (each counted once, at its first row; EXCLUDE GROUP leaves out equal
-- sums), so ties share the better rank as rankOf; peers are the lists there. Every row of the
-- place gets its list's rank and peers. Window functions only, one pass. {rows} is the ranked
-- rows (r: list_id, dimension, position, place_id, asn_label_id, ips) with each list's kind and
-- window (u): the stored rows of netlists.rank()'s lists, or the merged rows a build writes
-- ranked (netlists.MERGED_RANKED); netlists.py adds the statement that writes r.
WITH k AS (
 SELECT r.list_id,r.dimension,r.position,r.ips,u.kind,u.window_hours,
  CASE WHEN r.dimension<>'a' THEN upper(btrim(coalesce(p.country_code,''))) ELSE '' END AS cc,
  CASE WHEN r.dimension NOT IN ('a','c') THEN coalesce(p.region_code,'') END AS region_code,
  CASE WHEN r.dimension NOT IN ('a','c') THEN coalesce(p.region,'') END AS region,
  CASE WHEN r.dimension IN ('g','y') THEN coalesce(p.city,'') END AS city,
  CASE WHEN r.dimension IN ('a','y') THEN n.asn END AS asn
 FROM {rows} LEFT JOIN place_names p USING(place_id)
  LEFT JOIN asn_labels a USING(asn_label_id) LEFT JOIN asn_names n USING(asn_name_id)
 WHERE r.ips>0 AND r.dimension IN ('c','r','g','a','y')
), s AS (
 SELECT list_id,dimension,position,kind,window_hours,cc,region_code,region,city,asn,
  sum(ips) OVER own AS ips,row_number() OVER (own ORDER BY position)=1 AS first
 FROM k WHERE (dimension='a' OR cc<>'') AND (dimension NOT IN ('a','y') OR asn IS NOT NULL)
 WINDOW own AS (PARTITION BY kind,window_hours,dimension,cc,region_code,region,city,asn,list_id)
), r AS (
 SELECT list_id,dimension,position,
  1+count(*) FILTER (WHERE first) OVER (place ORDER BY ips DESC
   RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW EXCLUDE GROUP) AS ips_rank,
  count(*) FILTER (WHERE first) OVER place AS ips_peers
 FROM s WINDOW place AS (PARTITION BY kind,window_hours,dimension,cc,region_code,region,city,asn)
)
